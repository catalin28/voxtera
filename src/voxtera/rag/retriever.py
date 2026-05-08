"""Cosine-similarity retriever over the chunks store.

Given a user query, embeds it, fetches candidate chunks for the hotel,
ranks by cosine similarity, and returns the top-K results above a
minimum score threshold.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np
from loguru import logger

from voxtera.rag.embeddings import embed
from voxtera.rag.store import ChunksStore


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
    ) -> None:
        self._store = store
        self._top_k = top_k
        self._min_score = min_score
        # In-process cache: (hotel_id, language) -> (candidates, normalised matrix).
        # Chunks are ingested once at startup and never change during a session,
        # so caching eliminates the SQLite read + blob deserialization on every turn.
        self._chunk_cache: dict[tuple[str, str | None], tuple[list, np.ndarray]] = {}

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

    async def retrieve(
        self, *, hotel_id: str, query: str, language: str | None = None
    ) -> list[RetrievedChunk]:
        """Return the most relevant chunks for *query*, sorted by descending score."""
        if not query.strip():
            return []

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
        return results
