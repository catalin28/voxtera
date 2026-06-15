"""Phase 4 — booking flow (pure prompt-guided).

The LLM owns the booking conversation; Python only (a) gives the property
render the hotel-local time so relative dates resolve, and (b) injects the
booking-collection RULES (built from the declarative intent schemas) whenever
the property can file tickets. There is NO Python-side intent detection or
stage tracking by design — see docs/fix-plan/concierge-fix-plan.md Phase 4.

No network: a fake Anthropic client drives render_property_turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from voxtera.actions.hotel_config import HotelConfig, _coerce_timezone, load_hotel_config
from voxtera.actions.ticket import Category
from voxtera.call_center.hotel_time import hotel_local_now, hotel_time_note
from voxtera.call_center.intents import (
    RESTAURANT_BOOKING,
    SPA_BOOKING,
    booking_guidance_block,
    booking_recap,
)
from voxtera.call_center.property_render import (
    render_property_turn as _real_render_property_turn,
)

# --------------------------- HotelConfig.timezone ---------------------------


def test_shipped_configs_have_valid_timezones() -> None:
    """The two real hotel configs resolve to a usable IANA zone."""
    assert load_hotel_config("demo").timezone == "Europe/Paris"
    assert load_hotel_config("kempinski_ciragan").timezone == "Europe/Istanbul"


def test_coerce_timezone_accepts_valid_and_rejects_garbage(tmp_path) -> None:
    src = tmp_path / "x.yaml"
    assert _coerce_timezone("Europe/Istanbul", src) == "Europe/Istanbul"
    assert _coerce_timezone("  ", src) is None
    assert _coerce_timezone(None, src) is None
    # An unknown zone degrades to None (server clock) rather than failing load.
    assert _coerce_timezone("Mars/Olympus_Mons", src) is None


def test_bad_timezone_does_not_fail_config_load(tmp_path) -> None:
    cfg = tmp_path / "h.yaml"
    cfg.write_text(
        "hotel_name: Test\n"
        "official_language: en\n"
        "telegram_channel_id: '-100'\n"
        "timezone: Not/AZone\n"
        "allowed_categories: [Other]\n",
        encoding="utf-8",
    )
    loaded = load_hotel_config("h", config_dir=tmp_path)
    assert loaded.timezone is None  # degraded, but load succeeded


# ------------------------------- hotel_time ---------------------------------


def test_hotel_local_now_uses_zone_or_server_clock() -> None:
    _, source = hotel_local_now("Europe/Istanbul")
    assert source == "Europe/Istanbul"
    _, fallback = hotel_local_now(None)
    assert fallback == "server local time"
    _, bad = hotel_local_now("Not/AZone")
    assert bad == "server local time"


def test_hotel_time_note_is_context_only_and_instructs_absolute_dates() -> None:
    note = hotel_time_note("Europe/Istanbul")
    assert "do not read this aloud" in note
    assert "Europe/Istanbul" in note
    assert "absolute" in note.lower()


# --------------------------- booking_guidance_block -------------------------


def test_guidance_block_encodes_the_phase4_rules() -> None:
    block = booking_guidance_block().lower()
    # guest_type first + the hard contact rule.
    assert "in-house" in block and "external" in block
    assert "never ask an external visitor for a room number" in block
    # one slot per turn, absolute date, optional-last, confirm-before-file.
    assert "one" in block
    assert "absolute" in block
    assert "optional" in block
    assert "confirm" in block
    # slot-drift guard: treat the "Booking so far" recap as LOCKED.
    assert "booking so far" in block.lower()
    assert "locked" in block.lower()
    assert "never re-ask" in block


def test_guidance_block_covers_both_intents_from_schemas() -> None:
    block = booking_guidance_block()
    assert RESTAURANT_BOOKING.label in block
    assert SPA_BOOKING.label in block
    # Slot keys come straight from the declarative schemas (single source).
    for slot in RESTAURANT_BOOKING.mandatory_slots:
        assert slot.key in block
    assert "room number" in block.lower()  # in-house contact


# --------------------- property_render injection (fake client) --------------


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


def _hotel_config():
    return HotelConfig(
        hotel_id="demo",
        hotel_name="Grand Hôtel Lumière",
        official_language="French",
        telegram_channel_id="-100",
        allowed_categories=(Category.RESTAURANT, Category.CONCIERGE, Category.OTHER),
        timezone="Europe/Paris",
    )


class _FakeTicketer:
    def __init__(self):
        self._rt = SimpleNamespace(hotel_config=_hotel_config())

    def runtime(self, _hotel_id):
        return self._rt

    async def file(self, **kwargs):  # pragma: no cover - not exercised here
        return {"session_id": "vox-1", "category": "Restaurant"}


def _payload():
    return {
        "utterance": "I'd like to book a table tomorrow at 7pm.",
        "region": None,
        "decomposition": {"language": "en"},
        "retrieval": {"hotels": []},
        "transcript": "",
        "brief": True,
        "hotel_timezone": "Europe/Istanbul",
    }


def _one_round_client():
    return _FakeAnthropic(
        [(["Are you staying with us, or joining us from outside?"],
          SimpleNamespace(stop_reason="end_turn", content=[]))]
    )


@pytest.mark.asyncio
async def test_booking_guidance_and_time_anchor_injected_when_ticketing_on() -> None:
    client = _one_round_client()
    await _real_render_property_turn(
        payload=_payload(),
        hotel_id="demo",
        ticketer=_FakeTicketer(),
        model="m",
        client=client,
    )
    system_text = client.calls[0]["system"][0]["text"]
    # Booking rules ride in the cached system prompt.
    assert "BOOKING FLOW" in system_text
    assert "never ask an external visitor for a room number" in system_text.lower()
    # Time anchor rides on the CURRENT user message (changing timestamp →
    # must not bust the cached prefix), carrying the hotel's timezone.
    user_msg = client.calls[0]["messages"][-1]["content"]
    assert "Europe/Istanbul" in user_msg
    assert "do not read this aloud" in user_msg


# ------------------------- slot tracking (drift fix) ------------------------


def test_booking_recap_empty_when_no_slots() -> None:
    assert booking_recap(None) == ""
    assert booking_recap({}) == ""
    assert booking_recap({"restaurant": "", "name": "  "}) == ""


def test_booking_recap_locks_and_orders_slots() -> None:
    r = booking_recap({"party_size": "2", "restaurant": "Tuğra", "guest_type": "external"})
    assert "LOCKED" in r
    assert "Tuğra" in r and "external" in r and "2" in r
    # qualifying/identity ordered before logistics: guest_type < restaurant < party_size
    assert r.index("external") < r.index("Tuğra") < r.index("2")


@pytest.mark.asyncio
async def test_render_feeds_prior_slots_back_as_locked_recap() -> None:
    """Slots from a prior turn ride back into the model's user message, locked."""
    payload = _payload()
    payload["booking_slots"] = {"restaurant": "Tuğra", "guest_type": "external"}
    client = _one_round_client()
    await _real_render_property_turn(
        payload=payload, hotel_id="demo", ticketer=_FakeTicketer(), model="m", client=client
    )
    user_msg = client.calls[0]["messages"][-1]["content"]
    assert "LOCKED" in user_msg and "Tuğra" in user_msg


