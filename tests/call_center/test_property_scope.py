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


def _forbidden_decompose():
    """Property fast path must never pay the decompose LLM call."""

    async def fn(_u: str, _c: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("decomposer must not run on the property fast path")

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
    compound: _FakeCompound,
    property_kb: _FakePropertyKB | None,
    decompose: dict[str, Any] | None = None,
    classify: dict[str, Any] | None = None,
) -> ConciergePipeline:
    decompose_fn = (
        _scripted_decompose(decompose) if decompose is not None else _forbidden_decompose()
    )
    return ConciergePipeline(
        session_store=SessionStore(),  # in-memory fallback
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify(
                classify or {"type": "none", "confidence": 0.1, "signal": None}
            ),
            cache_get=None,
            cache_set=None,
        ),
        decomposer=QueryDecomposer(decompose_fn=decompose_fn),
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
    """Fast path: guide retrieval + render only — NO decompose LLM call."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)  # decomposer forbidden
    out = await p.run(utterance="What time is breakfast?", hotel_id=HOTEL)

    assert out["path"] == PATH_SCOPED
    assert out["reason"] == "property_fast"
    assert out["retrieval"]["source"] == "property_kb"
    assert out["retrieval"]["hotels"][0]["hotel_id"] == HOTEL
    # Guide queried with the RAW utterance; travel listings untouched.
    assert kb.calls == [{"hotel_id": HOTEL, "query": "What time is breakfast?", "language": None}]
    assert compound.calls == []
    assert "Casa Dell Arte" in out["answer"]


@pytest.mark.asyncio
async def test_property_scope_never_resolves_other_hotels() -> None:
    """A name mention can't trigger portfolio resolution — one property only."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)
    out = await p.run(utterance="Tell me about Some Other Hotel", hotel_id=HOTEL)

    assert out["path"] == PATH_SCOPED
    assert out["router"]["reason"] == "property_fast"
    assert len(kb.calls) == 1 and kb.calls[0]["hotel_id"] == HOTEL
    assert compound.calls == []


@pytest.mark.asyncio
async def test_property_scope_recommendation_uses_guide() -> None:
    """Recommendation-shaped questions still answer from the guide."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)
    out = await p.run(utterance="Do you have a spa?", hotel_id=HOTEL)

    assert out["path"] == PATH_SCOPED
    assert out["retrieval"]["source"] == "property_kb"
    assert compound.calls == []


@pytest.mark.asyncio
async def test_property_scope_guide_miss_fails_closed() -> None:
    """No guide chunks → no-match answer; never rescued from travel listings."""
    compound, kb = _FakeCompound(), _FakePropertyKB(empty=True)
    p = _build(compound=compound, property_kb=kb)
    out = await p.run(utterance="Do you have a helipad?", hotel_id=HOTEL)

    assert out["retrieval"]["hotels"] == []
    assert compound.calls == []  # no semantic fallback across the boundary
    assert out["answer"]  # the localized no-match reply


@pytest.mark.asyncio
async def test_property_scope_escalation_still_works() -> None:
    """The classifier runs concurrently and still gates the fast path."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(
        compound=compound,
        property_kb=kb,
        classify={"type": "live_complaint", "confidence": 0.95, "signal": "locked out"},
    )
    out = await p.run(utterance="I am locked out of my room!", hotel_id=HOTEL)

    assert out["path"] == "escalate"
    assert "colleague" in out["answer"].lower()


# ---------- Telegram ticket creation (legacy create_ticket restored) ----------


class _FakeTicketer:
    def __init__(self, *, result: dict | None):
        self._result = result
        self.calls: list[dict] = []

    async def file_from_turn(self, **kwargs) -> dict | None:
        self.calls.append(kwargs)
        return self._result


_ESCALATE = {"type": "booking", "confidence": 0.9, "signal": "book a massage"}


