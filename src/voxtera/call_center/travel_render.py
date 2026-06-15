"""Travel render — ONE LLM that recommends hotels AND books a stay (Phase 7).

The travel-agency counterpart of ``property_render``. The standard travel
render (``concierge._build_anthropic_render``) only talks — it recommends hotels
from the agency portfolio but cannot act. This variant gives that same render
the single action a travel agent has: filing a HOTEL-STAY booking via the
``book_hotel_stay`` tool, with the hotel-stay collection rules
(``travel_booking.stay_guidance_block``) and the silent slot recap.

Design mirrors property mode exactly (the part the user chose to keep): the LLM
owns the booking conversation and decides when to file; Python supplies the
hotel-local clock, the rules, and the parallel slot tracking. The agency books a
STAY only — never a restaurant or spa (that boundary is in the guidance block).

Delivery reuses ``PropertyTicketer``: the booked hotel is a portfolio listing
with no Telegram channel of its own, so the ticket is delivered through a
configured "desk" hotel runtime (``TRAVEL_BOOKING_DELIVERY_HOTEL_ID``, default
``kempinski_ciragan`` — both shipped configs share one channel), with the chosen
hotel's name stamped into the staff-facing summary. Category is ``Reservation``.

When no ticketer/channel is available the tool is absent and a no-promises rule
is added, so the agent refers the client on rather than inventing a booking.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from voxtera.call_center.concierge import (
    LLM_STOP_SEQUENCES,
    _anthropic,
    _build_render_user_msg,
    _with_persona,
)
from voxtera.call_center.hotel_time import hotel_time_note
from voxtera.call_center.session import build_message_turns
from voxtera.call_center.travel_booking import stay_guidance_block, stay_recap

# Max talk→tool→talk rounds (one ticket per turn; round 3 covers a retry on a
# rejected argument), matching property_render.
_MAX_ROUNDS = 3

# Which hotel runtime delivers travel-agency bookings. The portfolio hotels have
# no channel of their own, so we route through a shipped "desk" config; both
# demo and kempinski_ciragan point at the same shared Telegram channel.
DELIVERY_HOTEL_ID = os.environ.get("TRAVEL_BOOKING_DELIVERY_HOTEL_ID", "kempinski_ciragan")

_NO_ACTIONS_RULE = """

