"""Unit tests for BroadHotelDiscovery (Phase 2b).

Covers all 12 scenarios in docs/call-center/phase2b-development-plan.md §5.
All tests inject `embed_fn` and `search_fn` so neither the e5-large
model nor a live Qdrant is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.discovery import (
    REASON_EMPTY_QUERY,
    REASON_ERROR,
    REASON_NO_MATCH,
    REASON_NO_REGION_SCOPE,
    BroadHotelDiscovery,
)

REGION = "antalya"


def _hit(
    score: float,
    *,
    hotel_id: str,
    region: str = REGION,
    category: str = "wellness",
    chunk_id: str | None = None,
    idx: int = 0,
    activity_tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": idx,
        "score": score,
        "payload": {
            "chunk_id": chunk_id or f"{hotel_id}::{category}::{idx}",
            "hotel_id": hotel_id,
            "hotel_name": hotel_id.replace("_", " ").title(),
            "category": category,
            "text": f"text-{idx}",
            "text_en": f"text-en-{idx}",
            "region": region,
            "country": "tr",
            "district": "belek",
            "price_tier": "luxury",
            "activity_tags": activity_tags if activity_tags is not None else ["spa"],
        },
    }


def _fake_embed(_: str) -> list[float]:
    return [0.1] * 8


def _make_search_fn(hits: list[dict[str, Any]]):
    calls: list[dict[str, Any]] = []

    async def _fn(vector, flt, limit):
        calls.append({"vector": vector, "filter": flt, "limit": limit})
        return hits

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


class TestBroadDiscoveryCore:
    async def test_empty_region_returns_no_region_scope(self) -> None:
        search = _make_search_fn([])
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=search)
        result = await d.discover(region="   ", query="anything")
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_REGION_SCOPE
        assert search.calls == []  # type: ignore[attr-defined]

    async def test_empty_query_returns_empty_query(self) -> None:
        search = _make_search_fn([])
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=search)
        result = await d.discover(region=REGION, query="   ")
        assert result["count"] == 0
        assert result["reason"] == REASON_EMPTY_QUERY
        assert search.calls == []  # type: ignore[attr-defined]

    async def test_happy_path_returns_distinct_hotels_sorted(self) -> None:
        # Scores within RELATIVE_MARGIN (0.05) so all three hotels survive the margin trim.
        hits = [
            _hit(0.87, hotel_id="rixos_premium_belek", idx=1),
            _hit(0.90, hotel_id="maxx_royal_belek", idx=2),
            _hit(0.88, hotel_id="cornelia_de_luxe", idx=3),
        ]
        d = BroadHotelDiscovery(
            max_hotels=5, min_score=0.25,
            embed_fn=_fake_embed, search_fn=_make_search_fn(hits),
        )
        result = await d.discover(region=REGION, query="luxury hotel with spa")
        assert result["count"] == 3
        ids = [h["hotel_id"] for h in result["hotels"]]
        assert ids == [
            "maxx_royal_belek", "cornelia_de_luxe", "rixos_premium_belek",
        ]
        assert result["top_score"] == pytest.approx(0.90)
        assert result["reason"] is None

    async def test_aggregation_dedupes_hotel_with_multiple_chunks(self) -> None:
        hits = [
            _hit(0.7, hotel_id="rixos_premium_belek", idx=1),
            _hit(0.9, hotel_id="rixos_premium_belek", idx=2, category="amenities"),
            _hit(0.5, hotel_id="rixos_premium_belek", idx=3, category="overview"),
        ]
        d = BroadHotelDiscovery(
            max_hotels=5, min_score=0.25,
            embed_fn=_fake_embed, search_fn=_make_search_fn(hits),
        )
        result = await d.discover(region=REGION, query="spa")
        assert result["count"] == 1
        assert result["hotels"][0]["score"] == pytest.approx(0.9)
        assert result["hotels"][0]["evidence_chunk"]["category"] == "amenities"

    async def test_region_filter_present_in_search_body(self) -> None:
        search = _make_search_fn([_hit(0.8, hotel_id="rixos_premium_belek", idx=1)])
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=search)
        await d.discover(region=REGION, query="spa")
        must = search.calls[0]["filter"]["must"]  # type: ignore[attr-defined]
        assert {"key": "region", "match": {"value": REGION}} in must

    async def test_activity_tags_appends_filter(self) -> None:
        search = _make_search_fn([
            _hit(0.8, hotel_id="rixos_premium_belek", idx=1, activity_tags=["scuba_diving"]),
        ])
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=search)
        await d.discover(region=REGION, query="diving", activity_tags=["scuba_diving"])
        must = search.calls[0]["filter"]["must"]  # type: ignore[attr-defined]
        assert any(
            m.get("key") == "activity_tags"
            and m["match"]["any"] == ["scuba_diving"]
            for m in must
        )

    async def test_category_hint_appends_filter_with_overview(self) -> None:
        search = _make_search_fn([
            _hit(0.8, hotel_id="rixos_premium_belek", idx=1, category="food_beverage"),
        ])
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=search)
        await d.discover(region=REGION, query="dinner", category_hint="food_beverage")
        must = search.calls[0]["filter"]["must"]  # type: ignore[attr-defined]
        assert any(
            m.get("key") == "category"
            and set(m["match"]["any"]) == {"food_beverage", "overview"}
            for m in must
        )

    async def test_all_below_min_score_returns_no_match(self) -> None:
        hits = [
            _hit(0.10, hotel_id="rixos_premium_belek", idx=1),
            _hit(0.22, hotel_id="maxx_royal_belek", idx=2),
        ]
        d = BroadHotelDiscovery(
            max_hotels=5, min_score=0.25,
            embed_fn=_fake_embed, search_fn=_make_search_fn(hits),
        )
        result = await d.discover(region=REGION, query="zzz")
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_MATCH
        assert result["top_score"] == pytest.approx(0.22)

    async def test_backend_raises_returns_retriever_error(self) -> None:
        async def boom(_v, _f, _l):
            raise RuntimeError("qdrant down")

        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=boom)
        result = await d.discover(region=REGION, query="any")
        assert result["count"] == 0
        assert result["reason"] == REASON_ERROR

    async def test_max_hotels_caps_results(self) -> None:
        hits = [
            _hit(0.9 - 0.05 * i, hotel_id=f"hotel_{i}", idx=i) for i in range(5)
        ]
        d = BroadHotelDiscovery(
            max_hotels=2, min_score=0.25,
            embed_fn=_fake_embed, search_fn=_make_search_fn(hits),
        )
        result = await d.discover(region=REGION, query="any")
        assert result["count"] == 2
        assert [h["hotel_id"] for h in result["hotels"]] == ["hotel_0", "hotel_1"]

    async def test_region_whitespace_is_stripped(self) -> None:
        search = _make_search_fn([_hit(0.8, hotel_id="rixos_premium_belek", idx=1)])
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=search)
        await d.discover(region="  " + REGION + "  ", query="any")
        must = search.calls[0]["filter"]["must"]  # type: ignore[attr-defined]
        assert must[0]["match"]["value"] == REGION

    async def test_response_shape_matches_contract(self) -> None:
        hits = [_hit(0.8, hotel_id="rixos_premium_belek", idx=1)]
        d = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=_make_search_fn(hits))
        result = await d.discover(region=REGION, query="any")
        assert set(result) == {
            "region", "query", "normalized_query",
            "top_score", "count", "hotels", "reason",
        }
        hotel = result["hotels"][0]
        assert set(hotel) == {"hotel_id", "score", "evidence_chunk", "payload"}
        assert set(hotel["evidence_chunk"]) == {
            "chunk_id", "category", "text", "text_en",
        }


class TestThresholdBoundaries:
    async def test_hit_exactly_at_min_score_is_kept(self) -> None:
        d = BroadHotelDiscovery(
            max_hotels=5, min_score=0.25,
            embed_fn=_fake_embed,
            search_fn=_make_search_fn([_hit(0.25, hotel_id="rixos_premium_belek", idx=1)]),
        )
        result = await d.discover(region=REGION, query="any")
        assert result["count"] == 1

    async def test_hit_just_below_min_score_is_dropped(self) -> None:
        d = BroadHotelDiscovery(
            max_hotels=5, min_score=0.25,
            embed_fn=_fake_embed,
            search_fn=_make_search_fn([_hit(0.2499, hotel_id="rixos_premium_belek", idx=1)]),
        )
        result = await d.discover(region=REGION, query="any")
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_MATCH


def _make_rerank_fn(scores_by_text: dict[str, float] | None = None,
                    raises: bool = False):
    """Return a deterministic rerank_fn that maps chunk text -> rerank score.

    Unmapped texts get 0.10 (sub-floor) so the test can assert filtering.
    """
    calls: list[tuple[str, list[str]]] = []
    mapping = scores_by_text or {}

    def _fn(query: str, passages: list[str]) -> list[float]:
        calls.append((query, list(passages)))
        if raises:
            raise RuntimeError("boom")
        return [mapping.get(p, 0.10) for p in passages]

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


class TestRerank:
    async def test_rerank_reorders_hits_by_rerank_score(self) -> None:
        # Cosine order: A (0.82) > B (0.80). Rerank inverts it: B > A.
        hits = [
            _hit(0.82, hotel_id="hotel_a", chunk_id="a1", idx=1),
            _hit(0.80, hotel_id="hotel_b", chunk_id="b1", idx=2),
        ]
        # Use _hit's "text" payload to identify which passage scores higher.
        rerank = _make_rerank_fn({"text-1": 0.78, "text-2": 0.92})
        d = BroadHotelDiscovery(
            embed_fn=_fake_embed,
            search_fn=_make_search_fn(hits),
            rerank_fn=rerank,
        )
        result = await d.discover(region=REGION, query="spa with thalasso")

        assert result["count"] == 2
        assert [h["hotel_id"] for h in result["hotels"]] == ["hotel_b", "hotel_a"]
        # Rerank score replaces cosine in the public `score`.
        assert result["hotels"][0]["score"] == pytest.approx(0.92)
        assert result["hotels"][1]["score"] == pytest.approx(0.78)
        # Rerank ran exactly once with the right passages.
        assert len(rerank.calls) == 1  # type: ignore[attr-defined]
        q, passages = rerank.calls[0]  # type: ignore[attr-defined]
        assert q == "spa with thalasso"
        assert passages == ["text-1", "text-2"]

    async def test_rerank_drops_hits_below_rerank_min_score(self) -> None:
        # Both hits clear the cosine floor (0.70) but only one clears the
        # rerank floor (0.50).
        hits = [
            _hit(0.78, hotel_id="hotel_a", chunk_id="a1", idx=1),
            _hit(0.81, hotel_id="hotel_b", chunk_id="b1", idx=2),
        ]
        rerank = _make_rerank_fn({"text-1": 0.72, "text-2": 0.12})
        d = BroadHotelDiscovery(
            embed_fn=_fake_embed,
            search_fn=_make_search_fn(hits),
            rerank_fn=rerank,
        )
        result = await d.discover(region=REGION, query="any")

        assert result["count"] == 1
        assert result["hotels"][0]["hotel_id"] == "hotel_a"
        assert result["hotels"][0]["score"] == pytest.approx(0.72)

    async def test_rerank_failure_falls_back_to_cosine(self) -> None:
        # If rerank raises, retrieval must still return cosine-scored hits.
        hits = [_hit(0.82, hotel_id="hotel_a", chunk_id="a1", idx=1)]
        rerank = _make_rerank_fn(raises=True)
        d = BroadHotelDiscovery(
            embed_fn=_fake_embed,
            search_fn=_make_search_fn(hits),
            rerank_fn=rerank,
        )
        result = await d.discover(region=REGION, query="any")

        assert result["count"] == 1
        assert result["hotels"][0]["score"] == pytest.approx(0.82)

    async def test_rerank_disabled_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Env kill-switch bypasses rerank even when rerank_fn is provided.
        monkeypatch.setenv("RAG_RERANK_ENABLED", "false")
        hits = [_hit(0.82, hotel_id="hotel_a", chunk_id="a1", idx=1)]
        rerank = _make_rerank_fn({"text-1": 0.99})  # would change score if it ran
        d = BroadHotelDiscovery(
            embed_fn=_fake_embed,
            search_fn=_make_search_fn(hits),
            rerank_fn=rerank,
        )
        result = await d.discover(region=REGION, query="any")

        assert result["hotels"][0]["score"] == pytest.approx(0.82)
        assert rerank.calls == []  # type: ignore[attr-defined]

    async def test_rerank_relative_margin_trims_weaker_hotels(self) -> None:
        # Rerank scores: A=0.90 (leader), B=0.80 (within 0.15), C=0.60 (trim).
        hits = [
            _hit(0.81, hotel_id="hotel_a", chunk_id="a1", idx=1),
            _hit(0.80, hotel_id="hotel_b", chunk_id="b1", idx=2),
            _hit(0.79, hotel_id="hotel_c", chunk_id="c1", idx=3),
        ]
        rerank = _make_rerank_fn(
            {"text-1": 0.90, "text-2": 0.80, "text-3": 0.60}
        )
        d = BroadHotelDiscovery(
            embed_fn=_fake_embed,
            search_fn=_make_search_fn(hits),
            rerank_fn=rerank,
            rerank_relative_margin=0.15,
        )
        result = await d.discover(region=REGION, query="any")

        assert [h["hotel_id"] for h in result["hotels"]] == ["hotel_a", "hotel_b"]

    async def test_rerank_called_only_once_per_discover(self) -> None:
        # Sanity: even though rerank_fn looks like an embed_fn, it must run
        # exactly once on the post-search batch \u2014 not per-hit.
        hits = [
            _hit(0.82, hotel_id=f"hotel_{i}", chunk_id=f"c{i}", idx=i)
            for i in range(1, 11)
        ]
        rerank = _make_rerank_fn({f"text-{i}": 0.60 for i in range(1, 11)})
        d = BroadHotelDiscovery(
            embed_fn=_fake_embed,
            search_fn=_make_search_fn(hits),
            rerank_fn=rerank,
        )
        await d.discover(region=REGION, query="any")
        assert len(rerank.calls) == 1  # type: ignore[attr-defined]
        assert len(rerank.calls[0][1]) == 10  # type: ignore[attr-defined]