# --------------------- booking_extract (slot extractor) ---------------------


@pytest.mark.asyncio
async def test_extract_merges_known_slots_onto_prior() -> None:
    from voxtera.call_center.booking_extract import extract_booking_slots

    async def fake(_msg: str):
        return {"name": "Daniel", "contact": "555-1234", "junk": "ignored"}

    out = await extract_booking_slots(
        utterance="under Daniel, 555-1234",
        history=None,
        prior_slots={"restaurant": "Tuğra"},
        extract_fn=fake,
    )
    assert out["restaurant"] == "Tuğra"  # carried forward
    assert out["name"] == "Daniel" and out["contact"] == "555-1234"
    assert "junk" not in out  # coerced to known booking keys only


@pytest.mark.asyncio
async def test_extract_empty_when_not_a_booking() -> None:
    from voxtera.call_center.booking_extract import extract_booking_slots

    async def fake(_msg: str):
        return {}

    out = await extract_booking_slots(
        utterance="what time is breakfast?", history=None, prior_slots=None, extract_fn=fake
    )
    assert out == {}


@pytest.mark.asyncio
async def test_extract_never_raises_degrades_to_prior() -> None:
    from voxtera.call_center.booking_extract import extract_booking_slots

    async def boom(_msg: str):
        raise RuntimeError("anthropic down")

    out = await extract_booking_slots(
        utterance="x", history=None, prior_slots={"restaurant": "Tuğra"}, extract_fn=boom
    )
    assert out == {"restaurant": "Tuğra"}  # degraded to prior, no raise


@pytest.mark.asyncio
async def test_no_booking_guidance_or_time_anchor_without_ticketing() -> None:
    client = _one_round_client()
    await _real_render_property_turn(
        payload=_payload(),
        hotel_id="demo",
        ticketer=None,  # property cannot file → no booking, no-promises rule
        model="m",
        client=client,
    )
    system_text = client.calls[0]["system"][0]["text"]
    assert "BOOKING FLOW" not in system_text
    assert "You CANNOT perform actions" in system_text
    user_msg = client.calls[0]["messages"][-1]["content"]
    assert "do not read this aloud" not in user_msg
