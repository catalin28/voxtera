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

import time
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
    RELATIVE_MARGIN,
    RERANK_MIN_SCORE,
    RERANK_RELATIVE_MARGIN,
    canonical_region,
)
from voxtera.call_center.reranker import RerankFn, is_rerank_enabled

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
        relative_margin: float = RELATIVE_MARGIN,
        overshoot_mult: int = DISCOVERY_OVERSHOOT_MULT,
        embed_fn: EmbedFn | None = None,
        search_fn: SearchFn | None = None,
        rerank_fn: RerankFn | None = None,
        rerank_min_score: float = RERANK_MIN_SCORE,
        rerank_relative_margin: float = RERANK_RELATIVE_MARGIN,
    ) -> None:
        self._session = session
        self._collection = collection
        self._max_hotels = max_hotels
        self._min_score = min_score
        self._relative_margin = relative_margin
        self._overshoot_mult = overshoot_mult
        self._embed_fn = embed_fn
        self._search_fn = search_fn
        # Rerank is opt-in via dependency injection: tests pass rerank_fn=None
        # (default) and never load the model. Production wires a real one in.
        # The env kill-switch only affects callers that ask for it via
        # `rerank_fn` being non-None but env-disabled.
        self._rerank_fn = rerank_fn
        self._rerank_min_score = rerank_min_score
        self._rerank_relative_margin = rerank_relative_margin

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

        timings: dict[str, float] = {}
        try:
            t0 = time.perf_counter()
            vector = await self._embed(normalized_query)
            timings["embed_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
            body = self._build_search_body(vector, region, activity_tags, category_hint)
            t0 = time.perf_counter()
            hits = await self._search(body)
            timings["qdrant_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        except Exception as e:  # noqa: BLE001 — graceful degradation per contract
            logger.warning("BroadHotelDiscovery error for region={!r}: {}", region, e)
            empty = self._empty(region, query, normalized_query, REASON_ERROR)
            empty["timings"] = timings
            return empty

        t0 = time.perf_counter()
        hits, reranked = await self._maybe_rerank(normalized_query, hits)
        timings["rerank_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        result = self._finalize(region, query, normalized_query, hits, reranked=reranked)
        result["timings"] = timings
        return result

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
            {"key": "region", "match": {"value": canonical_region(region)}},
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

    async def _maybe_rerank(
        self, query: str, hits: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Replace each hit's `score` with a cross-encoder rerank score.

        Returns ``(hits, reranked)`` where ``reranked`` is True iff rerank
        actually ran (so `_finalize` can pick the right thresholds).
        Falls back to the unmodified hits — and the cosine-scale thresholds
        — on any rerank failure.
        """
        if self._rerank_fn is None or not hits or not is_rerank_enabled():
            return hits, False
        passages = [
            (h.get("payload") or {}).get("text") or ""
            for h in hits
        ]
        try:
            result = self._rerank_fn(query, passages)
            scores = await result if hasattr(result, "__await__") else result
        except Exception as e:  # noqa: BLE001 — never fail retrieval over rerank
            logger.warning("Reranker failed (falling back to cosine scores): {}", e)
            return hits, False
        if len(scores) != len(hits):
            logger.warning(
                "Reranker returned {} scores for {} hits; ignoring rerank",
                len(scores), len(hits),
            )
            return hits, False
        rescored: list[dict[str, Any]] = []
        for hit, s in zip(hits, scores):
            new_hit = dict(hit)
            new_hit["_cosine"] = float(hit.get("score", 0.0))
            new_hit["score"] = float(s)
            rescored.append(new_hit)
        rescored.sort(key=lambda h: h["score"], reverse=True)
        return rescored, True

    def _finalize(
        self,
        region: str,
        query: str,
        normalized_query: str,
        hits: list[dict[str, Any]],
        reranked: bool = False,
    ) -> dict[str, Any]:
        # Pick the active thresholds: rerank scale ([0,1] separated) when
        # rerank ran; cosine scale (compressed, junk-floor only) otherwise.
        min_score = self._rerank_min_score if reranked else self._min_score
        relative_margin = (
            self._rerank_relative_margin if reranked else self._relative_margin
        )

        # Aggregate by hotel_id, keep the highest-scoring chunk per hotel.
        best_per_hotel: dict[str, dict[str, Any]] = {}
        best_below_threshold = 0.0
        for h in hits:
            payload = h.get("payload", {}) or {}
            hotel_id = payload.get("hotel_id")
            if not hotel_id:
                continue
            score = float(h.get("score", 0.0))
            if score < min_score:
                if score > best_below_threshold:
                    best_below_threshold = score
                continue
            existing = best_per_hotel.get(hotel_id)
            if existing is None or score > existing["_score"]:
                best_per_hotel[hotel_id] = {"_score": score, "_hit": h}

        ordered = sorted(
            best_per_hotel.values(), key=lambda x: x["_score"], reverse=True
        )

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

        # Relative-margin filter: keep only hotels whose best-chunk score is
        # within `relative_margin` of the top hotel's score. Trim-only; top
        # hotel always survives.
        top_score = ordered[0]["_score"]
        within_margin = [
            e for e in ordered
            if e["_score"] >= top_score - relative_margin
        ]
        capped = within_margin[: self._max_hotels]

        hotels = [self._hotel_from_entry(e) for e in capped]
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
