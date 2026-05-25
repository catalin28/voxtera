"""Local embedding using sentence-transformers, with optional sidecar support.

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

**Sidecar mode:** When ``VOXTERA_EMBEDDING_URL`` is set (e.g.
``http://127.0.0.1:9400``), embedding requests are sent to the sidecar
server via HTTP instead of loading the model in-process. This eliminates
the 3-8s cold-start penalty per bot subprocess. Falls back to local model
if the sidecar is unreachable.

The model is loaded lazily on first call and cached for the lifetime of the
process.  CPU-bound encoding runs in a thread-pool executor to avoid
blocking the pipecat voice-loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from functools import lru_cache

from loguru import logger

EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_DIM = 384

# Standard prefixes for E5 models.
PREFIX_QUERY = "query: "
PREFIX_PASSAGE = "passage: "

# Sidecar URL — set by serve.py when the embedding server is running.
_EMBEDDING_URL: str | None = os.environ.get("VOXTERA_EMBEDDING_URL")


def _embed_via_sidecar(texts: list[str], prefix: str) -> list[list[float]] | None:
    """Try to embed via the sidecar server. Returns None on failure."""
    if not _EMBEDDING_URL:
        return None
    url = _EMBEDDING_URL.rstrip("/") + "/embed"
    payload = json.dumps({"texts": texts, "prefix": prefix}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            logger.debug(
                "[embed] sidecar returned {} embeddings in {}ms",
                len(data["embeddings"]),
                data.get("elapsed_ms", "?"),
            )
            return data["embeddings"]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("[embed] sidecar unavailable ({}), falling back to local model", exc)
        return None


@lru_cache(maxsize=1)
def _get_model():
    """Load the embedding model once and cache it."""
    from sentence_transformers import SentenceTransformer

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
    """Embed *texts* synchronously.

    Tries the sidecar server first (zero cold-start), falls back to
    loading the local model if the sidecar is unavailable.

    *prefix* is prepended to each text before encoding.  Use
    ``PREFIX_QUERY`` for search queries and ``PREFIX_PASSAGE`` for
    document chunks during ingest.
    """
    if not texts:
        return []

    # Fast path: sidecar already has the model loaded.
    result = _embed_via_sidecar(texts, prefix)
    if result is not None:
        return result

    # Fallback: load the model in-process (3-8s first time, cached after).
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
