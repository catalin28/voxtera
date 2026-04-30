"""Tests for voxtera.rag.embeddings — local sentence-transformers wrapper."""

from __future__ import annotations

import numpy as np

from voxtera.rag.embeddings import EMBEDDING_DIM, embed, embed_sync

# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestEmbedSync:
    def test_empty_input(self) -> None:
        assert embed_sync([]) == []

    def test_single_text(self) -> None:
        result = embed_sync(["hello world"])
        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM

    def test_multiple_texts(self) -> None:
        result = embed_sync(["hello", "world", "foo bar"])
        assert len(result) == 3
        for vec in result:
            assert len(vec) == EMBEDDING_DIM

    def test_vectors_are_normalised(self) -> None:
        """Model should return L2-normalised vectors (unit length)."""
        result = embed_sync(["test normalisation"])
        norm = float(np.linalg.norm(result[0]))
        assert abs(norm - 1.0) < 1e-4

    def test_similar_texts_have_high_cosine(self) -> None:
        vecs = embed_sync(["the hotel pool is open", "the swimming pool is available"])
        a, b = np.array(vecs[0]), np.array(vecs[1])
        cos = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        assert cos > 0.7

    def test_dissimilar_texts_have_lower_cosine(self) -> None:
        vecs = embed_sync(["the hotel pool is open", "quarterly earnings report for Q3"])
        a, b = np.array(vecs[0]), np.array(vecs[1])
        cos = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        assert cos < 0.85


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


class TestEmbedAsync:
    async def test_empty_input(self) -> None:
        assert await embed([]) == []

    async def test_returns_correct_dimensions(self) -> None:
        result = await embed(["hello"])
        assert len(result) == 1
        assert len(result[0]) == EMBEDDING_DIM
