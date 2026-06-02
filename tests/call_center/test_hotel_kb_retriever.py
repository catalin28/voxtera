"""Unit tests for HotelKBRetriever (Phase 2a).

Covers all 10 scenarios in docs/call-center/phase2a-development-plan.md §5.
All tests inject `embed_fn` and `search_fn` so neither the e5-large
model nor a live Qdrant is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.kb_retriever import (
    REASON_EMPTY_QUERY,
    REASON_ERROR,
    REASON_NO_HOTEL_SCOPE,
    REASON_NO_MATCH,
    HotelKBRetriever,
)

HOTEL = "rixos_premium_belek"


def _hit(score: float, *, hotel_id: str = HOTEL, category: str = "activities",
         chunk_id: str | None = None, idx: int = 0) -> dict[str, Any]:
    return {
        "id": idx,
        "score": score,
        "payload": {
            "chunk_id": chunk_id or f"{hotel_id}::{category}::{idx}",
            "hotel_id": hotel_id,
            "category": category,
            "text": f"text-{idx}",
            "text_en": f"text-en-{idx}",
            "activity_tags": ["water_park"],
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


class TestHotelKBRetrieverCore:
    async def test_empty_hotel_id_returns_no_hotel_scope(self) -> None:
        search = _make_search_fn([])
        r = HotelKBRetriever(embed_fn=_fake_embed, search_fn=search)
        result = await r.retrieve(hotel_id="", query="anything")
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_HOTEL_SCOPE
        assert search.calls == []  # type: ignore[attr-defined]

    async def test_empty_query_returns_empty_query(self) -> None:
        search = _make_search_fn([])
        r = HotelKBRetriever(embed_fn=_fake_embed, search_fn=search)
        result = await r.retrieve(hotel_id=HOTEL, query="   ")
        assert result["count"] == 0
        assert result["reason"] == REASON_EMPTY_QUERY
        assert search.calls == []  # type: ignore[attr-defined]

    async def test_happy_path_returns_top_k_sorted(self) -> None:
        hits = [_hit(0.9, idx=1), _hit(0.6, idx=2), _hit(0.8, idx=3)]
        r = HotelKBRetriever(top_k=3, min_score=0.25,
                             embed_fn=_fake_embed, search_fn=_make_search_fn(hits))
        result = await r.retrieve(hotel_id=HOTEL, query="is there a water park")
        assert result["count"] == 3
        scores = [c["score"] for c in result["chunks"]]
        assert scores == sorted(scores, reverse=True)
        assert result["top_score"] == pytest.approx(0.9)
        assert result["reason"] is None

    async def test_all_below_min_score_returns_no_match(self) -> None:
        hits = [_hit(0.10, idx=1), _hit(0.20, idx=2)]
        r = HotelKBRetriever(top_k=3, min_score=0.25,
                             embed_fn=_fake_embed, search_fn=_make_search_fn(hits))
        result = await r.retrieve(hotel_id=HOTEL, query="dogecoin payments")
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_MATCH
        assert result["top_score"] == pytest.approx(0.20)  # best filtered-out

    async def test_category_hint_adds_filter_with_overview(self) -> None:
        search = _make_search_fn([_hit(0.8, category="food_beverage", idx=1)])
        r = HotelKBRetriever(embed_fn=_fake_embed, search_fn=search)
        result = await r.retrieve(hotel_id=HOTEL, query="breakfast hours",
                                  category_hint="food_beverage")
        assert result["count"] == 1
        flt = search.calls[0]["filter"]  # type: ignore[attr-defined]
        must = flt["must"]
        assert {"key": "hotel_id", "match": {"value": HOTEL}} in must
        assert any(
            m.get("key") == "category"
            and set(m["match"]["any"]) == {"food_beverage", "overview"}
            for m in must
        )

    async def test_backend_raises_returns_retriever_error(self) -> None:
        async def boom(_v, _f, _l):
            raise RuntimeError("qdrant down")

        r = HotelKBRetriever(embed_fn=_fake_embed, search_fn=boom)
        result = await r.retrieve(hotel_id=HOTEL, query="any")
        assert result["count"] == 0
        assert result["reason"] == REASON_ERROR

    async def test_top_k_caps_results(self) -> None:
        hits = [_hit(0.9 - 0.01 * i, idx=i) for i in range(5)]
        r = HotelKBRetriever(top_k=1, min_score=0.25,
                             embed_fn=_fake_embed, search_fn=_make_search_fn(hits))
        result = await r.retrieve(hotel_id=HOTEL, query="any")
        assert result["count"] == 1

    async def test_hotel_id_whitespace_is_stripped(self) -> None:
        search = _make_search_fn([_hit(0.8, idx=1)])
        r = HotelKBRetriever(embed_fn=_fake_embed, search_fn=search)
        await r.retrieve(hotel_id="  " + HOTEL + "  ", query="any")
        must = search.calls[0]["filter"]["must"]  # type: ignore[attr-defined]
        assert must[0]["match"]["value"] == HOTEL

    async def test_response_shape_matches_contract(self) -> None:
        hits = [_hit(0.8, idx=1)]
        r = HotelKBRetriever(embed_fn=_fake_embed, search_fn=_make_search_fn(hits))
        result = await r.retrieve(hotel_id=HOTEL, query="any")
        assert set(result) == {
            "hotel_id", "query", "normalized_query",
            "top_score", "count", "chunks", "reason",
        }
        assert set(result["chunks"][0]) == {
            "chunk_id", "score", "category", "text", "text_en", "activity_tags",
        }


class TestThresholdBoundaries:
    async def test_hit_exactly_at_min_score_is_kept(self) -> None:
        r = HotelKBRetriever(top_k=3, min_score=0.25,
                             embed_fn=_fake_embed,
                             search_fn=_make_search_fn([_hit(0.25, idx=1)]))
        result = await r.retrieve(hotel_id=HOTEL, query="any")
        assert result["count"] == 1

    async def test_hit_just_below_min_score_is_dropped(self) -> None:
        r = HotelKBRetriever(top_k=3, min_score=0.25,
                             embed_fn=_fake_embed,
                             search_fn=_make_search_fn([_hit(0.2499, idx=1)]))
        result = await r.retrieve(hotel_id=HOTEL, query="any")
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_MATCH
