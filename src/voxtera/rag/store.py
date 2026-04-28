"""SQLite-backed chunk store for RAG content.

Multi-tenant from day one: every row carries a ``hotel_id`` even though
Phase 1 only uses a single hotel. Embeddings are stored as raw bytes
(``numpy.ndarray.tobytes()``) and unpacked with ``numpy.frombuffer()``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Dimension of intfloat/multilingual-e5-small vectors.
EMBEDDING_DIM = 384

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_id      TEXT NOT NULL,
    doc_id        TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    language      TEXT NOT NULL,
    category      TEXT,
    text          TEXT NOT NULL,
    embedding     BLOB NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (hotel_id, doc_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_tenant_lang ON chunks (hotel_id, language);
"""

_UPSERT_SQL = """\
INSERT INTO chunks (hotel_id, doc_id, chunk_index, language, category, text, embedding, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(hotel_id, doc_id, chunk_index) DO UPDATE SET
    language   = excluded.language,
    category   = excluded.category,
    text       = excluded.text,
    embedding  = excluded.embedding,
    updated_at = CURRENT_TIMESTAMP;
"""


@dataclass(frozen=True)
class StoredChunk:
    """A chunk row as returned by the store."""

    id: int
    hotel_id: str
    doc_id: str
    chunk_index: int
    language: str
    category: str | None
    text: str
    embedding: list[float]


class ChunksStore:
    """Synchronous SQLite store for embedded text chunks."""

    def __init__(self, db_path: str | Path) -> None:
        # Allow cross-thread access (useful when async code or Pipecat threads call the store)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # WAL mode for better concurrent-read behaviour.
        self._conn.execute("PRAGMA journal_mode=WAL;")

    def init_schema(self) -> None:
        """Create the chunks table and indexes (idempotent)."""
        self._conn.executescript(_SCHEMA_SQL)

    def upsert_chunk(
        self,
        *,
        hotel_id: str,
        doc_id: str,
        chunk_index: int,
        language: str,
        category: str | None,
        text: str,
        embedding: list[float],
    ) -> None:
        """Insert or update a single chunk identified by (hotel_id, doc_id, chunk_index)."""
        if len(embedding) != EMBEDDING_DIM:
            raise ValueError(
                f"Embedding dimension mismatch: expected {EMBEDDING_DIM}, got {len(embedding)}"
            )
        blob = np.array(embedding, dtype=np.float32).tobytes()
        self._conn.execute(
            _UPSERT_SQL,
            (hotel_id, doc_id, chunk_index, language, category, text, blob),
        )
        self._conn.commit()

    def fetch_for_hotel(self, *, hotel_id: str, language: str | None = None) -> list[StoredChunk]:
        """Return all chunks for a hotel, optionally filtered by language."""
        if language is not None:
            cursor = self._conn.execute(
                "SELECT id, hotel_id, doc_id, chunk_index, language, category, text, embedding "
                "FROM chunks WHERE hotel_id = ? AND language = ?",
                (hotel_id, language),
            )
        else:
            cursor = self._conn.execute(
                "SELECT id, hotel_id, doc_id, chunk_index, language, category, text, embedding "
                "FROM chunks WHERE hotel_id = ?",
                (hotel_id,),
            )
        return [self._row_to_chunk(row) for row in cursor.fetchall()]

    def delete_doc(self, *, hotel_id: str, doc_id: str) -> int:
        """Delete all chunks for a document. Returns number of rows deleted."""
        cursor = self._conn.execute(
            "DELETE FROM chunks WHERE hotel_id = ? AND doc_id = ?",
            (hotel_id, doc_id),
        )
        self._conn.commit()
        return cursor.rowcount

    def count(self, *, hotel_id: str | None = None) -> int:
        """Count chunks, optionally scoped to a hotel."""
        if hotel_id is not None:
            cursor = self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE hotel_id = ?", (hotel_id,)
            )
        else:
            cursor = self._conn.execute("SELECT COUNT(*) FROM chunks")
        row = cursor.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_chunk(row: tuple) -> StoredChunk:  # type: ignore[type-arg]
        """Convert a raw DB row to a StoredChunk dataclass."""
        id_, hotel_id, doc_id, chunk_index, language, category, text, blob = row
        embedding = np.frombuffer(blob, dtype=np.float32).tolist()
        return StoredChunk(
            id=id_,
            hotel_id=hotel_id,
            doc_id=doc_id,
            chunk_index=chunk_index,
            language=language,
            category=category,
            text=text,
            embedding=embedding,
        )
