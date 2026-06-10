"""P1.4 "one brain" — property (hotel) scope on the concierge pipeline.

With ``run(hotel_id=...)`` the concierge answers as ONE property's concierge:
KB retrieval reads that hotel's own guide (property KB) instead of the Qdrant
travel listings; name resolution and geography clarifications collapse to
scoped guide lookups. Without the scope, nothing changes — same pipeline,
same travel-agent behaviour.

No network, no Redis, no embedding model: scripted classifier/decomposer,
in-memory SessionStore, fake compound + property KB.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.pipeline import ConciergePipeline
from voxtera.call_center.router import PATH_BROAD, PATH_SCOPED, SourceRouter
from voxtera.call_center.session import SessionStore
from voxtera.call_center.triage import Triage

HOTEL = "casa-dell-arte"


def _scripted_classify(raw: dict[str, Any]):
    async def fn(_u: str) -> dict[str, Any]:
        return raw

    return fn


def _scripted_decompose(payload: dict[str, Any]):
    async def fn(_u: str, _c: dict[str, Any]) -> dict[str, Any]:
        return payload

    return fn


class _FakeCompound:
    """Travel-listings retriever — must NOT be hit under property scope."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def discover(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"reason": None, "missing_requirements": [], "hotels": []}


class _FakePropertyKB:
    def __init__(self, *, empty: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._empty = empty

    async def retrieve(
        self, *, hotel_id: str, query: str, language: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"hotel_id": hotel_id, "query": query, "language": language})
        hotels = []
        if not self._empty:
            hotels = [
                {
                    "hotel_id": hotel_id,
                    "score": 0.9,
                    "payload": {"hotel_name": "Casa Dell Arte", "district": "", "region": ""},
                    "evidence": {
                        "dining": {"text": "Breakfast is served 07:00-10:30.", "score": 0.9}
                    },
                }
            ]
        return {
            "source": "property_kb",
            "requirements": [query],
            "normalized_requirements": [query],
            "top_score": 0.9 if hotels else 0.0,
            "hotels": hotels,
        }


def _build(
    *,
    decompose: dict[str, Any],
    compound: _FakeCompound,
    property_kb: _FakePropertyKB | None,
) -> ConciergePipeline:
    return ConciergePipeline(
        session_store=SessionStore(),  # in-memory fallback
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify({"type": "none", "confidence": 0.1, "signal": None}),
            cache_get=None,
            cache_set=None,
        ),
        decomposer=QueryDecomposer(decompose_fn=_scripted_decompose(decompose)),
        triage=Triage(),
        router=SourceRouter(),
        compound=compound,
        property_kb=property_kb,
    )


_SCOPED_DECOMP = {
    "query_type": "scoped",
    "intent": "amenities",
    "requirements": ["breakfast time"],
    "language": "en",
}


@pytest.mark.asyncio
async def test_property_scope_reads_guide_not_listings() -> None:
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(decompose=_SCOPED_DECOMP, compound=compound, property_kb=kb)
    out = await p.run(utterance="What time is breakfast?", hotel_id=HOTEL)

    assert out["path"] == PATH_SCOPED
    assert out["retrieval"]["source"] == "property_kb"
    assert out["retrieval"]["hotels"][0]["hotel_id"] == HOTEL
    # Guide queried with the RAW utterance; travel listings untouched.
    assert kb.calls == [{"hotel_id": HOTEL, "query": "What time is breakfast?", "language": "en"}]
    assert compound.calls == []
    assert "Casa Dell Arte" in out["answer"]


@pytest.mark.asyncio
async def test_property_scope_collapses_hotel_resolution() -> None:
    """A name mention can't trigger portfolio resolution — one property only."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    decomp = dict(_SCOPED_DECOMP, hotel_mention="Some Other Hotel")
    p = _build(decompose=decomp, compound=compound, property_kb=kb)
    out = await p.run(utterance="Tell me about Some Other Hotel", hotel_id=HOTEL)

    assert out["path"] == PATH_SCOPED
    assert out["router"]["reason"] == "property_scope"
    assert len(kb.calls) == 1 and kb.calls[0]["hotel_id"] == HOTEL
    assert compound.calls == []


@pytest.mark.asyncio
async def test_property_scope_broad_query_uses_guide() -> None:
    """Recommendation-shaped questions still answer from the guide."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    decomp = {
        "query_type": "broad",
        "intent": "recommendation",
        "requirements": ["spa"],
        "language": "en",
    }
    p = _build(decompose=decomp, compound=compound, property_kb=kb)
    out = await p.run(utterance="Do you have a spa?", hotel_id=HOTEL)

    assert out["path"] == PATH_BROAD
    assert out["retrieval"]["source"] == "property_kb"
    assert compound.calls == []


