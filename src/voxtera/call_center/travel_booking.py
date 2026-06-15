"""Declarative hotel-stay booking schema for the TRAVEL-AGENCY path (Phase 7).

The single-hotel concierge (Phase 4, ``intents.py`` + ``property_render.py``)
books restaurant/spa tables for one hotel. The travel-agency path is a
DIFFERENT product: a travel agent talking to a *potential client*, who books a
HOTEL STAY at one of the many hotels the agency sells — and who can NOT book a
restaurant or spa (that is a property-mode action only).

So this module is the agency counterpart of ``intents.py``: it describes WHAT a
hotel-stay booking needs and generates the booking-rules prompt block
(:func:`stay_guidance_block`) the travel render includes when the agency can
file tickets. Like the property design (Option 1, pure prompt-guided), the LLM
decides whether the client is booking and when to file via the ``book_hotel_stay``
tool; Python only supplies the rules, the hotel-local clock, and the silent
slot tracking (see ``booking_extract.extract_stay_slots`` + ``stay_recap``).

The client on this path is always external (they are not staying yet), so there
is NO guest_type / room-number branch — the contact is always a phone or email.
"""

from __future__ import annotations

from dataclasses import dataclass

from voxtera.call_center.intents import Slot, booking_recap

# Slot keys the agency stay-booking tracks (LLM-extracted by
# booking_extract.extract_stay_slots, persisted in session["stay_slots"], fed
# back as the LOCKED recap). The hotel is resolved from the conversation rather
# than asked as a question, but it IS tracked so the ticket can name it.
STAY_SLOT_KEYS: tuple[str, ...] = (
    "hotel",
    "check_in",
    "check_out",
    "guests",
    "name",
    "contact",
)


@dataclass(frozen=True)
class StayIntentSchema:
    """The single bookable intent on the travel path: a hotel stay."""

    key: str
    label: str
    mandatory_slots: tuple[Slot, ...]


HOTEL_STAY = StayIntentSchema(
    key="hotel_stay",
    label="a hotel stay",
    mandatory_slots=(
        Slot(
            key="hotel",
            description="which hotel to book (the one the client settled on)",
            example="the hotel under discussion",
        ),
        Slot(
            key="check_in",
            description="the check-in date, resolved to an ABSOLUTE date",
            example="next Friday → Fri 19 June",
        ),
        Slot(
            key="check_out",
            description="the check-out date, resolved to an ABSOLUTE date",
            example="the Sunday after → Sun 21 June",
        ),
        Slot(key="guests", description="how many guests", example="2 adults, 1 child"),
        Slot(key="name", description="the name the booking is under"),
        Slot(
            key="contact",
            description="the client's phone number or email for the booking",
            example="a mobile number or email",
        ),
    ),
)


def stay_recap(slots: dict[str, str] | None) -> str:
    """LOCKED recap of the stay slots gathered so far (next-turn anti-drift).

    Reuses the property recap formatter with a stay-specific field order so the
    wording — "these details are LOCKED, do not re-ask or change them" — is
    identical to the proven property flow.
    """
    return booking_recap(slots, order=STAY_SLOT_KEYS)


def stay_guidance_block() -> str:
    """The always-on hotel-stay booking rules for the travel render.

    Included whenever the agency can file tickets. It does NOT tell the model a
    booking is happening — the model decides that from the conversation. It only
    shapes HOW to collect a hotel stay once the client wants to book, and draws
    a hard boundary the agency must not cross (no restaurant/spa).
    """
    mand = ", ".join(
        f"{s.key} ({s.description})" for s in HOTEL_STAY.mandatory_slots if s.key != "hotel"
    )
    lines = [
        "HOTEL-STAY BOOKING — when (and only when) the client wants to book a "
        "hotel stay you can actually file:",
        "- You are a TRAVEL AGENT. You can book a HOTEL STAY only. You CANNOT "
        "book a restaurant, a spa, a table, a treatment or any in-hotel service "
        "— if asked, explain that is arranged with the hotel directly after "
        "check-in, and do not collect those details.",
        "- The client is an outside guest, never staying yet: take a PHONE or "
        "EMAIL as the contact. Never ask for a room number.",
        f"- For {HOTEL_STAY.label}, collect: {mand}. The hotel itself is the one "
        "the client settled on in the conversation — confirm which hotel, do not "
        "make them spell it out if it is already clear.",
        "- Ask for only ONE missing detail per reply — never bundle questions.",
        "- A '[Booking so far … LOCKED]' note may list details the client has "
        "ALREADY given. Those values are FIXED: never re-ask for them and never "
        "change them — use them exactly. Only collect what is still missing.",
        "- Turn any relative date ('next Friday', 'the first week of July', "
        "'tonight') into an ABSOLUTE date using the booking time anchor, and read "
        "the absolute check-in and check-out dates back when you confirm.",
        "- File the booking with the book_hotel_stay tool only AFTER the client "
        "confirms, then close with a single short confirmation line.",
    ]
    return "\n".join(lines)
