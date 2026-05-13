"""Cosine-similarity retriever over the chunks store.

Given a user query, embeds it, fetches candidate chunks for the hotel,
ranks by cosine similarity, and returns the top-K results above a
minimum score threshold.

Two caches accelerate retrieval:

1. ``_chunk_cache`` — caches the (candidate chunks, normalised embedding
   matrix) per (hotel_id, language). Populated lazily on first retrieve
   or eagerly via :meth:`warmup`. Eliminates the SQLite read + blob
   deserialization on every turn.
2. ``_result_cache`` — caches (query → results) per (hotel_id, language,
   normalized_query). Eliminates the ~200-1500ms CPU cost of running
   ``multilingual-e5-small`` on the query every time. The big latency win.
   Pre-warmed at session start with a curated set of common hotel queries
   so the bot is fast on the FIRST utterance about breakfast/parking/etc.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from loguru import logger

from voxtera.rag.embeddings import embed
from voxtera.rag.store import ChunksStore

# Curated list of common hotel-concierge queries the warm-up phase will
# pre-compute and cache. Picked to cover the most-asked topics at any
# typical hotel: meals, amenities, location, recommendations. Per
# benchmark data, these queries pay 1.5-2 seconds when uncached and ~0ms
# when warm. The list is intentionally diverse: high-similarity queries
# (e.g. "what time is breakfast?" and "when does breakfast start?") still
# count as separate cache entries because the LLM sees the literal user
# transcript — but the cost of embedding both is small relative to the
# session payoff.
DEFAULT_WARMUP_QUERIES: tuple[str, ...] = (
    "what time does breakfast start",
    "what time is breakfast",
    "what restaurants do you have",
    "where is the gym",
    "where is the spa",
    "do you have parking",
    "what is the wifi password",
    "can you recommend a coffee shop nearby",
    "can you recommend a restaurant nearby",
    "how do i get to the airport",
    "what time does the spa close",
    "is there a pool",
    "what is the checkout time",
    "is room service available",
)


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk returned by the retriever, scored by relevance."""

    text: str
    score: float  # cosine similarity, clamped to 0..1
    doc_id: str
    category: str | None


