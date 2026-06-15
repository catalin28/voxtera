"""Phase 7 — hotel-stay booking on the TRAVEL-AGENCY path.

The travel agent can book a HOTEL STAY (never a restaurant/spa — that is a
property-mode action). Same prompt-guided design as Phase 4: the LLM owns the
booking and files via the book_hotel_stay tool; Python supplies the rules, the
clock and a silent parallel slot extractor. No network: a fake Anthropic client
drives render_travel_turn and a fake extract_fn drives the extractor.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voxtera.actions.ticket import Category
from voxtera.call_center.booking_extract import extract_stay_slots
from voxtera.call_center.travel_booking import (
    HOTEL_STAY,
    STAY_SLOT_KEYS,
    stay_guidance_block,
    stay_recap,
)
from voxtera.call_center.travel_render import (
    _validate_stay_args,
    render_travel_turn,
)

# ------------------------------ schema + recap ------------------------------


def test_stay_slot_keys_match_schema() -> None:
    # Every mandatory schema slot is a tracked key (single source of truth).
    for slot in HOTEL_STAY.mandatory_slots:
        assert slot.key in STAY_SLOT_KEYS
    assert "contact" in STAY_SLOT_KEYS and "check_in" in STAY_SLOT_KEYS


def test_stay_guidance_block_encodes_the_rules() -> None:
    block = stay_guidance_block().lower()
    # The hard boundary: a travel agent books a stay only, never restaurant/spa.
    assert "hotel stay" in block
    assert "cannot" in block and "restaurant" in block and "spa" in block
    # External client → phone/email, never a room number.
    assert "phone" in block or "email" in block
    assert "never ask for a room number" in block
    # one slot per turn, absolute dates, locked recap, confirm-before-file.
    assert "one" in block
    assert "absolute" in block
    assert "locked" in block
    assert "confirm" in block


def test_stay_recap_empty_and_locks_in_order() -> None:
    assert stay_recap(None) == ""
    assert stay_recap({"hotel": "  "}) == ""
    r = stay_recap({"name": "Daniel", "hotel": "Crystal Tat Beach", "check_in": "Fri 19 June"})
    assert "LOCKED" in r
    assert "Crystal Tat Beach" in r and "Daniel" in r
    # hotel ordered before check_in before name (STAY_SLOT_KEYS order).
    assert r.index("Crystal Tat Beach") < r.index("Fri 19 June") < r.index("Daniel")


# --------------------------- extract_stay_slots -----------------------------


@pytest.mark.asyncio
async def test_extract_stay_merges_and_coerces_to_stay_keys() -> None:
    async def fake(_msg: str):
        return {"check_in": "Fri 19 June", "guests": "2 adults", "restaurant": "ignored"}

    out = await extract_stay_slots(
        utterance="next Friday, two of us",
        history=None,
        prior_slots={"hotel": "Crystal Tat Beach"},
        extract_fn=fake,
    )
    assert out["hotel"] == "Crystal Tat Beach"  # carried forward
    assert out["check_in"] == "Fri 19 June" and out["guests"] == "2 adults"
    assert "restaurant" not in out  # not a stay key → dropped


@pytest.mark.asyncio
async def test_extract_stay_empty_when_not_a_booking() -> None:
    async def fake(_msg: str):
        return {}

    out = await extract_stay_slots(
        utterance="which hotels have a spa?", history=None, prior_slots=None, extract_fn=fake
    )
    assert out == {}


@pytest.mark.asyncio
async def test_extract_stay_never_raises_degrades_to_prior() -> None:
    async def boom(_msg: str):
        raise RuntimeError("anthropic down")

    out = await extract_stay_slots(
        utterance="x", history=None, prior_slots={"hotel": "X"}, extract_fn=boom
    )
    assert out == {"hotel": "X"}


# ------------------------------ arg validation ------------------------------


def _good_args() -> dict[str, str]:
    return {
        "hotel": "Crystal Tat Beach",
        "check_in": "Fri 19 June",
        "check_out": "Sun 21 June",
        "guests": "2 adults",
        "name": "Daniel",
        "contact": "555-1234",
        "language_detected": "en",
    }


def test_validate_stay_args_builds_reservation_ticket_fields() -> None:
    fields = _validate_stay_args(_good_args())
    assert fields["category"] == Category.RESERVATION.value
    # The stay details are stamped into the staff-facing summary.
    assert "Crystal Tat Beach" in fields["summary"]
    assert "Fri 19 June" in fields["summary"] and "Sun 21 June" in fields["summary"]
    assert "Daniel" in fields["summary"]
    # No room on a portfolio booking — the contact handle rides in room_number.
    assert fields["room_number"] == "555-1234"
    assert fields["language_detected"] == "en"


def test_validate_stay_args_rejects_missing_field() -> None:
    bad = _good_args()
    del bad["contact"]
    with pytest.raises(ValueError, match="contact"):
        _validate_stay_args(bad)


# --------------------- render_travel_turn (fake client) ---------------------


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
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls: list[dict] = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        deltas, final = self._rounds.pop(0)
        return _FakeStream(deltas, final)


class _FakeTicketer:
    def __init__(self, *, runtime=True):
        self._rt = SimpleNamespace() if runtime else None
        self.filed: list[dict] = []

    def runtime(self, _hotel_id):
        return self._rt

    async def file(self, **kwargs):
        self.filed.append(kwargs)
        return {"session_id": "vox-stay-1", "category": kwargs["fields"]["category"]}


def _payload(**over):
    p = {
        "utterance": "I'd like to book the Crystal Tat Beach for next Friday.",
        "region": None,
        "decomposition": {"language": "en"},
        "retrieval": {"hotels": []},
        "transcript": "",
        "brief": True,  # brief=True skips the image-catalog load (offline-safe)
        "hotel_timezone": "Europe/Istanbul",
    }
    p.update(over)
    return p


def _end_turn(deltas):
    return (deltas, SimpleNamespace(stop_reason="end_turn", content=[]))


def _tool_round(deltas, tool_input):
    tu = SimpleNamespace(type="tool_use", id="tu_1", input=tool_input)
    return (deltas, SimpleNamespace(stop_reason="tool_use", content=[tu]))


@pytest.mark.asyncio
async def test_stay_tool_and_guidance_and_anchor_injected_when_ticketing_on() -> None:
    client = _FakeAnthropic([_end_turn(["Which dates were you thinking?"])])
    out = await render_travel_turn(
        payload=_payload(stay_slots={"hotel": "Crystal Tat Beach"}),
        ticketer=_FakeTicketer(),
        model="m",
        client=client,
    )
    assert out["ticket"] is None
    call = client.calls[0]
    # The book_hotel_stay tool is offered.
    assert any(t["name"] == "book_hotel_stay" for t in call["tools"])
    # Stay rules ride in the cached system prompt.
    system_text = call["system"][0]["text"].lower()
    assert "hotel-stay booking" in system_text
    # Time anchor + LOCKED recap ride on the CURRENT user message.
    user_msg = call["messages"][-1]["content"]
    assert "Europe/Istanbul" in user_msg
    assert "LOCKED" in user_msg and "Crystal Tat Beach" in user_msg


@pytest.mark.asyncio
async def test_no_tool_and_no_promises_rule_without_ticketing() -> None:
    client = _FakeAnthropic([_end_turn(["Let me help you choose."])])
    out = await render_travel_turn(
        payload=_payload(),
        ticketer=_FakeTicketer(runtime=False),  # no channel → cannot book
        model="m",
        client=client,
    )
    assert out["ticket"] is None
    call = client.calls[0]
    assert "tools" not in call  # no tool offered
    assert "no reservation channel is connected" in call["system"][0]["text"].lower()


@pytest.mark.asyncio
async def test_confirmed_booking_files_a_reservation_ticket() -> None:
    ticketer = _FakeTicketer()
    client = _FakeAnthropic(
        [
            _tool_round([], _good_args()),  # model files on confirmation
            _end_turn(["All set — the agency will confirm your stay shortly."]),
        ]
    )
    out = await render_travel_turn(
        payload=_payload(utterance="yes, book it"),
        ticketer=ticketer,
        model="m",
        client=client,
    )
    assert out["ticket"] == {"session_id": "vox-stay-1", "category": Category.RESERVATION.value}
    assert "All set" in out["answer"]
    # Delivered as a Reservation, contact carried in room_number.
    assert len(ticketer.filed) == 1
    filed = ticketer.filed[0]["fields"]
    assert filed["category"] == Category.RESERVATION.value
    assert filed["room_number"] == "555-1234"
    # After filing, the model is barred from calling the tool again this turn.
    assert client.calls[1].get("tool_choice") == {"type": "none"}