@pytest.mark.asyncio
async def test_property_scope_guide_miss_fails_closed() -> None:
    """No guide chunks → no-match answer; never rescued from travel listings."""
    compound, kb = _FakeCompound(), _FakePropertyKB(empty=True)
    p = _build(decompose=_SCOPED_DECOMP, compound=compound, property_kb=kb)
    out = await p.run(utterance="Do you have a helipad?", hotel_id=HOTEL)

    assert out["retrieval"]["hotels"] == []
    assert compound.calls == []  # no semantic fallback across the boundary
    assert out["answer"]  # the localized no-match reply


@pytest.mark.asyncio
async def test_no_scope_keeps_travel_agent_behaviour() -> None:
    """hotel_id absent → the property KB is never consulted."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    decomp = {
        "query_type": "broad",
        "intent": "recommendation",
        "region": "antalya",
        "requirements": ["spa"],
        "language": "en",
    }
    p = _build(decompose=decomp, compound=compound, property_kb=kb)
    out = await p.run(utterance="Find me a spa hotel in Antalya")

    assert kb.calls == []
    assert len(compound.calls) >= 1
    assert out["path"] == PATH_BROAD


@pytest.mark.asyncio
async def test_property_scope_pins_session_hotel() -> None:
    """The session is pinned to the property so follow-ups stay scoped."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify({"type": "none", "confidence": 0.1, "signal": None}),
            cache_get=None,
            cache_set=None,
        ),
        decomposer=QueryDecomposer(decompose_fn=_scripted_decompose(_SCOPED_DECOMP)),
        triage=Triage(),
        router=SourceRouter(),
        compound=compound,
        property_kb=kb,
    )
    out = await p.run(utterance="What time is breakfast?", session_id="s-1", hotel_id=HOTEL)
    sess = await store.load(out["session_id"])
    assert sess.get("active_hotel_id") == HOTEL


# ---------- PropertyKBRetriever shaping (inner retriever mocked) ----------


@pytest.mark.asyncio
async def test_property_kb_retriever_shapes_chunks(monkeypatch, tmp_path) -> None:
    from voxtera.call_center.property_kb import PropertyKBRetriever
    from voxtera.rag.retriever import RetrievedChunk, Retriever

    async def fake_retrieve(self, *, hotel_id: str, query: str, language=None):  # noqa: ANN001
        return [
            RetrievedChunk(
                text="Breakfast 07:00-10:30.", score=0.91, doc_id="d1", category="dining"
            ),
            RetrievedChunk(text="Pool opens at 08:00.", score=0.55, doc_id="d2", category="dining"),
        ]

    monkeypatch.setattr(Retriever, "retrieve", fake_retrieve)
    kb = PropertyKBRetriever(db_path=tmp_path / "guide.db")
    out = await kb.retrieve(hotel_id="demo", query="when is breakfast?")

    assert out["source"] == "property_kb"
    assert out["top_score"] == pytest.approx(0.91)
    (hotel,) = out["hotels"]
    assert hotel["hotel_id"] == "demo"
    # Duplicate categories both kept, under disambiguated labels.
    assert set(hotel["evidence"]) == {"dining", "dining_2"}
    assert hotel["evidence"]["dining"]["text"].startswith("Breakfast")


@pytest.mark.asyncio
async def test_property_kb_retriever_empty_guide(monkeypatch, tmp_path) -> None:
    from voxtera.call_center.property_kb import PropertyKBRetriever
    from voxtera.rag.retriever import Retriever

    async def fake_retrieve(self, *, hotel_id: str, query: str, language=None):  # noqa: ANN001
        return []

    monkeypatch.setattr(Retriever, "retrieve", fake_retrieve)
    kb = PropertyKBRetriever(db_path=tmp_path / "guide.db")
    out = await kb.retrieve(hotel_id="demo", query="anything")
    assert out["hotels"] == [] and out["top_score"] == 0.0