class Retriever:
    """Async cosine-similarity retriever backed by ChunksStore + local embeddings."""

    def __init__(
        self,
        store: ChunksStore,
        *,
        top_k: int = 5,
        min_score: float = 0.25,
        result_cache_size: int = 256,
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._min_score = min_score
        # Chunk cache: (hotel_id, language) -> (candidates, normalised matrix).
        # Chunks are ingested once at startup and never change during a session,
        # so caching eliminates the SQLite read + blob deserialization.
        self._chunk_cache: dict[tuple[str, str | None], tuple[list, np.ndarray]] = {}
        # Query→result cache: (hotel_id, language, normalized_query) -> results.
        # LRU-bounded so a long session can't grow this unbounded; OrderedDict
        # gives O(1) move-to-end on hit. 256 entries is generous — typical
        # hotel sessions ask 20-50 distinct questions.
        self._result_cache: OrderedDict[tuple[str, str | None, str], list[RetrievedChunk]] = (
            OrderedDict()
        )
        self._result_cache_max = result_cache_size

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Cache-key normalization. Lowercase, strip, collapse whitespace,
        strip trailing punctuation that doesn't change semantic meaning.

        Aggressive enough that "What time is breakfast?" and "what time is breakfast"
        share a cache entry, but conservative enough that "where is the spa" and
        "where is the gym" stay distinct.
        """
        # Strip trailing question marks / periods / exclamations.
        normalized = query.strip().lower().rstrip("?.!,;:")
        # Collapse internal whitespace.
        return " ".join(normalized.split())

    async def warmup(self, *, hotel_id: str, language: str | None = None) -> None:
        """Pre-load and cache the chunk matrix so the first query is instant.

        Call this as a fire-and-forget task after the bot joins the room.
        """
        cache_key = (hotel_id, language)
        if cache_key in self._chunk_cache:
            return
        candidates = await asyncio.to_thread(
            self._store.fetch_for_hotel, hotel_id=hotel_id, language=language
        )
        if not candidates:
            logger.info("[rag-warmup] no chunks found for hotel_id={!r}", hotel_id)
            return
        matrix = np.array([c.embedding for c in candidates], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        matrix /= norms
        self._chunk_cache[cache_key] = (candidates, matrix)
        logger.info("[rag-warmup] cached {} chunks for hotel_id={!r}", len(candidates), hotel_id)

    async def warmup_queries(
        self,
        *,
        hotel_id: str,
        queries: tuple[str, ...] | list[str] = DEFAULT_WARMUP_QUERIES,
        language: str | None = None,
    ) -> None:
        """Pre-run a curated set of common queries to populate the result cache.

        Call this as a fire-and-forget task after the bot joins the room.
        Each query takes ~150-300ms to embed locally; 14 queries = ~3-4s of
        background work. After this completes, those queries return in ~0ms
        on first ask — eliminating the 1500ms+ "first RAG retrieval" hit
        that the user feels on their first conversational turn.
        """
        logger.info(
            "[rag-warmup] pre-computing {} common queries for hotel_id={!r}",
            len(queries),
            hotel_id,
        )
        for q in queries:
            try:
                await self.retrieve(hotel_id=hotel_id, query=q, language=language)
            except Exception:
                logger.opt(exception=True).warning("[rag-warmup] failed for query={!r}", q)
        logger.info(
            "[rag-warmup] result cache populated ({} entries)",
            len(self._result_cache),
        )

    async def retrieve(
        self, *, hotel_id: str, query: str, language: str | None = None
    ) -> list[RetrievedChunk]:
        """Return the most relevant chunks for *query*, sorted by descending score."""
        if not query.strip():
            return []

        # Result-cache lookup — covers ~80% of repeated questions in a session
        # AND every pre-warmed query. Skips embedding + similarity entirely.
        normalized = self._normalize_query(query)
        result_key = (hotel_id, language, normalized)
        cached = self._result_cache.get(result_key)
        if cached is not None:
            # Touch the entry so LRU ordering reflects recent access.
            self._result_cache.move_to_end(result_key)
            logger.debug(
                "[rag] result cache HIT for query={!r} (returning {} chunks)",
                normalized[:60],
                len(cached),
            )
            return cached

        cache_key = (hotel_id, language)
        if cache_key not in self._chunk_cache:
            # Cache miss (first query before warmup completed, or new language).
            # Load from SQLite and populate cache.
            candidates = await asyncio.to_thread(
                self._store.fetch_for_hotel, hotel_id=hotel_id, language=language
            )
            if not candidates:
                logger.debug("No chunks for hotel_id={!r}, language={!r}", hotel_id, language)
                return []
            matrix = np.array([c.embedding for c in candidates], dtype=np.float32)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            matrix /= norms
            self._chunk_cache[cache_key] = (candidates, matrix)

        candidates, matrix = self._chunk_cache[cache_key]
        if not candidates:
            return []

        try:
            query_vectors = await embed([query])
        except Exception:
            logger.opt(exception=True).warning("Embedding API error — returning no results")
            return []

        if not query_vectors:
            logger.warning("Embedding service returned no vectors for query")
            return []

        query_vec = np.array(query_vectors[0], dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []
        query_vec /= query_norm

        scores = matrix @ query_vec  # shape (n_candidates,)
        # Clamp to [0, 1] before filtering — negative values from floating-point
        # noise should not leak through a min_score of 0.0.
        clamped = np.clip(scores, 0.0, 1.0)

        # Build scored pairs, filter, sort, and cap.
        scored = [
            RetrievedChunk(
                text=c.text,
                score=float(s),
                doc_id=c.doc_id,
                category=c.category,
            )
            for c, s in zip(candidates, clamped, strict=True)
            if float(s) >= self._min_score
        ]
        scored.sort(key=lambda r: r.score, reverse=True)

        results = scored[: self._top_k]
        logger.debug(
            "Retrieved {}/{} chunks (top_k={}, min_score={}) for hotel_id={!r}",
            len(results),
            len(candidates),
            self._top_k,
            self._min_score,
            hotel_id,
        )

        # Populate the result cache for subsequent identical queries.
        # Empty results are cached too — re-running an embedding for a query
        # that found nothing is wasted work.
        self._result_cache[result_key] = results
        # LRU eviction: drop the oldest entry once we exceed the bound.
        while len(self._result_cache) > self._result_cache_max:
            self._result_cache.popitem(last=False)

        return results
