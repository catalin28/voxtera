"""Tests for voxtera.rag.store — ChunksStore."""

from __future__ import annotations

import pytest

from voxtera.rag.store import EMBEDDING_DIM, ChunksStore, StoredChunk


def _fake_embedding(seed: float = 0.0) -> list[float]:
    """Return a deterministic embedding vector for testing."""
    return [seed + i * 0.001 for i in range(EMBEDDING_DIM)]


@pytest.fixture()
def store() -> ChunksStore:
    """In-memory store with schema already initialised."""
    s = ChunksStore(":memory:")
    s.init_schema()
    return s


class TestInitSchema:
    def test_creates_table(self, store: ChunksStore) -> None:
        # If we got here, init_schema() succeeded.
        assert store.count() == 0

    def test_idempotent(self) -> None:
        s = ChunksStore(":memory:")
        s.init_schema()
        s.init_schema()  # second call should not raise
        assert s.count() == 0


class TestUpsertChunk:
    def test_insert_single(self, store: ChunksStore) -> None:
        store.upsert_chunk(
            hotel_id="h1",
            doc_id="d1",
            chunk_index=0,
            language="en",
            category="menu",
            text="Breakfast is served 7-10am.",
            embedding=_fake_embedding(1.0),
        )
        assert store.count(hotel_id="h1") == 1

    def test_upsert_replaces_on_conflict(self, store: ChunksStore) -> None:
        common = dict(hotel_id="h1", doc_id="d1", chunk_index=0, language="en", category="menu")
        store.upsert_chunk(**common, text="old text", embedding=_fake_embedding(1.0))
        store.upsert_chunk(**common, text="new text", embedding=_fake_embedding(2.0))
        assert store.count(hotel_id="h1") == 1
        chunks = store.fetch_for_hotel(hotel_id="h1")
        assert chunks[0].text == "new text"

    def test_different_chunk_indexes_create_separate_rows(self, store: ChunksStore) -> None:
        for idx in range(3):
            store.upsert_chunk(
                hotel_id="h1",
                doc_id="d1",
                chunk_index=idx,
                language="en",
                category=None,
                text=f"chunk {idx}",
                embedding=_fake_embedding(float(idx)),
            )
        assert store.count(hotel_id="h1") == 3

    def test_rejects_wrong_embedding_dimension(self, store: ChunksStore) -> None:
        with pytest.raises(ValueError, match=f"expected {EMBEDDING_DIM}, got 512"):
            store.upsert_chunk(
                hotel_id="h1",
                doc_id="d1",
                chunk_index=0,
                language="en",
                category=None,
                text="bad dim",
                embedding=[0.1] * 512,
            )


class TestFetchForHotel:
    def test_filters_by_hotel(self, store: ChunksStore) -> None:
        for hotel in ("h1", "h2"):
            store.upsert_chunk(
                hotel_id=hotel,
                doc_id="d1",
                chunk_index=0,
                language="en",
                category=None,
                text=f"text for {hotel}",
                embedding=_fake_embedding(),
            )
        chunks = store.fetch_for_hotel(hotel_id="h1")
        assert len(chunks) == 1
        assert chunks[0].hotel_id == "h1"

    def test_filters_by_language(self, store: ChunksStore) -> None:
        for lang in ("en", "ru"):
            store.upsert_chunk(
                hotel_id="h1",
                doc_id="d1",
                chunk_index=0 if lang == "en" else 1,
                language=lang,
                category=None,
                text=f"text in {lang}",
                embedding=_fake_embedding(),
            )
        chunks = store.fetch_for_hotel(hotel_id="h1", language="en")
        assert len(chunks) == 1
        assert chunks[0].language == "en"

    def test_returns_stored_chunk_dataclass(self, store: ChunksStore) -> None:
        emb = _fake_embedding(5.0)
        store.upsert_chunk(
            hotel_id="h1",
            doc_id="d1",
            chunk_index=0,
            language="en",
            category="spa",
            text="Relax",
            embedding=emb,
        )
        chunk = store.fetch_for_hotel(hotel_id="h1")[0]
        assert isinstance(chunk, StoredChunk)
        assert chunk.doc_id == "d1"
        assert chunk.category == "spa"
        # Embedding round-trips through BLOB with float32 precision.
        assert len(chunk.embedding) == EMBEDDING_DIM
        assert abs(chunk.embedding[0] - emb[0]) < 1e-5


class TestDeleteDoc:
    def test_deletes_and_returns_count(self, store: ChunksStore) -> None:
        for idx in range(3):
            store.upsert_chunk(
                hotel_id="h1",
                doc_id="d1",
                chunk_index=idx,
                language="en",
                category=None,
                text=f"chunk {idx}",
                embedding=_fake_embedding(),
            )
        deleted = store.delete_doc(hotel_id="h1", doc_id="d1")
        assert deleted == 3
        assert store.count(hotel_id="h1") == 0

    def test_does_not_affect_other_docs(self, store: ChunksStore) -> None:
        for doc in ("d1", "d2"):
            store.upsert_chunk(
                hotel_id="h1",
                doc_id=doc,
                chunk_index=0,
                language="en",
                category=None,
                text=f"text for {doc}",
                embedding=_fake_embedding(),
            )
        store.delete_doc(hotel_id="h1", doc_id="d1")
        assert store.count(hotel_id="h1") == 1

    def test_returns_zero_for_missing_doc(self, store: ChunksStore) -> None:
        deleted = store.delete_doc(hotel_id="h1", doc_id="nonexistent")
        assert deleted == 0


class TestCount:
    def test_global_count(self, store: ChunksStore) -> None:
        for hotel in ("h1", "h2"):
            store.upsert_chunk(
                hotel_id=hotel,
                doc_id="d1",
                chunk_index=0,
                language="en",
                category=None,
                text="x",
                embedding=_fake_embedding(),
            )
        assert store.count() == 2

    def test_scoped_count(self, store: ChunksStore) -> None:
        for hotel in ("h1", "h2"):
            store.upsert_chunk(
                hotel_id=hotel,
                doc_id="d1",
                chunk_index=0,
                language="en",
                category=None,
                text="x",
                embedding=_fake_embedding(),
            )
        assert store.count(hotel_id="h1") == 1
