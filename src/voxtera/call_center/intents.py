"""Declarative booking-intent schemas (Phase 4).

These schemas describe WHAT a booking needs. They do not detect intent and
they do not run the conversation — in the chosen design (Option 1, pure
prompt-guided; see docs/fix-plan/concierge-fix-plan.md and the project memory)
the LLM decides whether the guest is booking, which intent applies, and when to
file. Python never guesses intent.

Their sole job here is to GENERATE the booking-rules prompt block
(:func:`booking_guidance_block`) that ``render_property_turn`` always includes
when the property can file tickets. Keeping the slots declarative means the
rules the model sees stay in sync with one source of truth.

Collection rules encoded in the block:
  - establish guest_type FIRST (it decides the contact detail);
  - one slot per turn;
  - in-house → room number, external → phone/email (never a room number for an
    external visitor);
  - resolve relative dates to an absolute date and read it back when confirming;
  - optional extras only after everything mandatory is settled.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from voxtera.actions.ticket import Category

# Guest type — qualifies which contact detail a booking needs (in-house guests
# give a room number; external visitors give a phone or email). Never ask an
# external visitor for a room number.
GUEST_IN_HOUSE = "in_house"
GUEST_EXTERNAL = "external"


@dataclass(frozen=True)
class Slot:
    """One piece of information a booking needs."""

    key: str
    # Short description of what to capture — read by the model, not the guest.
    description: str
    example: str | None = None


@dataclass(frozen=True)
class IntentSchema:
    """A declarative booking intent (restaurant, spa, …)."""

    key: str  # stable id
    label: str  # natural phrase for prompts ("a restaurant reservation")
    category: str  # Telegram ticket Category value the create_ticket tool uses
    mandatory_slots: tuple[Slot, ...]
    # Contact slot keyed by guest_type. in_house guests give a room number;
    # external visitors give a phone or email — never a room number.
    contact_by_guest_type: dict[str, Slot] = field(default_factory=dict)
    optional_slots: tuple[Slot, ...] = ()


# --- Slot fragments reused across intents -------------------------------

_ROOM_NUMBER = Slot(
    key="room_number",
    description="the in-house guest's room number",
    example="room 412",
)
_CONTACT_EXTERNAL = Slot(
    key="contact",
    description="an external visitor's phone number or email for the booking",
    example="a mobile number or email",
)
_GUEST_NAME = Slot(key="name", description="the name the booking is under")


# --- Intent schemas -----------------------------------------------------

RESTAURANT_BOOKING = IntentSchema(
    key="restaurant_booking",
    label="a restaurant reservation",
    category=Category.RESTAURANT.value,
    mandatory_slots=(
        Slot(
            key="date_time",
            description="the date and time of the reservation",
            example="tomorrow at 7pm → Tue 16 June, 7:00 PM",
        ),
        Slot(key="party_size", description="how many people", example="a table for two"),
        _GUEST_NAME,
    ),
    contact_by_guest_type={
        GUEST_IN_HOUSE: _ROOM_NUMBER,
        GUEST_EXTERNAL: _CONTACT_EXTERNAL,
    },
    optional_slots=(
        Slot(key="seating", description="seating preference", example="indoor or terrace"),
        Slot(key="dietary", description="dietary or allergy notes"),
        Slot(key="occasion", description="special occasion", example="anniversary"),
    ),
)

SPA_BOOKING = IntentSchema(
    key="spa_booking",
    label="a spa appointment",
    category=Category.CONCIERGE.value,
    mandatory_slots=(
        Slot(key="treatment", description="which treatment", example="a 60-minute massage"),
        Slot(
            key="date_time",
            description="the date and time of the appointment",
            example="this Friday afternoon → Fri 19 June, ~2:00 PM",
        ),
        _GUEST_NAME,
    ),
    contact_by_guest_type={
        GUEST_IN_HOUSE: _ROOM_NUMBER,
        GUEST_EXTERNAL: _CONTACT_EXTERNAL,
    },
    optional_slots=(
        Slot(key="therapist", description="therapist gender preference"),
        Slot(key="party_size", description="how many guests", example="couple's treatment"),
    ),
)

# All booking intents the property concierge can collect. Order matters only
# for prompt readability.
INTENT_SCHEMAS: tuple[IntentSchema, ...] = (RESTAURANT_BOOKING, SPA_BOOKING)


def _intent_line(schema: IntentSchema) -> str:
    """One-line per-intent slot summary for the guidance block."""
    mand = ", ".join(f"{s.key} ({s.description})" for s in schema.mandatory_slots)
    contact_in = schema.contact_by_guest_type[GUEST_IN_HOUSE].description
    contact_ex = schema.contact_by_guest_type[GUEST_EXTERNAL].description
    opt = ", ".join(s.key for s in schema.optional_slots)
    return (
        f"- For {schema.label}: collect {mand}; "
        f"contact = {contact_in} if in-house, or {contact_ex} if external; "
        f"optional extras (only after the rest): {opt}."
    )


# Booking slot keys we track (LLM-extracted by booking_extract.py, persisted in
# the session, fed back as the LOCKED recap). Kept here as the single list both
# the extractor and the recap agree on.
BOOKING_SLOT_KEYS = (
    "intent",
    "guest_type",
    "restaurant",
    "treatment",
    "date_time",
    "party_size",
    "name",
    "contact",
)


# Order the recap reads in — qualifying/identity first, then logistics.
_RECAP_ORDER = BOOKING_SLOT_KEYS


def booking_recap(slots: dict[str, str] | None, order: tuple[str, ...] | None = None) -> str:
    """Format persisted slots into a LOCKED recap line for the next turn.

    Empty string when there is nothing to recap. The wording tells the model
    these are fixed — the antidote to it re-asking or swapping a value. ``order``
    overrides the field order (the travel stay-booking passes its own keys);
    unknown keys are appended in dict order either way.
    """
    if not slots:
        return ""
    clean = {k: str(v).strip() for k, v in slots.items() if str(v).strip()}
    if not clean:
        return ""
    recap_order = order or _RECAP_ORDER
    keys = [k for k in recap_order if k in clean]
    keys += [k for k in clean if k not in recap_order]
    pairs = "; ".join(f"{k}: {clean[k]}" for k in keys)
    return (
        "[Booking so far — these details are LOCKED. Do NOT re-ask for them and "
        "do NOT change them; use these exact values and only collect what is "
        f"still missing: {pairs}]"
    )


def booking_guidance_block() -> str:
    """The always-on booking rules for the property render.

    Included whenever the property can file tickets. It does NOT tell the model
    a booking is happening — the model decides that from the conversation. It
    only shapes HOW to collect details once a booking is underway, and keeps
    the rules in sync with the declarative schemas above.
    """
    lines = [
        "BOOKING FLOW — when (and only when) the guest wants to make a booking "
        "you can actually file:",
        "- FIRST find out whether they are staying at the hotel (in-house) or an "
        "outside visitor (external) — this decides how to reach them. NEVER ask "
        "an external visitor for a room number.",
        "- Ask for only ONE missing detail per reply — never bundle questions.",
        "- A '[Booking so far … LOCKED]' note may appear in the message listing "
        "details the guest has ALREADY given. Those values are FIXED: never "
        "re-ask for them and never change them — use them exactly (if it says "
        "restaurant: Tuğra, it is Tuğra, never another venue). Only collect what "
        "is still missing.",
        "- Turn any relative date/time ('tomorrow', 'tonight at 8', 'this "
        "Friday') into an ABSOLUTE date using the booking time anchor, and read "
        "that absolute date back when you confirm.",
        "- Offer optional extras only after every mandatory detail is settled; "
        "never lead with them.",
        "- File the booking only after the guest confirms, then close with a "
        "single short confirmation line.",
    ]
    lines.extend(_intent_line(s) for s in INTENT_SCHEMAS)
    return "\n".join(lines)
