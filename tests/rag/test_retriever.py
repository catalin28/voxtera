"""Tests for the cosine-similarity retriever."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from voxtera.rag.retriever import RetrievedChunk, Retriever
from voxtera.rag.store import EMBEDDING_DIM, ChunksStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unit_vec(index: int) -> list[float]:
    """Return a unit vector with 1.0 at *index*, 0.0 elsewhere."""
    v = [0.0] * EMBEDDING_DIM
    v[index] = 1.0
    return v


def _store_with_chunks(chunks: list[dict[str, object]]) -> ChunksStore:
    """Create an in-memory store pre-loaded with the given chunks."""
    store = ChunksStore(":memory:")
    store.init_schema()
    for c in chunks:
        store.upsert_chunk(
            hotel_id=str(c.get("hotel_id", "h1")),
            doc_id=str(c.get("doc_id", "d1")),
            chunk_index=int(c.get("chunk_index", 0)),
            language=str(c.get("language", "en")),
            category=c.get("category"),  # type: ignore[arg-type]
            text=str(c.get("text", "")),
            embedding=c.get("embedding", _unit_vec(0)),  # type: ignore[arg-type]
        )
    return store


def _mock_embed(return_vec: list[float]) -> AsyncMock:
    """Return an AsyncMock for embed() that always returns *return_vec*."""
    mock = AsyncMock(return_value=[return_vec])
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetrieveEmpty:
    """Empty store returns [] without calling the embedding API."""

    @pytest.mark.asyncio
    async def test_empty_store(self) -> None:
        store = ChunksStore(":memory:")
        store.init_schema()
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed") as mock_emb:
            result = await retriever.retrieve(hotel_id="h1", query="anything")

        assert result == []
        mock_emb.assert_not_called()


class TestRetrieveMatching:
    """Chunks that match the query vector are returned with high scores."""

    @pytest.mark.asyncio
    async def test_matching_chunk_returned(self) -> None:
        """A chunk whose embedding is identical to the query should score ~1.0."""
        vec = _unit_vec(5)
        store = _store_with_chunks(
            [
                {"text": "pool info", "embedding": vec, "chunk_index": 0},
            ]
        )
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed", _mock_embed(vec)):
            result = await retriever.retrieve(hotel_id="h1", query="pool")

        assert len(result) == 1
        assert result[0].text == "pool info"
        assert result[0].score > 0.99

    @pytest.mark.asyncio
    async def test_result_is_retrieved_chunk(self) -> None:
        vec = _unit_vec(0)
        store = _store_with_chunks(
            [
                {"text": "t", "embedding": vec, "doc_id": "d1", "category": "amenities"},
            ]
        )
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed", _mock_embed(vec)):
            result = await retriever.retrieve(hotel_id="h1", query="x")

        assert isinstance(result[0], RetrievedChunk)
        assert result[0].doc_id == "d1"
        assert result[0].category == "amenities"

    @pytest.mark.asyncio
    async def test_sorted_descending(self) -> None:
        """Higher-similarity chunks come first."""
        # v0 and v1 are orthogonal unit vectors.
        # Query is closer to v0 than v1 (blend: 0.9*v0 + 0.1*v1).
        v0 = np.array(_unit_vec(0), dtype=np.float32)
        v1 = np.array(_unit_vec(1), dtype=np.float32)
        query_raw = 0.9 * v0 + 0.1 * v1
        query_vec = (query_raw / np.linalg.norm(query_raw)).tolist()

        store = _store_with_chunks(
            [
                {"text": "second", "embedding": _unit_vec(1), "chunk_index": 0},
                {"text": "first", "embedding": _unit_vec(0), "chunk_index": 1},
            ]
        )
        retriever = Retriever(store, min_score=0.05)

        with patch("voxtera.rag.retriever.embed", _mock_embed(query_vec)):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert len(result) == 2
        assert result[0].text == "first"
        assert result[1].text == "second"
        assert result[0].score > result[1].score
        # Both scores should be in (0, 1) — partial matches, not exact.
        for r in result:
            assert 0.0 < r.score < 1.0


class TestRetrieveFiltering:
    """min_score and top_k caps are respected."""

    @pytest.mark.asyncio
    async def test_below_min_score_excluded(self) -> None:
        """Orthogonal vectors have similarity ~0 — below any min_score > 0."""
        store = _store_with_chunks(
            [
                {"text": "irrelevant", "embedding": _unit_vec(0), "chunk_index": 0},
            ]
        )
        retriever = Retriever(store, min_score=0.3)

        # Query vector is orthogonal to stored chunk.
        with patch("voxtera.rag.retriever.embed", _mock_embed(_unit_vec(1))):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert result == []

    @pytest.mark.asyncio
    async def test_top_k_cap(self) -> None:
        """Only top_k results are returned even when more pass min_score."""
        # All chunks share the same direction as the query → all score ~1.0.
        vec = _unit_vec(3)
        store = _store_with_chunks(
            [{"text": f"chunk-{i}", "embedding": vec, "chunk_index": i} for i in range(5)]
        )
        retriever = Retriever(store, top_k=2)

        with patch("voxtera.rag.retriever.embed", _mock_embed(vec)):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_language_filter(self) -> None:
        """language kwarg is forwarded to fetch_for_hotel."""
        vec = _unit_vec(0)
        store = _store_with_chunks(
            [
                {"text": "en", "embedding": vec, "chunk_index": 0, "language": "en"},
                {"text": "es", "embedding": vec, "chunk_index": 1, "language": "es"},
            ]
        )
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed", _mock_embed(vec)):
            result = await retriever.retrieve(hotel_id="h1", query="q", language="es")

        assert len(result) == 1
        assert result[0].text == "es"


class TestRetrieveErrorHandling:
    """Embedding API failures degrade gracefully."""

    @pytest.mark.asyncio
    async def test_embed_error_returns_empty(self) -> None:
        vec = _unit_vec(0)
        store = _store_with_chunks(
            [
                {"text": "chunk", "embedding": vec},
            ]
        )
        retriever = Retriever(store)

        failing_embed = AsyncMock(side_effect=RuntimeError("API down"))
        with patch("voxtera.rag.retriever.embed", failing_embed):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert result == []

    @pytest.mark.asyncio
    async def test_hotel_isolation(self) -> None:
        """Chunks from other hotels are not returned."""
        vec = _unit_vec(0)
        store = _store_with_chunks(
            [
                {"text": "other", "embedding": vec, "hotel_id": "h2"},
            ]
        )
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed", _mock_embed(vec)):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert result == []


class TestRetrieveEdgeCases:
    """Score clamping and missing category."""

    @pytest.mark.asyncio
    async def test_score_clamped_to_zero(self) -> None:
        """Negative cosine similarity (anti-correlated) is clamped to 0.0."""
        # Stored embedding points in the opposite direction to the query.
        pos = _unit_vec(0)
        neg = [-x for x in pos]
        store = _store_with_chunks(
            [
                {"text": "anti", "embedding": neg, "chunk_index": 0},
            ]
        )
        retriever = Retriever(store, min_score=0.0)

        with patch("voxtera.rag.retriever.embed", _mock_embed(pos)):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        # Anti-correlated → raw score ≈ -1.0, clamped to 0.0, filtered out by >= 0.0.
        # 0.0 == 0.0 passes the >= check, so it appears with score 0.0.
        assert len(result) == 1
        assert result[0].score == 0.0

    @pytest.mark.asyncio
    async def test_category_none_by_default(self) -> None:
        vec = _unit_vec(0)
        store = _store_with_chunks(
            [
                {"text": "no cat", "embedding": vec},
            ]
        )
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed", _mock_embed(vec)):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert result[0].category is None

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self) -> None:
        """Empty or whitespace-only query short-circuits without store or API calls."""
        vec = _unit_vec(0)
        store = _store_with_chunks([{"text": "x", "embedding": vec}])
        retriever = Retriever(store)

        for q in ("", "   ", "\n"):
            result = await retriever.retrieve(hotel_id="h1", query=q)
            assert result == []

    @pytest.mark.asyncio
    async def test_embed_returns_empty_list(self) -> None:
        """If embed() returns [] instead of raising, retriever degrades gracefully."""
        vec = _unit_vec(0)
        store = _store_with_chunks([{"text": "x", "embedding": vec}])
        retriever = Retriever(store)

        with patch("voxtera.rag.retriever.embed", AsyncMock(return_value=[])):
            result = await retriever.retrieve(hotel_id="h1", query="q")

        assert result == []