@pytest.mark.asyncio
async def test_actionable_turn_files_telegram_ticket() -> None:
    """Escalated hotel-mode turns create a ticket and confirm it to the guest."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    ticketer = _FakeTicketer(result={"session_id": "vox-1", "category": "Reservation"})
    p = _build(compound=compound, property_kb=kb, classify=_ESCALATE)
    p._property_ticketer = ticketer
    out = await p.run(
        utterance="Can you book me a massage for tonight?", session_id="tk-1", hotel_id=HOTEL
    )

    assert out["reason"] == "property_ticket"
    assert out["escalation"]["ticket"] == {"session_id": "vox-1", "category": "Reservation"}
    assert "team" in out["answer"].lower()  # confirms staff were notified
    assert ticketer.calls[0]["hotel_id"] == HOTEL
    assert "massage" in ticketer.calls[0]["utterance"]
    assert "ticket_ms" in out["timings"]


@pytest.mark.asyncio
async def test_ticket_failure_falls_back_to_handoff_line() -> None:
    """If delivery fails, the bot must NOT claim staff were notified."""
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb, classify=_ESCALATE)
    p._property_ticketer = _FakeTicketer(result=None)
    out = await p.run(utterance="Can you book me a massage?", hotel_id=HOTEL)

    assert out["reason"] == "escalation_classifier"
    assert out["escalation"]["ticket"] is None
    assert "colleague" in out["answer"].lower()


@pytest.mark.asyncio
async def test_no_ticketer_keeps_old_escalation_behaviour() -> None:
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb, classify=_ESCALATE)
    out = await p.run(utterance="Can you book me a massage?", hotel_id=HOTEL)
    assert out["reason"] == "escalation_classifier"
    assert "colleague" in out["answer"].lower()


@pytest.mark.asyncio
async def test_ticketer_field_extraction_fallback(monkeypatch) -> None:
    """LLM extraction failure still files a usable ticket (fallback fields)."""
    from voxtera.actions.hotel_config import HotelConfig
    from voxtera.actions.ticket import Category
    from voxtera.call_center.property_actions import PropertyTicketer

    sent: list = []

    class _FakeSink:
        async def send(self, ticket) -> bool:  # noqa: ANN001
            sent.append(ticket)
            return True

    class _FakeRuntime:
        hotel_config = HotelConfig(
            hotel_id="demo",
            hotel_name="Grand Hôtel Lumière",
            official_language="French",
            telegram_channel_id="-100",
            allowed_categories=(Category.RESERVATION, Category.OTHER),
        )
        sink = _FakeSink()

    ticketer = PropertyTicketer()
    ticketer._runtimes["demo"] = _FakeRuntime()  # skip Telegram bootstrap

    async def _boom(**_kw):
        raise RuntimeError("anthropic down")

    monkeypatch.setattr(
        "voxtera.call_center.clients.anthropic_client",
        lambda: (_ for _ in ()).throw(RuntimeError("anthropic down")),
    )
    out = await ticketer.file_from_turn(
        hotel_id="demo",
        utterance="Can you book me a massage for 7pm? Room 412.",
        transcript="User: hi",
        language="en",
    )
    assert out is not None and out["category"] in ("Other", "Reservation")
    (ticket,) = sent
    assert ticket.original_quote.startswith("Can you book me a massage")
    assert ticket.summary  # fallback summary = raw utterance


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
        decomposer=QueryDecomposer(decompose_fn=_forbidden_decompose()),
        triage=Triage(),
        router=SourceRouter(),
        compound=compound,
        property_kb=kb,
    )
    out = await p.run(utterance="What time is breakfast?", session_id="s-1", hotel_id=HOTEL)
    sess = await store.load(out["session_id"])
    assert sess.get("active_hotel_id") == HOTEL
    # Transcript memory works without decompose: the turn was appended.
    assert [t.get("utterance") for t in sess.get("history") or []] == ["What time is breakfast?"]


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
