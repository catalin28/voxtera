"""Thin async wrapper around OpenAI's embedding API.

Used by the chunker (during ingest) and the retriever (at query time).
Batches up to 100 texts per API call and retries on transient errors.
"""

from __future__ import annotations

import asyncio
import time

import openai
from loguru import logger

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# OpenAI embeddings API accepts up to 2048 inputs, but we cap at 100
# to keep individual request payloads reasonable.
_BATCH_SIZE = 100

# Retry config for transient (5xx / network) errors.
_MAX_RETRIES = 3
_INITIAL_BACKOFF_S = 0.5

# Cache of AsyncOpenAI clients keyed by api_key. Constructing a fresh client
# on every embed() call costs ~50-150ms of TLS handshake per bot turn, which
# is significant on the voice-loop critical path. One client per api_key is
# safe: openai.AsyncOpenAI is documented as concurrency-safe and reuses an
# underlying httpx connection pool.
_client_cache: dict[str, openai.AsyncOpenAI] = {}


def _get_client(api_key: str) -> openai.AsyncOpenAI:
    client = _client_cache.get(api_key)
    if client is None:
        client = openai.AsyncOpenAI(api_key=api_key)
        _client_cache[api_key] = client
    return client


def _clear_client_cache() -> None:
    """Drop all cached clients. Intended for test isolation only."""
    _client_cache.clear()


async def embed(texts: list[str], *, api_key: str) -> list[list[float]]:
    """Return one embedding vector per input string.

    Empty input returns ``[]`` immediately.  Inputs are batched into groups
    of 100 for the API call.  Retries up to 3 times on 5xx / network errors
    with exponential backoff.  4xx errors are raised immediately.

    The underlying ``AsyncOpenAI`` client is cached per ``api_key`` so the
    TLS handshake is paid once per process, not once per turn.
    """
    if not texts:
        return []

    client = _get_client(api_key)
    all_embeddings: list[list[float]] = []
    t0 = time.perf_counter()

    for batch_start in range(0, len(texts), _BATCH_SIZE):
        batch = texts[batch_start : batch_start + _BATCH_SIZE]
        response = await _embed_with_retry(client, batch)
        # OpenAI returns embeddings sorted by index within the batch.
        sorted_data = sorted(response.data, key=lambda d: d.index)
        all_embeddings.extend(d.embedding for d in sorted_data)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.debug(
        "Embedded {} text(s) in {:.0f}ms ({} batch(es))",
        len(texts),
        elapsed_ms,
        -(-len(texts) // _BATCH_SIZE),  # ceil division
    )
    return all_embeddings


async def _embed_with_retry(
    client: openai.AsyncOpenAI,
    batch: list[str],
) -> openai.types.CreateEmbeddingResponse:
    """Call the embeddings endpoint with retry on transient failures."""
    last_exc: Exception | None = None
    backoff = _INITIAL_BACKOFF_S

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
            )
        except openai.APIStatusError as exc:
            # 4xx → client error, don't retry.
            if 400 <= exc.status_code < 500:
                raise
            # 5xx → transient, retry.
            logger.warning(
                "Embedding API returned {} (attempt {}/{}), retrying in {:.1f}s",
                exc.status_code,
                attempt,
                _MAX_RETRIES,
                backoff,
            )
            last_exc = exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            logger.warning(
                "Embedding API network error (attempt {}/{}): {}, retrying in {:.1f}s",
                attempt,
                _MAX_RETRIES,
                exc,
                backoff,
            )
            last_exc = exc

        # Don't sleep after the final attempt — we're about to raise.
        if attempt < _MAX_RETRIES:
            await asyncio.sleep(backoff)
            backoff *= 2

    # All retries exhausted.
    raise last_exc  # type: ignore[misc]
