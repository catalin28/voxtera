"""Pipecat ``FunctionSchema`` for the ``create_ticket`` LLM tool.

This module is intentionally pure — it builds and returns a schema based on a
:class:`~voxtera.actions.hotel_config.HotelConfig`. It does not register the
tool, does not import ``bot.py``, and has no side effects. The caller (the
bot startup wiring in Phase 3) owns registration.

The schema's ``category`` enum is restricted to the hotel's
``allowed_categories``, so Claude cannot file a ticket in a category the
hotel hasn't opted into.
"""

from __future__ import annotations

from typing import Final

from pipecat.adapters.schemas.function_schema import FunctionSchema

from voxtera.actions.hotel_config import HotelConfig

# The function name Claude will call. Keep this stable — changing it would
# require coordinating prompt updates and any logs/dashboards keyed on it.
CREATE_TICKET_FUNCTION_NAME: Final[str] = "create_ticket"


def build_create_ticket_tool(hotel_config: HotelConfig) -> FunctionSchema:
    """Build the ``create_ticket`` FunctionSchema for a given hotel.

    The returned schema's ``category`` parameter is constrained to the
    hotel's allowed categories. Other fields are required so Claude cannot
    file an underspecified ticket — the system prompt instructs Claude to
    gather missing info from the guest before calling the tool.
    """
    allowed = [c.value for c in hotel_config.allowed_categories]
    properties = {
        "category": {
            "type": "string",
            "enum": allowed,
            "description": (
                "The kind of request being filed. Choose the closest match from the "
                "allowed list. Use 'Other' only if no other category fits."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "A one-line description of the request, written in the hotel's staff "
                f"language ({hotel_config.official_language}), for hotel staff to read. "
                "State the issue plainly. Do NOT include the room number here (that goes "
                "in `room_number`). Do NOT include the original guest quote (that goes in "
                "`original_quote`). Aim for under 120 characters."
            ),
            "maxLength": 500,
        },
        "room_number": {
            "type": "string",
            "description": (
                "The guest's room number, as they stated it. If the guest has not yet "
                "provided one, ASK THEM before calling this tool — do not call the tool "
                "with a placeholder or empty value."
            ),
        },
        "original_quote": {
            "type": "string",
            "description": (
                "The guest's verbatim words describing the issue, in their own language. "
                "Do not translate. Pick the single most representative phrase if the "
                "guest spoke at length."
            ),
            "maxLength": 1000,
        },
        "language_detected": {
            "type": "string",
            "description": (
                "The language the guest spoke in, as a human-readable label "
                "(e.g. 'French', 'Japanese', 'Spanish'). Use this even if the bot's "
                "audio output language was already locked — it describes the guest's "
                "side of the conversation."
            ),
        },
    }
    description = (
        "File a ticket with hotel staff for a guest request, complaint, or booking. "
        "ONLY call this tool AFTER you have summarized the request to the guest in their "
        "own language and they have confirmed (e.g. 'yes', 'oui', 'sì'). If the guest "
        "declines or hesitates, do NOT call this tool. The tool posts a single, "
        "non-revocable message to the staff channel."
    )
    return FunctionSchema(
        name=CREATE_TICKET_FUNCTION_NAME,
        description=description,
        properties=properties,
        required=["category", "summary", "room_number", "original_quote", "language_detected"],
    )
