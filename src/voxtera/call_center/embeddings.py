"""Embedding helpers for the Voxtera call-center RAG stack.

Wraps the multilingual-e5-large encoder so all retrievers and the
ingestion pipeline share one model instance and one place to apply
the `query: ` / `passage: ` prefixes the model expects.

The model is lazy-loaded on first use to keep import-time cheap and
to let unit tests run without ever loading it (they inject their own
`embed_fn`).
"""

from __future__ import annotations

import time

from loguru import logger

EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
PREFIX_PASSAGE = "passage: "
PREFIX_QUERY = "query: "

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: {}", EMBEDDING_MODEL)
        t0 = time.perf_counter()
        _model = SentenceTransformer(EMBEDDING_MODEL)
        logger.info("Model loaded in {:.1f}s", time.perf_counter() - t0)
    return _model


def embed_texts(texts: list[str], prefix: str = PREFIX_QUERY) -> list[list[float]]:
    """Embed a batch of texts with the given e5-large prefix."""
    model = _get_model()
    prefixed = [f"{prefix}{t}" for t in texts]
    embeddings = model.encode(prefixed, normalize_embeddings=True)
    return embeddings.tolist()


def embed_query(text: str) -> list[float]:
    """Embed a single query string with the `query: ` prefix."""
    return embed_texts([text], prefix=PREFIX_QUERY)[0]


def embed_passages(texts: list[str]) -> list[list[float]]:
    """Embed a batch of passages with the `passage: ` prefix."""
    return embed_texts(texts, prefix=PREFIX_PASSAGE)
