"""Scoped Hotel Knowledge Base retriever (Phase 2a).

Given a resolved `hotel_id` and a user query, returns the top-K most
relevant KB chunks for that single hotel from the Qdrant `hotel_kb`
collection, with zero cross-hotel leakage (enforced by a Qdrant
`must` filter on `hotel_id`).

Decision contract (see docs/call-center/phase2a-user-story.md §4):

    {
      "hotel_id":         str,
      "query":            str,
      "normalized_query": str,
      "top_score":        float,
      "count":            int,
      "chunks":           list[dict],
      "reason":           str | None,
    }

`reason` is non-null whenever `count == 0`, drawn from:
    "empty_query", "no_hotel_scope", "no_match_above_threshold",
    "retriever_error".

Both the embedder and the Qdrant search call are injectable so unit
tests can run without loading the e5-large model or hitting a live
Qdrant.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from loguru import logger

from voxtera.call_center.clients import qdrant_request
from voxtera.call_center.embeddings import embed_query
from voxtera.call_center.kb_config import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_K,
    QDRANT_COLLECTION,
)

EmbedFn = Callable[[str], Awaitable[list[float]] | list[float]]
SearchFn = Callable[[list[float], dict, int], Awaitable[list[dict]]]

# Enumerated reason strings
REASON_EMPTY_QUERY = "empty_query"
REASON_NO_HOTEL_SCOPE = "no_hotel_scope"
REASON_NO_MATCH = "no_match_above_threshold"
REASON_ERROR = "retriever_error"


class HotelKBRetriever:
    """Phase 2a — scoped retrieval over `hotel_kb` filtered by `hotel_id`."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        collection: str = QDRANT_COLLECTION,
        top_k: int = DEFAULT_TOP_K,
        min_score: float = DEFAULT_MIN_SCORE,
        embed_fn: EmbedFn | None = None,
        search_fn: SearchFn | None = None,
    ) -> None:
        self._session = session
        self._collection = collection
        self._top_k = top_k
        self._min_score = min_score
        self._embed_fn = embed_fn
        self._search_fn = search_fn

    async def retrieve(
        self,
        *,
        hotel_id: str,
        query: str,
        category_hint: str | None = None,
    ) -> dict[str, Any]:
        hotel_id = (hotel_id or "").strip()
        normalized_query = (query or "").strip()

        if not hotel_id:
            return self._empty(hotel_id, query, normalized_query, REASON_NO_HOTEL_SCOPE)
        if not normalized_query:
            return self._empty(hotel_id, query, normalized_query, REASON_EMPTY_QUERY)

        try:
            vector = await self._embed(normalized_query)
            search_body = self._build_search_body(vector, hotel_id, category_hint)
            hits = await self._search(search_body)
        except Exception as e:  # noqa: BLE001 — graceful degradation per contract
            logger.warning("HotelKBRetriever error for hotel_id={!r}: {}", hotel_id, e)
            return self._empty(hotel_id, query, normalized_query, REASON_ERROR)

        return self._finalize(hotel_id, query, normalized_query, hits)

    # --- internals ---

    async def _embed(self, text: str) -> list[float]:
        if self._embed_fn is not None:
            result = self._embed_fn(text)
            if hasattr(result, "__await__"):
                return await result  # type: ignore[return-value]
            return result  # type: ignore[return-value]
        return embed_query(text)

    def _build_search_body(
        self, vector: list[float], hotel_id: str, category_hint: str | None
    ) -> dict[str, Any]:
        must: list[dict[str, Any]] = [
            {"key": "hotel_id", "match": {"value": hotel_id}},
        ]
        if category_hint:
            # Additive: hint plus "overview" so the LLM is never starved of context.
            must.append({"key": "category", "match": {"any": [category_hint, "overview"]}})
        return {
            "vector": vector,
            "limit": self._top_k * 2,  # overshoot so the threshold cut can still produce top_k
            "with_payload": True,
            "filter": {"must": must},
        }

    async def _search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if self._search_fn is not None:
            return await self._search_fn(body["vector"], body["filter"], body["limit"])
        if self._session is None:
            raise RuntimeError("HotelKBRetriever needs either a session or a search_fn")
        resp = await qdrant_request(
            self._session,
            "POST",
            f"/collections/{self._collection}/points/search",
            json=body,
        )
        return resp.get("result", []) or []

    def _finalize(
        self,
        hotel_id: str,
        query: str,
        normalized_query: str,
        hits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        scored = sorted(hits, key=lambda h: h.get("score", 0.0), reverse=True)
        kept = [h for h in scored if h.get("score", 0.0) >= self._min_score][: self._top_k]

        if not kept:
            best_below = scored[0].get("score", 0.0) if scored else 0.0
            return {
                "hotel_id": hotel_id,
                "query": query,
                "normalized_query": normalized_query,
                "top_score": float(best_below),
                "count": 0,
                "chunks": [],
                "reason": REASON_NO_MATCH,
            }

        chunks = [self._chunk_from_hit(h) for h in kept]
        return {
            "hotel_id": hotel_id,
            "query": query,
            "normalized_query": normalized_query,
            "top_score": float(chunks[0]["score"]),
            "count": len(chunks),
            "chunks": chunks,
            "reason": None,
        }

    @staticmethod
    def _chunk_from_hit(hit: dict[str, Any]) -> dict[str, Any]:
        payload = hit.get("payload", {}) or {}
        return {
            "chunk_id": payload.get("chunk_id", str(hit.get("id", ""))),
            "score": float(hit.get("score", 0.0)),
            "category": payload.get("category", ""),
            "text": payload.get("text", ""),
            "text_en": payload.get("text_en", ""),
            "activity_tags": payload.get("activity_tags", []),
        }

    @staticmethod
    def _empty(
        hotel_id: str, query: str, normalized_query: str, reason: str
    ) -> dict[str, Any]:
        return {
            "hotel_id": hotel_id,
            "query": query,
            "normalized_query": normalized_query,
            "top_score": 0.0,
            "count": 0,
            "chunks": [],
            "reason": reason,
        }
