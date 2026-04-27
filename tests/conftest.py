"""Shared test fixtures."""

from __future__ import annotations

import pytest

from voxtera.rag.embeddings import _clear_client_cache


@pytest.fixture(autouse=True)
def _reset_embedding_client_cache() -> None:
    """Drop the cached AsyncOpenAI client before each test so per-test
    ``patch('voxtera.rag.embeddings.openai.AsyncOpenAI')`` calls take effect.

    Without this, the first test to call ``embed()`` caches the patched mock
    keyed by api_key, and subsequent tests with the same api_key never re-
    invoke the patched constructor and therefore see the wrong mock methods.
    """
    _clear_client_cache()
