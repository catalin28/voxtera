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
from voxtera.call_center.router import PATH_SCOPED, SourceRouter
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
async def test_property_turn_never_calls_classifier() -> None:
    """No escalation classifier on the property path (legacy parity, −0.6-0.9s
    per turn). Urgent issues are tickets via the render's create_ticket tool,
    not an unconnected hand-off line."""

    async def forbidden_classify(_u: str) -> dict[str, Any]:
        raise AssertionError("classifier must not run on the property fast path")

    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = ConciergePipeline(
        session_store=SessionStore(),
        classifier=EscalationClassifier(
            classify_fn=forbidden_classify, cache_get=None, cache_set=None
        ),
        decomposer=QueryDecomposer(decompose_fn=_forbidden_decompose()),
        triage=Triage(),
        router=SourceRouter(),
        compound=compound,
        property_kb=kb,
    )
    out = await p.run(utterance="I am locked out of my room!", hotel_id=HOTEL)

    # Handled by the (stubbed) render — no escalation short-circuit.
    assert out["path"] == PATH_SCOPED
    assert out["reason"] == "property_fast"
    assert "classify_ms" not in out["timings"]


# ---------- KB-offer follow-up ("…shall I get the menu?" → "yes") ----------


@pytest.mark.asyncio
async def test_kb_offer_yes_reruns_original_question(monkeypatch) -> None:
    """A bare "yes" accepting a guide-fetch offer must re-run the ORIGINAL
    question through KB retrieval — not embed the literal "yes" (which matches
    nothing and makes the render fabricate "I don't have the menu")."""

    # Turn 1 offers to fetch; turn 2 answers. Scripted by call order.
    calls: list[dict[str, Any]] = []

    async def fake_render(*, payload, hotel_id, ticketer, model, on_delta=None, client=None):
        calls.append(payload)
        if len(calls) == 1:
            answer = (
                "I don't have the dinner menu to hand — let me check with the "
                "kitchen and come back to you with the details."
            )
        else:
            answer = "Here is the Tuğra menu: " + ", ".join(
                (h.get("evidence") or {}).get("dining", {}).get("text", "")
                for h in (payload.get("retrieval") or {}).get("hotels", [])
            )
        if on_delta is not None:
            await on_delta(answer)
        return {"answer": answer, "ticket": None}

    monkeypatch.setattr("voxtera.call_center.property_render.render_property_turn", fake_render)

    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)

    q = "Do you have the dinner menu for Tuğra?"
    out1 = await p.run(utterance=q, session_id="menu-1", hotel_id=HOTEL)
    assert out1["reason"] == "property_fast"
    # Turn-1 guide query is the real question.
    assert kb.calls[0]["query"] == q

    # Turn 2: bare affirmation accepting the fetch offer.
    out2 = await p.run(utterance="Yes.", session_id="menu-1", hotel_id=HOTEL)
    assert out2["reason"] == "property_fast"
    # The re-run queried the ORIGINAL question, NOT "Yes." — this is the fix.
    assert kb.calls[1]["query"] == q
    assert kb.calls[1]["query"].lower() != "yes."
    # The render received evidence and answered the menu.
    assert "menu" in out2["answer"].lower()
    # The guest's literal turn is still recorded in history.
    assert out2["utterance"] == "Yes."


@pytest.mark.asyncio
async def test_bare_yes_without_offer_does_not_rerun(monkeypatch) -> None:
    """A "yes" that does NOT follow a guide-fetch offer must behave normally —
    the KB query is the literal utterance, no re-run hijack."""

    async def fake_render(*, payload, hotel_id, ticketer, model, on_delta=None, client=None):
        # Plain answer with no fetch offer → pending_kb_offer never armed.
        answer = "The breakfast buffet runs until 10:30."
        if on_delta is not None:
            await on_delta(answer)
        return {"answer": answer, "ticket": None}

    monkeypatch.setattr("voxtera.call_center.property_render.render_property_turn", fake_render)

    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)

    await p.run(utterance="What time is breakfast?", session_id="b-1", hotel_id=HOTEL)
    await p.run(utterance="Yes.", session_id="b-1", hotel_id=HOTEL)
    # No offer armed → second turn queries the literal "Yes.", not the prior Q.
    assert kb.calls[1]["query"] == "Yes."


