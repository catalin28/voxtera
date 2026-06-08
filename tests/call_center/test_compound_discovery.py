"""Unit tests for CompoundAndDiscovery (Phase 2c) and relative-margin filtering."""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.compound import (
    REASON_EMPTY_REQUIREMENTS,
    REASON_ERROR,
    REASON_NO_MATCH,
    REASON_NO_REGION_SCOPE,
    REASON_PARTIAL_MATCH,
    CompoundAndDiscovery,
)
from voxtera.call_center.discovery import BroadHotelDiscovery
from voxtera.call_center.kb_retriever import HotelKBRetriever

REGION = "antalya"


def _hit(
    score: float,
    *,
    hotel_id: str,
    region: str = REGION,
    category: str = "wellness",
    idx: int = 0,
    activity_tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": idx,
        "score": score,
        "payload": {
            "chunk_id": f"{hotel_id}::{category}::{idx}",
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


def _scripted_discovery(hits_by_query: dict[str, list[dict[str, Any]]]) -> BroadHotelDiscovery:
    """A BroadHotelDiscovery whose search_fn returns hits keyed by the most recent query.

    Captures the requirement string via the embed_fn (which sees the raw text).
    """
    state: dict[str, str] = {"q": ""}

    def capture_embed(text: str) -> list[float]:
        state["q"] = text
        return [0.1] * 8

    async def search(_v, _f, _l):
        return hits_by_query.get(state["q"], [])

    return BroadHotelDiscovery(
        min_score=0.25,
        relative_margin=1.0,  # margin off for compound fixtures
        embed_fn=capture_embed,
        search_fn=search,
    )


# ---------------- Relative-margin filtering ----------------


class TestRelativeMargin:
    async def test_kb_retriever_drops_tail_outside_margin(self) -> None:
        # Top 0.90, others 0.86 (within margin) and 0.70 (outside margin)
        hits = [
            {
                "id": 1,
                "score": 0.90,
                "payload": {
                    "chunk_id": "a",
                    "hotel_id": "h1",
                    "category": "amenities",
                    "text": "t1",
                    "text_en": "t1",
                    "activity_tags": [],
                },
            },
            {
                "id": 2,
                "score": 0.86,
                "payload": {
                    "chunk_id": "b",
                    "hotel_id": "h1",
                    "category": "amenities",
                    "text": "t2",
                    "text_en": "t2",
                    "activity_tags": [],
                },
            },
            {
                "id": 3,
                "score": 0.70,
                "payload": {
                    "chunk_id": "c",
                    "hotel_id": "h1",
                    "category": "amenities",
                    "text": "t3",
                    "text_en": "t3",
                    "activity_tags": [],
                },
            },
        ]

        async def search(_v, _f, _l):
            return hits

        r = HotelKBRetriever(
            top_k=5, min_score=0.25, relative_margin=0.05, embed_fn=_fake_embed, search_fn=search
        )
        result = await r.retrieve(hotel_id="h1", query="spa")
        assert result["count"] == 2
        kept = [c["score"] for c in result["chunks"]]
        assert kept == [pytest.approx(0.90), pytest.approx(0.86)]

    async def test_kb_retriever_lone_top_chunk_survives(self) -> None:
        # Only one chunk; it must always survive the margin.
        hits = [
            {
                "id": 1,
                "score": 0.80,
                "payload": {
                    "chunk_id": "a",
                    "hotel_id": "h1",
                    "category": "x",
                    "text": "t",
                    "text_en": "t",
                    "activity_tags": [],
                },
            }
        ]

        async def search(_v, _f, _l):
            return hits

        r = HotelKBRetriever(
            top_k=3, min_score=0.25, relative_margin=0.05, embed_fn=_fake_embed, search_fn=search
        )
        result = await r.retrieve(hotel_id="h1", query="x")
        assert result["count"] == 1
        assert result["reason"] is None

    async def test_broad_discovery_drops_hotels_outside_margin(self) -> None:
        hits = [
            _hit(0.90, hotel_id="h_top", idx=1),
            _hit(0.87, hotel_id="h_mid", idx=2),
            _hit(0.70, hotel_id="h_far", idx=3),
        ]

        async def search(_v, _f, _l):
            return hits

        d = BroadHotelDiscovery(
            max_hotels=5,
            min_score=0.25,
            relative_margin=0.05,
            embed_fn=_fake_embed,
            search_fn=search,
        )
        result = await d.discover(region=REGION, query="spa")
        assert [h["hotel_id"] for h in result["hotels"]] == ["h_top", "h_mid"]


# ---------------- Compound-AND core ----------------


class TestCompoundCore:
    async def test_empty_region_searches_all_regions(self) -> None:
        # Empty region == "all regions": compound fans out across the whole
        # collection instead of short-circuiting with no_region_scope.
        c = CompoundAndDiscovery(discovery=_scripted_discovery({}))
        result = await c.discover(region="  ", requirements=["spa"])
        assert result["reason"] != REASON_NO_REGION_SCOPE

    async def test_empty_requirements_short_circuits(self) -> None:
        c = CompoundAndDiscovery(discovery=_scripted_discovery({}))
        result = await c.discover(region=REGION, requirements=["   ", ""])
        assert result["count"] == 0
        assert result["reason"] == REASON_EMPTY_REQUIREMENTS

    async def test_strict_intersection_happy_path(self) -> None:
        # Both requirements return overlapping hotel "rixos" — must intersect cleanly.
        discovery = _scripted_discovery(
            {
                "spa": [
                    _hit(0.85, hotel_id="rixos", idx=1, category="wellness"),
                    _hit(0.80, hotel_id="cornelia", idx=2, category="wellness"),
                ],
                "scuba diving": [
                    _hit(0.82, hotel_id="rixos", idx=3, category="activities"),
                    _hit(0.78, hotel_id="maxx", idx=4, category="activities"),
                ],
            }
        )
        c = CompoundAndDiscovery(discovery=discovery)
        result = await c.discover(region=REGION, requirements=["spa", "scuba diving"])
        assert result["reason"] is None
        assert result["count"] == 1
        h = result["hotels"][0]
        assert h["hotel_id"] == "rixos"
        # Per-requirement evidence both present.
        assert set(h["evidence"]) == {"spa", "scuba diving"}
        assert h["evidence"]["spa"]["category"] == "wellness"
        assert h["evidence"]["scuba diving"]["category"] == "activities"
        # Score is average of per-requirement scores.
        assert h["score"] == pytest.approx((0.85 + 0.82) / 2)
        assert result["missing_requirements"] == []

    async def test_partial_match_drops_smallest_requirement(self) -> None:
        # "spa" matches many hotels; "kids_club" matches a unique one with no overlap;
        # intersection is empty → drop "kids_club" → "spa" alone survives.
        discovery = _scripted_discovery(
            {
                "spa": [
                    _hit(0.85, hotel_id="rixos", idx=1),
                    _hit(0.80, hotel_id="cornelia", idx=2),
                ],
                "kids_club": [_hit(0.75, hotel_id="orphan_hotel", idx=3, category="children")],
            }
        )
        c = CompoundAndDiscovery(discovery=discovery)
        result = await c.discover(region=REGION, requirements=["spa", "kids_club"])
        assert result["reason"] == REASON_PARTIAL_MATCH
        assert result["count"] >= 1
        assert "kids_club" in result["missing_requirements"]
        assert "spa" not in result["missing_requirements"]
        for h in result["hotels"]:
            assert "spa" in h["evidence"]
            assert "kids_club" not in h["evidence"]

    async def test_all_empty_returns_no_match(self) -> None:
        discovery = _scripted_discovery({})  # every requirement returns []
        c = CompoundAndDiscovery(discovery=discovery)
        result = await c.discover(region=REGION, requirements=["a", "b", "c"])
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_MATCH
        assert set(result["missing_requirements"]) == {"a", "b", "c"}

    async def test_single_requirement_passes_through(self) -> None:
        discovery = _scripted_discovery(
            {
                "spa": [_hit(0.85, hotel_id="rixos", idx=1)],
            }
        )
        c = CompoundAndDiscovery(discovery=discovery)
        result = await c.discover(region=REGION, requirements=["spa"])
        assert result["reason"] is None
        assert result["count"] == 1
        assert result["hotels"][0]["hotel_id"] == "rixos"

    async def test_max_requirements_caps_input(self) -> None:
        # Six requirements supplied; only the first 5 are processed.
        discovery = _scripted_discovery(
            {f"r{i}": [_hit(0.85, hotel_id="rixos", idx=i)] for i in range(6)}
        )
        c = CompoundAndDiscovery(discovery=discovery, max_requirements=5)
        result = await c.discover(region=REGION, requirements=[f"r{i}" for i in range(6)])
        # Strict intersection over r0..r4 only → rixos passes.
        assert result["reason"] is None
        assert result["count"] == 1
        assert len(result["normalized_requirements"]) == 5
        assert "r5" not in result["hotels"][0]["evidence"]

    async def test_max_hotels_caps_intersection(self) -> None:
        # Both requirements yield 4 shared hotels; max_hotels=2 must trim.
        ids = ["h1", "h2", "h3", "h4"]
        scores = {"h1": 0.90, "h2": 0.88, "h3": 0.85, "h4": 0.82}
        discovery = _scripted_discovery(
            {
                "spa": [_hit(scores[i], hotel_id=i, idx=k) for k, i in enumerate(ids)],
                "diving": [_hit(scores[i], hotel_id=i, idx=k + 10) for k, i in enumerate(ids)],
            }
        )
        c = CompoundAndDiscovery(discovery=discovery, max_hotels=2)
        result = await c.discover(region=REGION, requirements=["spa", "diving"])
        assert result["count"] == 2
        assert [h["hotel_id"] for h in result["hotels"]] == ["h1", "h2"]

    async def test_fan_out_failure_returns_retriever_error(self) -> None:
        async def boom(_v, _f, _l):
            raise RuntimeError("qdrant down")

        discovery = BroadHotelDiscovery(
            min_score=0.25,
            embed_fn=_fake_embed,
            search_fn=boom,
        )
        # Force the underlying retriever_error → BroadHotelDiscovery returns reason
        # "retriever_error" with count 0; intersection then has empty maps and falls
        # through the partial-match loop to NO_MATCH.
        c = CompoundAndDiscovery(discovery=discovery)
        result = await c.discover(region=REGION, requirements=["spa", "diving"])
        assert result["count"] == 0
        assert result["reason"] == REASON_NO_MATCH

    async def test_compound_error_path(self) -> None:
        class Boom(BroadHotelDiscovery):
            async def discover(self, **_):  # type: ignore[override]
                raise RuntimeError("disco down")

        c = CompoundAndDiscovery(discovery=Boom())
        result = await c.discover(region=REGION, requirements=["spa"])
        assert result["count"] == 0
        assert result["reason"] == REASON_ERROR

    async def test_response_shape_matches_contract(self) -> None:
        discovery = _scripted_discovery(
            {
                "spa": [_hit(0.85, hotel_id="rixos", idx=1)],
            }
        )
        c = CompoundAndDiscovery(discovery=discovery)
        result = await c.discover(region=REGION, requirements=["spa"])
        assert set(result) == {
            "region",
            "requirements",
            "normalized_requirements",
            "top_score",
            "count",
            "hotels",
            "missing_requirements",
            "reason",
            "timings",
        }
        h = result["hotels"][0]
        assert set(h) == {"hotel_id", "score", "payload", "evidence"}