ACTIONS — read carefully.
You CANNOT file a booking right now: no reservation channel is connected. NEVER
say you will book, reserve, hold or confirm a hotel — those are promises nothing
will keep. Help the client choose, then warmly direct them to call the agency to
finalise the reservation."""

_BOOK_HOTEL_STAY_TOOL: dict[str, Any] = {
    "name": "book_hotel_stay",
    "description": (
        "File a hotel-stay reservation request to the agency once the client has "
        "confirmed. Call it only after the client says yes, with every field "
        "collected. Do not narrate the tool; just call it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hotel": {"type": "string", "description": "The hotel to book."},
            "check_in": {"type": "string", "description": "Absolute check-in date."},
            "check_out": {"type": "string", "description": "Absolute check-out date."},
            "guests": {"type": "string", "description": "How many guests (e.g. '2 adults')."},
            "name": {"type": "string", "description": "Name the booking is under."},
            "contact": {"type": "string", "description": "Client phone number or email."},
            "language_detected": {
                "type": "string",
                "description": "Language the client is speaking (e.g. 'en', 'French').",
            },
        },
        "required": [
            "hotel",
            "check_in",
            "check_out",
            "guests",
            "name",
            "contact",
            "language_detected",
        ],
    },
}


def _validate_stay_args(args: Any) -> dict[str, str]:
    """Coerce book_hotel_stay tool args to ticket fields. Raises on bad input."""
    if not callable(getattr(args, "get", None)):
        raise ValueError(f"expected mapping for arguments, got {type(args).__name__}")

    def require(key: str, max_len: int) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"missing or empty required argument {key!r}")
        value = value.strip()
        if len(value) > max_len:
            raise ValueError(f"argument {key!r} exceeds max length {max_len}")
        if "\x00" in value:
            raise ValueError(f"argument {key!r} contains a null byte")
        return value

    hotel = require("hotel", 200)
    check_in = require("check_in", 64)
    check_out = require("check_out", 64)
    guests = require("guests", 64)
    name = require("name", 120)
    contact = require("contact", 200)
    language = require("language_detected", 64)
    summary = (
        f"Hotel-stay booking — {hotel}: {check_in} → {check_out}, {guests}. "
        f"Guest {name}, contact {contact}."
    )
    from voxtera.actions.ticket import Category

    return {
        "category": Category.RESERVATION.value,
        "summary": summary[:500],
        # Portfolio bookings have no room; the field is required by the ticket
        # model but carries the contact handle so staff can reach the client.
        "room_number": contact[:64],
        "language_detected": language,
    }


async def _execute_book_stay(
    args: Any, *, ticketer: Any
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """File one hotel-stay booking. Returns (tool_result_payload, ticket | None)."""
    try:
        fields = _validate_stay_args(args)
    except ValueError as e:
        logger.warning("[travel-render] book_hotel_stay invalid args: {}", e)
        return (
            {
                "status": "rejected",
                "reason": str(e),
                "guidance": (
                    "The arguments did not parse. Apologize briefly to the client "
                    "and ask them to clarify the missing information."
                ),
            },
            None,
        )

    ticket = await ticketer.file(
        hotel_id=DELIVERY_HOTEL_ID,
        fields=fields,
        original_quote=fields["summary"],
    )
    if ticket:
        return (
            {
                "status": "filed",
                "category": ticket["category"],
                "guidance": (
                    "Confirm to the client in their language that the reservation "
                    "request has been sent to the agency and someone will follow "
                    "up to finalise it. Keep it to one short sentence."
                ),
            },
            ticket,
        )
    return (
        {
            "status": "failed",
            "guidance": (
                "The request could not be delivered. Apologize briefly in the "
                "client's language and suggest they call the agency directly."
            ),
        },
        None,
    )


async def render_travel_turn(
    *,
    payload: dict[str, Any],
    ticketer: Any | None,
    model: str,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Answer one travel turn; the model may file a hotel-stay booking.

    Returns ``{"answer": str, "ticket": {"session_id", "category"} | None}``.
    Mirrors :func:`property_render.render_property_turn`.
    """
    runtime = ticketer.runtime(DELIVERY_HOTEL_ID) if ticketer is not None else None
    can_book = runtime is not None

    brief = bool(payload.get("brief"))
    prompt_name = "travel_agent_voice_render_brief" if brief else "concierge_render"
    include_images = not brief
    hotel_id = (payload.get("hotel_id") or "").strip() or None
    system_text = _with_persona(prompt_name, include_images=include_images, hotel_id=hotel_id)

    tools: list[dict[str, Any]] = []
    if can_book:
        tools.append(_BOOK_HOTEL_STAY_TOOL)
        system_text += "\n\n" + stay_guidance_block()
    else:
        system_text += _NO_ACTIONS_RULE

    client = client or _anthropic()
    history_messages = build_message_turns(payload.get("history"))
    user_content = _build_render_user_msg(payload)
    if can_book:
        # The time anchor + LOCKED recap ride on the CURRENT user message (never
        # the cached system prompt), same rule as property mode.
        user_content += "\n\n" + hotel_time_note(payload.get("hotel_timezone"))
        recap = stay_recap(payload.get("stay_slots"))
        if recap:
            user_content += "\n\n" + recap
    messages: list[dict[str, Any]] = [
        *history_messages,
        {"role": "user", "content": user_content},
    ]
    parts: list[str] = []
    ticket: dict[str, Any] | None = None
    max_tokens = 140 if brief else 512

    tool_choice: dict[str, str] | None = None
    for round_no in range(_MAX_ROUNDS):
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            stop_sequences=LLM_STOP_SEQUENCES,
            system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            **kwargs,
        ) as stream:
            async for delta in stream.text_stream:
                parts.append(delta)
                if on_delta is not None:
                    await on_delta(delta)
            final = await stream.get_final_message()

        if getattr(final, "stop_reason", None) != "tool_use":
            break
        tool_use = next((b for b in final.content if getattr(b, "type", "") == "tool_use"), None)
        if tool_use is None:  # defensive: stop_reason lied
            break
        logger.info(
            "[travel-render] book_hotel_stay call (round {}): {}",
            round_no + 1,
            {k: str(v)[:60] for k, v in (tool_use.input or {}).items()},
        )
        result_payload, ticket = await _execute_book_stay(tool_use.input, ticketer=ticketer)

        if parts and parts[-1] and not parts[-1].endswith((" ", "\n")):
            parts.append(" ")
        messages.append({"role": "assistant", "content": final.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result_payload),
                    }
                ],
            }
        )
        if ticket is not None and result_payload.get("status") == "filed":
            # One booking per turn: the model speaks its confirmation next round
            # but may not call the tool again.
            tool_choice = {"type": "none"}

    return {"answer": "".join(parts).strip(), "ticket": ticket}
