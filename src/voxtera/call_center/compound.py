"""Compound-AND Discovery — multi-requirement hotel intersection (Phase 2c).

Given a `region` and a list of free-form `requirements` (e.g. ["spa for
my wife", "scuba diving for me"]), runs N parallel `BroadHotelDiscovery`
searches (one per requirement) and intersects them at the hotel level.

A hotel passes only if every requirement contributed at least one
supporting chunk for it (each survives `min_score` and `relative_margin`
at the requirement level — Phase 2b guarantees that).

Decision contract:

    {
      "region":               str,
      "requirements":         list[str],
      "normalized_requirements": list[str],
      "top_score":            float,   # mean of per-requirement top scores for the best hotel
      "count":                int,
      "hotels":               list[dict],   # each: {hotel_id, score, payload, evidence: {requirement -> chunk}}
      "missing_requirements": list[str],    # populated when reason == "partial_match_only"
      "reason":               str | None,
    }

`reason` is non-null whenever `count == 0`, drawn from:
    "empty_requirements", "no_region_scope", "partial_match_only",
    "no_match_above_threshold", "retriever_error".

Graceful degradation: if the strict intersection is empty, we drop the
weakest-supported requirement(s) until either at least one hotel
survives ("partial_match_only" + `missing_requirements`) or every
requirement is exhausted ("no_match_above_threshold").
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from loguru import logger

from voxtera.call_center.discovery import BroadHotelDiscovery
from voxtera.call_center.kb_config import (
    DEFAULT_MAX_HOTELS,
    DEFAULT_MAX_REQUIREMENTS,
)

REASON_EMPTY_REQUIREMENTS = "empty_requirements"
REASON_NO_REGION_SCOPE = "no_region_scope"
REASON_PARTIAL_MATCH = "partial_match_only"
REASON_NO_MATCH = "no_match_above_threshold"
REASON_ERROR = "retriever_error"


class CompoundAndDiscovery:
    """Phase 2c — N-requirement intersection over BroadHotelDiscovery."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        discovery: BroadHotelDiscovery | None = None,
        max_hotels: int = DEFAULT_MAX_HOTELS,
        max_requirements: int = DEFAULT_MAX_REQUIREMENTS,
    ) -> None:
        self._session = session
        self._discovery = discovery or BroadHotelDiscovery(session=session)
        self._max_hotels = max_hotels
        self._max_requirements = max_requirements

    async def discover(
        self,
        *,
        region: str,
        requirements: list[str],
        activity_tags: list[str] | None = None,
        category_hint: str | None = None,
    ) -> dict[str, Any]:
        region = (region or "").strip()
        normalized = [r.strip() for r in (requirements or []) if r and r.strip()]
        normalized = normalized[: self._max_requirements]

        if not region:
            return self._empty(region, requirements, normalized, REASON_NO_REGION_SCOPE)
        if not normalized:
            return self._empty(region, requirements, normalized, REASON_EMPTY_REQUIREMENTS)

        try:
            per_req = await self._fan_out(normalized, region, activity_tags, category_hint)
        except Exception as e:  # noqa: BLE001 — graceful degradation per contract
            logger.warning("CompoundAndDiscovery error for region={!r}: {}", region, e)
            return self._empty(region, requirements, normalized, REASON_ERROR)

        result = self._intersect(region, requirements, normalized, per_req)
        # Aggregate per-requirement timings: embed/qdrant run in parallel so
        # the wall-time is dominated by the slowest leg.
        embed = [t.get("timings", {}).get("embed_ms", 0.0) for t in per_req]
        qdrant = [t.get("timings", {}).get("qdrant_ms", 0.0) for t in per_req]
        result["timings"] = {
            "fan_out": len(per_req),
            "embed_ms_max": round(max(embed) if embed else 0.0, 1),
            "qdrant_ms_max": round(max(qdrant) if qdrant else 0.0, 1),
        }
        return result

    # --- internals ---

    async def _fan_out(
        self,
        requirements: list[str],
        region: str,
        activity_tags: list[str] | None,
        category_hint: str | None,
    ) -> list[dict[str, Any]]:
        tasks = [
            self._discovery.discover(
                region=region,
                query=req,
                activity_tags=activity_tags,
                category_hint=category_hint,
            )
            for req in requirements
        ]
        return await asyncio.gather(*tasks)

    def _intersect(
        self,
        region: str,
        requirements: list[str],
        normalized: list[str],
        per_req: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Build per-requirement hotel_id -> hotel dict maps.
        maps: list[dict[str, dict[str, Any]]] = [
            {h["hotel_id"]: h for h in r.get("hotels", [])} for r in per_req
        ]

        # Strict intersection across all requirements.
        strict_ids = set(maps[0])
        for m in maps[1:]:
            strict_ids &= set(m)

        if strict_ids:
            hotels = self._build_hotels(strict_ids, maps, normalized)
            return self._payload(region, requirements, normalized, hotels, [], None)

        # Graceful degradation: drop the requirement with the smallest hotel set
        # iteratively until an intersection survives or we run out.
        kept_idx = list(range(len(normalized)))
        dropped: list[int] = []
        while len(kept_idx) > 1:
            # Drop the requirement contributing the fewest hotels in the strict pool.
            kept_idx.sort(key=lambda i: len(maps[i]))
            dropped.append(kept_idx.pop(0))
            ids = set(maps[kept_idx[0]])
            for i in kept_idx[1:]:
                ids &= set(maps[i])
            if ids:
                kept_normalized = [normalized[i] for i in kept_idx]
                hotels = self._build_hotels(ids, [maps[i] for i in kept_idx], kept_normalized)
                missing = [normalized[i] for i in dropped]
                return self._payload(
                    region, requirements, normalized, hotels, missing, REASON_PARTIAL_MATCH
                )

        return self._payload(region, requirements, normalized, [], normalized, REASON_NO_MATCH)

    def _build_hotels(
        self,
        ids: set[str],
        maps: list[dict[str, dict[str, Any]]],
        requirements: list[str],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for hid in ids:
            per_req_hits = [m[hid] for m in maps]
            avg_score = sum(h["score"] for h in per_req_hits) / len(per_req_hits)
            out.append({
                "hotel_id": hid,
                "score": float(avg_score),
                "payload": per_req_hits[0]["payload"],
                "evidence": {
                    req: h["evidence_chunk"]
                    for req, h in zip(requirements, per_req_hits)
                },
            })
        out.sort(key=lambda h: h["score"], reverse=True)
        return out[: self._max_hotels]

    def _payload(
        self,
        region: str,
        requirements: list[str],
        normalized: list[str],
        hotels: list[dict[str, Any]],
        missing: list[str],
        reason: str | None,
    ) -> dict[str, Any]:
        return {
            "region": region,
            "requirements": list(requirements or []),
            "normalized_requirements": normalized,
            "top_score": float(hotels[0]["score"]) if hotels else 0.0,
            "count": len(hotels),
            "hotels": hotels,
            "missing_requirements": missing,
            "reason": reason,
        }

    @staticmethod
    def _empty(
        region: str,
        requirements: list[str],
        normalized: list[str],
        reason: str,
    ) -> dict[str, Any]:
        return {
            "region": region,
            "requirements": list(requirements or []),
            "normalized_requirements": normalized,
            "top_score": 0.0,
            "count": 0,
            "hotels": [],
            "missing_requirements": [],
            "reason": reason,
        }
