"""Phase 6 — end-to-end verification of the concierge fixes.

This pins the parts of Phases 1–5 that the rest of the suite left unguarded:

  * P0 leak guards (stop_sequences + a turn-sized max_tokens) actually reach
    the Anthropic call — the top-priority fix had no test.
  * A whole restaurant booking runs through the REAL ConciergePipeline +
    REAL render_property_turn (only the Anthropic client and the network
    leaves are faked), proving the Phase 4 booking guidance + hotel-local
    time anchor reach the model every turn and a confirmed ticket lands.

What this canNOT verify offline (documented in docs/call-center/
phase6-verification.md): whether the LLM actually OBEYS those prompt rules,
plus latency and leakage-guard frame counts — those need a live audio replay
against the running stack.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from voxtera.actions.hotel_config import HotelConfig
from voxtera.actions.ticket import Category
from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.concierge import LLM_STOP_SEQUENCES
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.pipeline import ConciergePipeline
from voxtera.call_center.property_render import render_property_turn
from voxtera.call_center.router import PATH_SCOPED, SourceRouter
from voxtera.call_center.session import SessionStore
from voxtera.call_center.triage import Triage

HOTEL = "kempinski_ciragan"  # real config → Europe/Istanbul timezone


# --------------------------- fake Anthropic client --------------------------


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
    """Serves scripted (deltas, final) rounds in order across the whole call."""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls: list[dict] = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        deltas, final = self._rounds.pop(0)
        return _FakeStream(deltas, final)


def _hotel_config():
    return HotelConfig(
        hotel_id=HOTEL,
        hotel_name="Çırağan Palace Kempinski Istanbul",
        official_language="tr",
        telegram_channel_id="-100",
        allowed_categories=(Category.RESTAURANT, Category.CONCIERGE, Category.OTHER),
        timezone="Europe/Istanbul",
    )


class _FakeTicketer:
    def __init__(self, *, file_result):
        self._rt = SimpleNamespace(hotel_config=_hotel_config())
        self._file_result = file_result
        self.file_calls: list[dict] = []

    def runtime(self, _hotel_id):
        return self._rt

    async def file(self, **kwargs):
        self.file_calls.append(kwargs)
        return self._file_result


def _payload(brief: bool = True) -> dict[str, Any]:
    return {
        "utterance": "I'd like to book a table tomorrow at seven.",
        "region": None,
        "decomposition": {"language": "en"},
        "retrieval": {"hotels": []},
        "transcript": "",
        "brief": brief,
        "hotel_timezone": "Europe/Istanbul",
    }


def _end_turn(text: str):
    return ([text], SimpleNamespace(stop_reason="end_turn", content=[]))


# ----------------------------- P0 leak guards -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("brief,expected_max", [(True, 140), (False, 512)])
async def test_render_applies_stop_sequences_and_turn_sized_max_tokens(brief, expected_max) -> None:
    """Every guest-facing render call carries the P0 scaffold-leak guards."""
    client = _FakeAnthropic([_end_turn("How may I help?")])
    await render_property_turn(
        payload=_payload(brief=brief),
        hotel_id=HOTEL,
        ticketer=_FakeTicketer(file_result=None),
        model="m",
        client=client,
    )
    call = client.calls[0]
    assert call["stop_sequences"] == LLM_STOP_SEQUENCES
    assert call["max_tokens"] == expected_max


@pytest.mark.asyncio
async def test_no_actions_render_still_guarded() -> None:
    """The no-ticketing render path keeps the stop-sequence guard too."""
    client = _FakeAnthropic([_end_turn("The front desk can help with that.")])
    await render_property_turn(
        payload=_payload(), hotel_id=HOTEL, ticketer=None, model="m", client=client
    )
    assert client.calls[0]["stop_sequences"] == LLM_STOP_SEQUENCES


# --------------- end-to-end booking through the real pipeline ----------------


class _FakePropertyKB:
    async def retrieve(self, *, hotel_id, query, language=None):
        return {
            "source": "property_kb",
            "requirements": [query],
            "normalized_requirements": [query],
            "top_score": 0.9,
            "hotels": [
                {
                    "hotel_id": hotel_id,
                    "score": 0.9,
                    "payload": {"hotel_name": "Çırağan Palace Kempinski Istanbul"},
                    "evidence": {"dining": {"text": "Tuğra serves Ottoman cuisine.", "score": 0.9}},
                }
            ],
        }


async def _forbidden_decompose(_u, _c):
    raise AssertionError("decomposer must not run on the property fast path")


async def _forbidden_classify(_u):
    raise AssertionError("classifier must not run on the property fast path")


def _pipeline(
    ticketer, fake_client, monkeypatch, *, extracted=None
) -> tuple[ConciergePipeline, SessionStore]:
    # Drive the REAL render_property_turn but with a fake Anthropic client.
    monkeypatch.setattr(
        "voxtera.call_center.property_render._anthropic", lambda: fake_client
    )

    # The parallel slot extractor would otherwise hit the network — stub it.
    # Default: no-op (returns prior slots). Pass `extracted` to script a result.
    async def _fake_extract(**kwargs):
        if extracted is not None:
            return dict(extracted)
        return kwargs.get("prior_slots") or {}

    monkeypatch.setattr(
        "voxtera.call_center.booking_extract.extract_booking_slots", _fake_extract
    )
    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(
            classify_fn=_forbidden_classify, cache_get=None, cache_set=None
        ),
        decomposer=QueryDecomposer(decompose_fn=_forbidden_decompose),
        triage=Triage(),
        router=SourceRouter(),
        property_kb=_FakePropertyKB(),
        property_ticketer=ticketer,
    )
    return p, store


@pytest.mark.asyncio
async def test_booking_flow_end_to_end_through_pipeline(monkeypatch) -> None:
    """A two-turn restaurant booking through ConciergePipeline.run with the REAL
    property render: the booking guidance + hotel-local time anchor reach the
    model on turn 1, and a confirmed ticket lands on turn 2."""
    tool_use = SimpleNamespace(
        type="tool_use",
        id="tu1",
        input={
            "category": "Restaurant",
            "summary": "Yarın akşam saat 19:00, iki kişilik masa, oda 412",
            "room_number": "412",
            "original_quote": "In-house, room 412, a table for two under Dan.",
            "language_detected": "English",
        },
    )
    # Turn 1: ask the qualifying question. Turns 2: file then confirm (2 rounds).
    client = _FakeAnthropic(
        [
            _end_turn("Will you be dining as an in-house guest, or joining us from outside?"),
            (["One moment. "], SimpleNamespace(stop_reason="tool_use", content=[tool_use])),
            _end_turn("All set — the restaurant team has been notified."),
        ]
    )
    ticketer = _FakeTicketer(file_result={"session_id": "vox-42", "category": "Restaurant"})
    p, _store = _pipeline(ticketer, client, monkeypatch)
    sid = "book-e2e"

    out1 = await p.run(
        utterance="I'd like to book a table tomorrow at seven.",
        session_id=sid,
        hotel_id=HOTEL,
        brief=True,
    )
    assert out1["path"] == PATH_SCOPED
    # The Phase 4 booking guidance rode in the (cached) system prompt...
    sys_text = client.calls[0]["system"][0]["text"]
    assert "BOOKING FLOW" in sys_text
    assert "never ask an external visitor for a room number" in sys_text.lower()
    # ...and the hotel-local time anchor rode on the current user message,
    # carrying the REAL timezone resolved from config/hotels/kempinski_ciragan.yaml.
    user_msg = client.calls[0]["messages"][-1]["content"]
    assert "Europe/Istanbul" in user_msg
    assert "do not read this aloud" in user_msg

    out2 = await p.run(
        utterance="In-house, room 412, a table for two under Dan.",
        session_id=sid,
        hotel_id=HOTEL,
        brief=True,
    )
    assert out2["reason"] == "property_ticket"
    assert out2["escalation"]["ticket"]["session_id"] == "vox-42"
    assert "notified" in out2["answer"].lower()
    # The ticket was actually filed via the (fake) ticketer with the room number.
    (filed,) = ticketer.file_calls
    assert filed["fields"]["room_number"] == "412"


@pytest.mark.asyncio
async def test_booking_slots_persist_and_lock_across_turns(monkeypatch) -> None:
    """The slot-drift fix end-to-end: turn 1 the parallel extractor reports the
    venue + guest_type; Python persists them; turn 2 the LOCKED recap is fed
    back so the model can't re-ask or swap the venue."""
    client = _FakeAnthropic([_end_turn("And your name?"), _end_turn("Thank you, Daniel.")])
    slots = {"restaurant": "Tuğra", "guest_type": "external", "party_size": "2"}
    p, store = _pipeline(_FakeTicketer(file_result=None), client, monkeypatch, extracted=slots)
    sid = "slots-persist"

    await p.run(
        utterance="A table for two at Tuğra — we're outside guests.",
        session_id=sid,
        hotel_id=HOTEL,
        brief=True,
    )
    sess = await store.load(sid)
    assert sess["booking_slots"]["restaurant"] == "Tuğra"
    assert sess["booking_slots"]["guest_type"] == "external"

    await p.run(utterance="Daniel.", session_id=sid, hotel_id=HOTEL, brief=True)
    # Turn 1 = one render call; turn 2 = the second.
    turn2_user_msg = client.calls[1]["messages"][-1]["content"]
    assert "LOCKED" in turn2_user_msg
    assert "Tuğra" in turn2_user_msg  # the venue is pinned — no drift possible