# ---------- One-brain ticket flow (create_ticket tool on the render) ----------


@pytest.fixture(autouse=True)
def _stub_property_render(monkeypatch):
    """Offline stand-in for the tool-capable property render.

    Mimics the plain-render fallback ("Top matches: <names>.") so the
    retrieval-behaviour tests above keep their assertions; individual tests
    re-patch it to script ticket outcomes.
    """

    async def fake(*, payload, hotel_id, ticketer, model, on_delta=None, client=None):
        names = ", ".join(
            (h.get("payload") or {}).get("hotel_name", h.get("hotel_id"))
            for h in (payload.get("retrieval") or {}).get("hotels", [])[:3]
        )
        answer = f"Top matches: {names}." if names else "I don't have that to hand."
        if on_delta is not None:
            await on_delta(answer)
        return {"answer": answer, "ticket": None}

    monkeypatch.setattr("voxtera.call_center.property_render.render_property_turn", fake)


@pytest.mark.asyncio
async def test_booking_request_goes_to_render_not_escalation(monkeypatch) -> None:
    """A 'booking' classifier verdict no longer short-circuits — the render
    LLM (which holds create_ticket) owns the whole flow."""

    async def fake_render(**_kw):
        return {
            "answer": "For what time, and how many people will be dining?",
            "ticket": None,
        }

    monkeypatch.setattr("voxtera.call_center.property_render.render_property_turn", fake_render)
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(
        compound=compound,
        property_kb=kb,
        classify={"type": "booking", "confidence": 0.9, "signal": "book a table"},
    )
    out = await p.run(utterance="I want to book a table at La Petite Arras.", hotel_id=HOTEL)

    assert out["path"] == PATH_SCOPED  # not escalate
    assert out["reason"] == "property_fast"
    assert "how many people" in out["answer"]


@pytest.mark.asyncio
async def test_render_filed_ticket_lands_in_result(monkeypatch) -> None:
    async def fake_render(**_kw):
        return {
            "answer": "Done — the Restaurant team has been notified.",
            "ticket": {"session_id": "vox-9", "category": "Restaurant"},
        }

    monkeypatch.setattr("voxtera.call_center.property_render.render_property_turn", fake_render)
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)
    out = await p.run(utterance="Yes, send it.", session_id="tk-1", hotel_id=HOTEL)

    assert out["reason"] == "property_ticket"
    assert out["escalation"]["ticket"]["session_id"] == "vox-9"
    assert "notified" in out["answer"]


@pytest.mark.asyncio
async def test_render_failure_falls_back_to_plain_render(monkeypatch) -> None:
    async def broken_render(**_kw):
        raise RuntimeError("anthropic down")

    monkeypatch.setattr("voxtera.call_center.property_render.render_property_turn", broken_render)
    compound, kb = _FakeCompound(), _FakePropertyKB()
    p = _build(compound=compound, property_kb=kb)
    out = await p.run(utterance="What time is breakfast?", hotel_id=HOTEL)

    # Plain render fallback (render_fn=None → deterministic names line).
    assert out["reason"] == "property_fast"
    assert "Casa Dell Arte" in out["answer"]


# ---------- render_property_turn unit tests (fake Anthropic client) ----------


from types import SimpleNamespace  # noqa: E402

from voxtera.call_center.property_render import (  # noqa: E402
    render_property_turn as _real_render_property_turn,
)


class _FakeStream:
    def __init__(self, deltas, final):
        self._deltas, self._final = deltas, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return self._final


