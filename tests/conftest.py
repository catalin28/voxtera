"""Shared test fixtures."""

from __future__ import annotations

import pytest

from voxtera.rag.embeddings import _clear_model_cache


@pytest.fixture(autouse=True)
def _reset_embedding_model_cache() -> None:
    """Drop the cached SentenceTransformer before each test so mocks take effect."""
    _clear_model_cache()
