"""Local embedding using sentence-transformers.

Uses ``intfloat/multilingual-e5-small`` — an instruction-tuned retrieval
model that handles asymmetric queries (short question → long passage)
well across 100+ languages including en, fr, ro, tr.

The model expects prefixed inputs:
- Queries:   ``"query: What are the pool hours?"``
- Documents: ``"passage: Pool open 6:30–22:00 …"``

The public ``embed()`` and ``embed_sync()`` functions accept a ``prefix``
parameter (default ``"query: "``) so callers can switch between query and
passage mode.  The CLI ingest path passes ``"passage: "``; the retriever
uses the default ``"query: "``.

The model is loaded lazily on first call and cached for the lifetime of the
process.  CPU-bound encoding runs in a thread-pool executor to avoid
blocking the pipecat voice-loop.
"""

from __future__ import annotations

import asyncio
import time
from functools import lru_cache

from loguru import logger
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_DIM = 384

# Standard prefixes for E5 models.
PREFIX_QUERY = "query: "
PREFIX_PASSAGE = "passage: "


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    """Load the embedding model once and cache it."""
    logger.info("Loading local embedding model: {}", EMBEDDING_MODEL)
    t0 = time.perf_counter()
    model = SentenceTransformer(EMBEDDING_MODEL)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.info("Loaded embedding model in {:.0f} ms", elapsed_ms)
    return model


def _clear_model_cache() -> None:
    """Drop the cached model.  Intended for test isolation only."""
    _get_model.cache_clear()


def embed_sync(texts: list[str], *, prefix: str = PREFIX_QUERY) -> list[list[float]]:
    """Embed *texts* synchronously using the local model.

    *prefix* is prepended to each text before encoding.  Use
    ``PREFIX_QUERY`` for search queries and ``PREFIX_PASSAGE`` for
    document chunks during ingest.
    """
    if not texts:
        return []

    model = _get_model()
    prefixed = [prefix + t for t in texts]
    t0 = time.perf_counter()
    embeddings = model.encode(prefixed, normalize_embeddings=True)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.debug("Embedded {} text(s) locally in {:.0f} ms", len(texts), elapsed_ms)
    return embeddings.tolist()


async def embed(texts: list[str], *, prefix: str = PREFIX_QUERY) -> list[list[float]]:
    """Async wrapper — runs the CPU-bound encode in a thread-pool executor."""
    if not texts:
        return []
    return await asyncio.to_thread(embed_sync, texts, prefix=prefix)
