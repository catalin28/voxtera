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
    "hotels":               list[dict],   # each: {hotel_id, score, payload,
                               #        evidence: {requirement -> chunk}}
      "missing_requirements": list[str],    # populated when reason == "partial_match_only"
      "reason":               str | None,
    }

`reason` is non-null whenever `count == 0`, drawn from:
    "empty_requirements", "no_region_scope", "partial_match_only",
    "no_match_above_threshold", "retriever_error".

Graceful degradation: if the strict intersection is empty, we drop the
weakest-supported requirement(s) — at most HALF of them — until either at
least one hotel survives ("partial_match_only" + `missing_requirements`)
or the drop budget is exhausted ("no_match_above_threshold"). A partial
that satisfies less than half the request is treated as no match.
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
        hotel_id: str | None = None,
        rerank: bool = True,
    ) -> dict[str, Any]:
        region = (region or "").strip()
        normalized = [r.strip() for r in (requirements or []) if r and r.strip()]
        normalized = normalized[: self._max_requirements]

        if not normalized:
            return self._empty(region, requirements, normalized, REASON_EMPTY_REQUIREMENTS)

        try:
            per_req = await self._fan_out(
                normalized, region, activity_tags, category_hint, hotel_id=hotel_id, rerank=rerank
            )
        except Exception as e:  # noqa: BLE001 — graceful degradation per contract
            logger.warning("CompoundAndDiscovery error for region={!r}: {}", region, e)
            return self._empty(region, requirements, normalized, REASON_ERROR)

        result = self._intersect(region, requirements, normalized, per_req)
        # Confirmation pass: the strict intersection above only looks at the
        # top-K hotels PER requirement, so a hotel can be dropped from a
        # requirement it actually satisfies (e.g. Moyo has a spa but isn't in the
        # top-K for "spa", which competes against every hotel). For each dropped
        # requirement, scope-check the SURVIVING hotels against their own chunks
        # — the same scoped query that answers "does Moyo have a spa?" — and
        # confirm where the evidence is genuinely there. This makes broad and
        # scoped agree instead of contradicting each other. Only runs on a
        # partial match, so the common (full-match / no-match) paths pay nothing.
        if (
            result.get("reason") == REASON_PARTIAL_MATCH
            and result.get("missing_requirements")
            and result.get("hotels")
        ):
            await self._confirm_missing(result, region, rerank=rerank)
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

    async def fetch_hotel_chunks(
        self,
        *,
        hotel_id: str,
        query: str,
        region: str = "",
        k: int = 6,
    ) -> list[dict[str, Any]]:
        """Delegate to BroadHotelDiscovery: top-`k` distinct chunks for one hotel.

        Used by the scoped path so a question about a KNOWN hotel is answered
        from several of its passages, not a single best-matching chunk.
        """
        return await self._discovery.fetch_hotel_chunks(
            hotel_id=hotel_id, query=query, region=region, k=k
        )

    # --- internals ---

    async def _fan_out(
        self,
        requirements: list[str],
        region: str,
        activity_tags: list[str] | None,
        category_hint: str | None,
        *,
        hotel_id: str | None = None,
        rerank: bool = True,
    ) -> list[dict[str, Any]]:
        tasks = [
            self._discovery.discover(
                region=region,
                query=req,
                activity_tags=activity_tags,
                category_hint=category_hint,
                hotel_id=hotel_id,
                rerank=rerank,
            )
            for req in requirements
        ]
        return await asyncio.gather(*tasks)

    async def _confirm_missing(self, result: dict[str, Any], region: str, *, rerank: bool) -> None:
        """Back-fill dropped requirements by scope-checking the surviving hotels.

        Mutates ``result`` in place: for each requirement in
        ``missing_requirements``, runs a scoped ``discover`` (hotel_id-filtered)
        against each surviving hotel — the same path used to answer a question
        about a specific hotel — and, where the hotel genuinely matches, attaches
        the evidence chunk to that hotel and removes the requirement from the
        global ``missing_requirements``. A requirement stays missing only if NO
        survivor confirms it. If everything is confirmed, the reason is cleared
        (the result is now a full match).
        """
        survivors: list[dict[str, Any]] = result.get("hotels") or []
        missing: list[str] = list(result.get("missing_requirements") or [])
        if not survivors or not missing:
            return

        still_missing: list[str] = []
        for req in missing:
            # Scoped check per survivor, in parallel. discover(hotel_id=…) applies
            # the same min_score / rerank gates as the broad search, so a
            # confirmation here means the same thing a scoped query would.
            tasks = [
                self._discovery.discover(
                    region=region, query=req, hotel_id=h["hotel_id"], rerank=rerank
                )
                for h in survivors
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            confirmed_any = False
            for hotel, res in zip(survivors, results, strict=False):
                if isinstance(res, BaseException) or not isinstance(res, dict):
                    continue
                hit = next(
                    (
                        hh
                        for hh in (res.get("hotels") or [])
                        if hh.get("hotel_id") == hotel["hotel_id"]
                    ),
                    None,
                )
                if hit is not None:
                    hotel.setdefault("evidence", {})[req] = hit.get("evidence_chunk")
                    confirmed_any = True
            if not confirmed_any:
                still_missing.append(req)

        result["missing_requirements"] = still_missing
        if not still_missing:
            # Every requirement is now backed by evidence on at least one hotel.
            result["reason"] = None

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
        # iteratively until an intersection survives — but never discard MORE
        # THAN HALF of what the caller asked for. Unbounded dropping degraded
        # "quiet + spa + kids club + sea view + antalya" all the way down to
        # the lone place token ("antalya"), returning hotels that matched
        # NOTHING the guest actually asked for (live defect D1/D5, 2026-06-07).
        # A partial answer that ignores most of the request is worse than an
        # honest no-match, which the pipeline renders fail-closed.
        max_drops = len(normalized) // 2
        kept_idx = list(range(len(normalized)))
        dropped: list[int] = []
        while len(kept_idx) > 1 and len(dropped) < max_drops:
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
            out.append(
                {
                    "hotel_id": hid,
                    "score": float(avg_score),
                    "payload": per_req_hits[0]["payload"],
                    "evidence": {
                        req: h["evidence_chunk"]
                        for req, h in zip(requirements, per_req_hits, strict=False)
                    },
                }
            )
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
