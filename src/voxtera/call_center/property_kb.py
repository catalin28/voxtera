"""Property KB — the hotel's OWN guide as a concierge retrieval source.

P1.4 ("one brain"): the concierge pipeline serves two products with one
retrieval contract:

- **travel agent** — cross-hotel discovery over the Qdrant listings KB
  (~1,500 portfolio hotels), routed by :class:`SourceRouter`;
- **hotel concierge** — questions about ONE property ("what time is
  breakfast?"), answered from that property's ingested guide in the
  per-hotel SQLite RAG store (``voxtera.rag``) — the same store the legacy
  ``BOT_BRAIN=hotel`` voice pipeline reads via ``RAGContextInjector``.

This module adapts the SQLite retriever to the retrieval dict shape the
concierge render step already consumes (``{"hotels": [{payload, evidence}]}``),
so scoping a request with ``hotel_id`` flips the source without touching
decompose, triage, render, or the voice/chat surfaces.

Embeddings: ``voxtera.rag.embeddings`` honours ``VOXTERA_EMBEDDING_URL``
(sidecar) and falls back to in-process sentence-transformers, so the
standalone concierge service works in both setups.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from loguru import logger


class PropertyKBRetriever:
    """Per-hotel SQLite guide retrieval in the concierge retrieval shape."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        top_k: int = 6,
        min_score: float = 0.25,
    ) -> None:
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        path = Path(db_path or os.environ.get("VOXTERA_DB_PATH", default_db))
        path.parent.mkdir(parents=True, exist_ok=True)
        store = ChunksStore(path)
        store.init_schema()
        self._retriever = Retriever(store, top_k=top_k, min_score=min_score)
        self._hotel_names: dict[str, str] = {}

    def hotel_name(self, hotel_id: str) -> str:
        """Display name from the hotel's config; falls back to the id."""
        if hotel_id not in self._hotel_names:
            try:
                from voxtera.actions import load_hotel_config

                self._hotel_names[hotel_id] = load_hotel_config(hotel_id).hotel_name
            except Exception:  # noqa: BLE001 — config is optional for retrieval
                self._hotel_names[hotel_id] = hotel_id
        return self._hotel_names[hotel_id]

    async def retrieve(
        self,
        *,
        hotel_id: str,
        query: str,
        language: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve guide chunks for one property, shaped for the render step.

        Returns the concierge retrieval dict: zero or one "hotel" (the
        property), with the matched guide chunks as its evidence map. An empty
        ``hotels`` list keeps the pipeline's fail-closed no-match behaviour.
        """
        chunks = await self._retriever.retrieve(hotel_id=hotel_id, query=query, language=language)
        evidence: dict[str, Any] = {}
        for i, chunk in enumerate(chunks):
            label = chunk.category or f"passage_{i + 1}"
            if label in evidence:  # duplicate category → keep both
                label = f"{label}_{i + 1}"
            evidence[label] = {
                "text": chunk.text,
                "score": round(float(chunk.score), 4),
                "doc_id": chunk.doc_id,
                "category": chunk.category,
            }
        hotels: list[dict[str, Any]] = []
        if evidence:
            hotels.append(
                {
                    "hotel_id": hotel_id,
                    "score": float(chunks[0].score),
                    "payload": {
                        "hotel_name": self.hotel_name(hotel_id),
                        "district": "",
                        "region": "",
                    },
                    "evidence": evidence,
                }
            )
        else:
            logger.info(
                "[property-kb] no guide chunks for hotel_id={!r} query={!r}", hotel_id, query[:80]
            )
        return {
            "source": "property_kb",
            "requirements": [query],
            "normalized_requirements": [query],
            "top_score": float(chunks[0].score) if chunks else 0.0,
            "hotels": hotels,
        }