class _FakeAnthropic:
    """messages.stream(**kwargs) fed from a scripted list of rounds."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls: list[dict] = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        deltas, final = self._rounds.pop(0)
        return _FakeStream(deltas, final)


def _hotel_config():
    from voxtera.actions.hotel_config import HotelConfig
    from voxtera.actions.ticket import Category

    return HotelConfig(
        hotel_id="demo",
        hotel_name="Grand Hôtel Lumière",
        official_language="French",
        telegram_channel_id="-100",
        allowed_categories=(Category.RESERVATION, Category.RESTAURANT, Category.OTHER),
    )


class _FakeRuntimeTicketer:
    def __init__(self, *, file_result):
        self._file_result = file_result
        self.file_calls: list[dict] = []
        self._rt = SimpleNamespace(hotel_config=_hotel_config())

    def runtime(self, _hotel_id):
        return self._rt

    async def file(self, **kwargs):
        self.file_calls.append(kwargs)
        return self._file_result


def _payload(utterance="Book a table at 8 for two, room 412."):
    return {
        "utterance": utterance,
        "region": None,
        "decomposition": {"language": "en"},
        "retrieval": {"hotels": []},
        "transcript": "",
        "brief": True,
    }


_TOOL_ARGS = {
    "category": "Restaurant",
    "summary": "Table pour deux à 20h, chambre 412",
    "room_number": "412",
    "original_quote": "Book a table at 8 for two, room 412.",
    "language_detected": "English",
}


@pytest.mark.asyncio
async def test_render_tool_loop_files_and_confirms() -> None:
    tool_use = SimpleNamespace(type="tool_use", id="tu1", input=dict(_TOOL_ARGS))
    client = _FakeAnthropic(
        [
            (["One moment. "], SimpleNamespace(stop_reason="tool_use", content=[tool_use])),
            (["Done — the team is notified."], SimpleNamespace(stop_reason="end_turn", content=[])),
        ]
    )
    ticketer = _FakeRuntimeTicketer(file_result={"session_id": "vox-7", "category": "Restaurant"})
    deltas: list[str] = []

    async def on_delta(d):
        deltas.append(d)

    out = await _real_render_property_turn(
        payload=_payload(),
        hotel_id="demo",
        ticketer=ticketer,
        model="m",
        on_delta=on_delta,
        client=client,
    )
    assert out["ticket"] == {"session_id": "vox-7", "category": "Restaurant"}
    assert out["answer"].startswith("One moment.")
    assert out["answer"].endswith("notified.")
    assert "".join(deltas).startswith("One moment. ")
    # Round 1 advertised the tool; round 2 carried tool_result + tool_choice none.
    assert client.calls[0]["tools"][0]["name"] == "create_ticket"
    assert client.calls[1]["tool_choice"] == {"type": "none"}
    assert client.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"
    # System prompt carries the legacy confirmation rules.
    assert "confirmation rule" in client.calls[0]["system"][0]["text"]
    (filed,) = ticketer.file_calls
    assert filed["fields"]["room_number"] == "412"


@pytest.mark.asyncio
async def test_render_invalid_tool_args_get_rejected_payload() -> None:
    bad = dict(_TOOL_ARGS)
    bad.pop("room_number")
    tool_use = SimpleNamespace(type="tool_use", id="tu1", input=bad)
    client = _FakeAnthropic(
        [
            ([""], SimpleNamespace(stop_reason="tool_use", content=[tool_use])),
            (
                ["Could you give me your room number?"],
                SimpleNamespace(stop_reason="end_turn", content=[]),
            ),
        ]
    )
    ticketer = _FakeRuntimeTicketer(file_result=None)
    out = await _real_render_property_turn(
        payload=_payload(), hotel_id="demo", ticketer=ticketer, model="m", client=client
    )
    assert out["ticket"] is None
    assert ticketer.file_calls == []  # validation rejected before delivery
    result_json = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "rejected" in result_json and "room_number" in result_json


@pytest.mark.asyncio
async def test_render_without_ticket_layer_has_no_tool_and_no_promises_rule() -> None:
    client = _FakeAnthropic(
        [
            (
                ["The front desk can arrange that for you."],
                SimpleNamespace(stop_reason="end_turn", content=[]),
            )
        ]
    )
    out = await _real_render_property_turn(
        payload=_payload(), hotel_id="demo", ticketer=None, model="m", client=client
    )
    assert out["ticket"] is None
    assert "tools" not in client.calls[0]
    assert "You CANNOT perform actions" in client.calls[0]["system"][0]["text"]


def test_validate_ticket_args_rules() -> None:
    from voxtera.call_center.property_render import _validate_ticket_args

    cfg = _hotel_config()
    fields = _validate_ticket_args(dict(_TOOL_ARGS), cfg)
    assert fields["category"] == "Restaurant"

    import pytest as _pytest

    with _pytest.raises(ValueError, match="room_number"):
        _validate_ticket_args({**_TOOL_ARGS, "room_number": "  "}, cfg)
    with _pytest.raises(ValueError, match="category"):
        _validate_ticket_args({**_TOOL_ARGS, "category": "Maintenance"}, cfg)  # not allowed here
