"""Tests for Phase 1 hotel resolver decision behavior."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from voxtera.call_center.resolver import HotelResolver


def _hit(hotel_id: str, name: str, score: float) -> dict[str, Any]:
    return {
        "_score": score,
        "_source": {
            "hotel_id": hotel_id,
            "name": name,
        },
    }


class TestHotelResolverCore:
    @pytest.mark.asyncio
    async def test_empty_mention_returns_no_match_without_search(self) -> None:
        search_fn = AsyncMock(return_value=[])
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("   ")

        assert result["decision"] == "no_match"
        assert result["reason"] == "empty_mention"
        assert result["top_score"] == 0.0
        search_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resolve_when_score_above_threshold(self) -> None:
        search_fn = AsyncMock(
            return_value=[_hit("rixos_premium_belek", "Rixos Premium Belek", 0.91)]
        )
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("Rixos Belek")

        assert result["decision"] == "auto_resolve"
        assert result["hotel_id"] == "rixos_premium_belek"
        assert result["top_score"] == pytest.approx(0.91)
        assert result["candidates"] == []

    @pytest.mark.asyncio
    async def test_clarification_returns_top_three_sorted_candidates(self) -> None:
        search_fn = AsyncMock(
            return_value=[
                _hit("hilton_belek", "Hilton Belek", 0.72),
                _hit("hilton_lara", "Hilton Lara", 0.78),
                _hit("hilton_bomonti_istanbul", "Hilton Bomonti Istanbul", 0.61),
                _hit("hilton_ankara", "Hilton Ankara", 0.60),
            ]
        )
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("Hilton Antalya")

        assert result["decision"] == "needs_clarification"
        assert result["hotel_id"] is None
        assert result["reason"] == "score_in_clarification_band"
        assert len(result["candidates"]) == 3
        assert [c["hotel_id"] for c in result["candidates"]] == [
            "hilton_lara",
            "hilton_belek",
            "hilton_bomonti_istanbul",
        ]

    @pytest.mark.asyncio
    async def test_no_match_when_score_below_threshold(self) -> None:
        search_fn = AsyncMock(return_value=[_hit("foo_hotel", "Foo Hotel", 0.42)])
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("unknown hotel")

        assert result["decision"] == "no_match"
        assert result["reason"] == "score_below_min_threshold"
        assert result["hotel_id"] is None

    @pytest.mark.asyncio
    async def test_dominant_top_auto_resolves_at_modest_score(self) -> None:
        # "Casa Dell Arte" -> ES ~0.48 absolute, but 2x the runner-up. A clear
        # winner must auto-resolve via relative dominance, not be rejected by the
        # absolute threshold (the real Casa Dell Arte bug).
        search_fn = AsyncMock(
            return_value=[
                _hit("casa_dell_arte_residance", "Casa Dell Arte Residance", 0.48),
                _hit("casa_dellarte_arts", "Casa dell'Arte Hotel of Arts", 0.21),
                _hit("casa_tuana", "Casa Tuana Alacati", 0.15),
            ]
        )
        resolver = HotelResolver(search_fn=search_fn)
        result = await resolver.resolve("Casa Dell Arte")
        assert result["decision"] == "auto_resolve"
        assert result["hotel_id"] == "casa_dell_arte_residance"
        assert result["reason"] == "dominant_top_match"

    @pytest.mark.asyncio
    async def test_close_candidates_do_not_dominate(self) -> None:
        # Two near-tied candidates (no dominance) stay in the clarify band rather
        # than auto-resolving the wrong one.
        search_fn = AsyncMock(
            return_value=[
                _hit("hilton_lara", "Hilton Lara", 0.60),
                _hit("hilton_belek", "Hilton Belek", 0.58),
            ]
        )
        resolver = HotelResolver(search_fn=search_fn)
        result = await resolver.resolve("Hilton")
        assert result["decision"] == "needs_clarification"

    @pytest.mark.asyncio
    async def test_no_candidates_returns_no_match(self) -> None:
        search_fn = AsyncMock(return_value=[])
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("something")

        assert result["decision"] == "no_match"
        assert result["reason"] == "no_candidates"

    @pytest.mark.asyncio
    async def test_normalizes_apostrophe_and_whitespace(self) -> None:
        search_fn = AsyncMock(return_value=[_hit("kaya_palazzo_belek", "Kaya Palazzo Belek", 0.90)])
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("  Kaya\u2019da   ")

        assert result["normalized_mention"] == "kaya'da"
        search_fn.assert_awaited_once_with("kaya'da", 10)

    @pytest.mark.asyncio
    async def test_search_error_degrades_safely(self) -> None:
        search_fn = AsyncMock(side_effect=RuntimeError("es down"))
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("Rixos")

        assert result["decision"] == "no_match"
        assert result["reason"] == "resolver_error"


class TestThresholdBoundaries:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("score", "expected"),
        [
            (0.85, "auto_resolve"),
            (0.84, "needs_clarification"),
            (0.55, "needs_clarification"),
            (0.54, "no_match"),
        ],
    )
    async def test_decision_boundaries(self, score: float, expected: str) -> None:
        search_fn = AsyncMock(
            return_value=[_hit("rixos_premium_belek", "Rixos Premium Belek", score)]
        )
        resolver = HotelResolver(search_fn=search_fn)

        result = await resolver.resolve("Rixos")

        assert result["decision"] == expected
