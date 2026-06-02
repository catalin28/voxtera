"""Broad Hotel Discovery — cross-hotel semantic retrieval (Phase 2b).

Given a `region` and a free-form intent query (no `hotel_id`), returns
the top-N **hotels** sorted by best-supporting-chunk score, each carrying
one evidence chunk. Optional `activity_tags` and `category_hint` narrow
the search.

Decision contract (see docs/call-center/phase2b-user-story.md §3):

    {
      "region":           str,
      "query":            str,
      "normalized_query": str,
      "top_score":        float,
      "count":            int,
      "hotels":           list[dict],   # each: {hotel_id, score, evidence_chunk, payload}
      "reason":           str | None,
    }

`reason` is non-null whenever `count == 0`, drawn from:
    "empty_query", "no_region_scope", "no_match_above_threshold",
    "retriever_error".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from loguru import logger

from voxtera.call_center.clients import qdrant_request
from voxtera.call_center.embeddings import embed_query
from voxtera.call_center.kb_config import (
    DEFAULT_MAX_HOTELS,
    DEFAULT_MIN_SCORE,
    DISCOVERY_OVERSHOOT_MULT,
    QDRANT_COLLECTION,
)

EmbedFn = Callable[[str], Awaitable[list[float]] | list[float]]
SearchFn = Callable[[list[float], dict, int], Awaitable[list[dict]]]

REASON_EMPTY_QUERY = "empty_query"
REASON_NO_REGION_SCOPE = "no_region_scope"
REASON_NO_MATCH = "no_match_above_threshold"
REASON_ERROR = "retriever_error"


class BroadHotelDiscovery:
    """Phase 2b — cross-hotel semantic discovery filtered by region."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        collection: str = QDRANT_COLLECTION,
        max_hotels: int = DEFAULT_MAX_HOTELS,
        min_score: float = DEFAULT_MIN_SCORE,
        overshoot_mult: int = DISCOVERY_OVERSHOOT_MULT,
        embed_fn: EmbedFn | None = None,
        search_fn: SearchFn | None = None,
    ) -> None:
        self._session = session
        self._collection = collection
        self._max_hotels = max_hotels
        self._min_score = min_score
        self._overshoot_mult = overshoot_mult
        self._embed_fn = embed_fn
        self._search_fn = search_fn

    async def discover(
        self,
        *,
        region: str,
        query: str,
        activity_tags: list[str] | None = None,
        category_hint: str | None = None,
    ) -> dict[str, Any]:
        region = (region or "").strip()
        normalized_query = (query or "").strip()

        if not region:
            return self._empty(region, query, normalized_query, REASON_NO_REGION_SCOPE)
        if not normalized_query:
            return self._empty(region, query, normalized_query, REASON_EMPTY_QUERY)

        try:
            vector = await self._embed(normalized_query)
            body = self._build_search_body(vector, region, activity_tags, category_hint)
            hits = await self._search(body)
        except Exception as e:  # noqa: BLE001 — graceful degradation per contract
            logger.warning("BroadHotelDiscovery error for region={!r}: {}", region, e)
            return self._empty(region, query, normalized_query, REASON_ERROR)

        return self._finalize(region, query, normalized_query, hits)

    # --- internals ---

    async def _embed(self, text: str) -> list[float]:
        if self._embed_fn is not None:
            result = self._embed_fn(text)
            if hasattr(result, "__await__"):
                return await result  # type: ignore[return-value]
            return result  # type: ignore[return-value]
        return embed_query(text)

    def _build_search_body(
        self,
        vector: list[float],
        region: str,
        activity_tags: list[str] | None,
        category_hint: str | None,
    ) -> dict[str, Any]:
        must: list[dict[str, Any]] = [
            {"key": "region", "match": {"value": region}},
        ]
        if activity_tags:
            must.append({"key": "activity_tags", "match": {"any": list(activity_tags)}})
        if category_hint:
            must.append({"key": "category", "match": {"any": [category_hint, "overview"]}})
        return {
            "vector": vector,
            "limit": self._max_hotels * self._overshoot_mult,
            "with_payload": True,
            "filter": {"must": must},
        }

    async def _search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if self._search_fn is not None:
            return await self._search_fn(body["vector"], body["filter"], body["limit"])
        if self._session is None:
            raise RuntimeError("BroadHotelDiscovery needs either a session or a search_fn")
        resp = await qdrant_request(
            self._session,
            "POST",
            f"/collections/{self._collection}/points/search",
            json=body,
        )
        return resp.get("result", []) or []

    def _finalize(
        self,
        region: str,
        query: str,
        normalized_query: str,
        hits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Aggregate by hotel_id, keep the highest-scoring chunk per hotel.
        best_per_hotel: dict[str, dict[str, Any]] = {}
        best_below_threshold = 0.0
        for h in hits:
            payload = h.get("payload", {}) or {}
            hotel_id = payload.get("hotel_id")
            if not hotel_id:
                continue
            score = float(h.get("score", 0.0))
            if score < self._min_score:
                if score > best_below_threshold:
                    best_below_threshold = score
                continue
            existing = best_per_hotel.get(hotel_id)
            if existing is None or score > existing["_score"]:
                best_per_hotel[hotel_id] = {"_score": score, "_hit": h}

        ordered = sorted(
            best_per_hotel.values(), key=lambda x: x["_score"], reverse=True
        )[: self._max_hotels]

        if not ordered:
            return {
                "region": region,
                "query": query,
                "normalized_query": normalized_query,
                "top_score": float(best_below_threshold),
                "count": 0,
                "hotels": [],
                "reason": REASON_NO_MATCH,
            }

        hotels = [self._hotel_from_entry(e) for e in ordered]
        return {
            "region": region,
            "query": query,
            "normalized_query": normalized_query,
            "top_score": float(hotels[0]["score"]),
            "count": len(hotels),
            "hotels": hotels,
            "reason": None,
        }

    @staticmethod
    def _hotel_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
        hit = entry["_hit"]
        payload = hit.get("payload", {}) or {}
        return {
            "hotel_id": payload.get("hotel_id", ""),
            "score": float(hit.get("score", 0.0)),
            "evidence_chunk": {
                "chunk_id": payload.get("chunk_id", str(hit.get("id", ""))),
                "category": payload.get("category", ""),
                "text": payload.get("text", ""),
                "text_en": payload.get("text_en", ""),
            },
            "payload": {
                "region": payload.get("region", ""),
                "country": payload.get("country", ""),
                "district": payload.get("district", ""),
                "price_tier": payload.get("price_tier", ""),
                "activity_tags": payload.get("activity_tags", []),
                "hotel_name": payload.get("hotel_name", ""),
            },
        }

    @staticmethod
    def _empty(
        region: str, query: str, normalized_query: str, reason: str
    ) -> dict[str, Any]:
        return {
            "region": region,
            "query": query,
            "normalized_query": normalized_query,
            "top_score": 0.0,
            "count": 0,
            "hotels": [],
            "reason": reason,
        }
