"""Hotel-local current time — for booking date resolution (Phase 4).

The call-center concierge pipeline (``render_property_turn``) builds its
Anthropic request directly and does NOT pass through the Pipecat
:class:`~voxtera.time_context.TimeContextInjector`, so it has no idea what
time it is at the hotel. A booking needs that: "a table for two tomorrow at
7pm" can only become a concrete date if the model knows the hotel's local
*now*. A PSTN caller's own timezone is unknown and irrelevant — the table is
reserved in the HOTEL's local time — so we anchor to ``HotelConfig.timezone``.

We deliberately do NOT parse the relative date in Python: dates arrive in many
languages and phrasings ("demain", "yarın akşam", "this Friday"). Instead we
hand the model the hotel-local current datetime and instruct it (in the prompt)
to resolve and read back an ABSOLUTE date. Python supplies the fact; the LLM
does the resolution — consistent with the Option 1 booking design.

Safety: never raises. An unknown/None timezone degrades to the server clock.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger


def hotel_local_now(timezone: str | None) -> tuple[datetime, str]:
    """Return ``(now, source_label)`` for the hotel's local clock.

    Uses ``timezone`` (an IANA name like "Europe/Istanbul") when valid;
    otherwise falls back to the server's own clock. The label names which
    source was used so the prompt note can be honest.
    """
    if timezone:
        try:
            return datetime.now(ZoneInfo(timezone)), timezone
        except (ZoneInfoNotFoundError, ValueError, OSError):
            logger.warning(
                "[hotel-time] unknown timezone {!r}; using server clock", timezone
            )
    return datetime.now(), "server local time"


def hotel_time_note(timezone: str | None) -> str:
    """Build the for-context-only current-time line injected into a booking turn.

    Mirrors the wording style of
    :meth:`voxtera.time_context.TimeContextInjector._inject_time` so the model
    treats it the same way: context only, never read aloud, used to turn
    "tomorrow 7pm" into a concrete date.
    """
    now, source = hotel_local_now(timezone)
    # Built without %-d / %-I so it stays portable across OSes.
    hour_12 = now.hour % 12 or 12
    ampm = "AM" if now.hour < 12 else "PM"
    stamp = f"{now:%A}, {now.day} {now:%B} {now.year}, {hour_12}:{now.minute:02d} {ampm}"
    return (
        f"[Booking time anchor — for context only, do not read this aloud: the "
        f"hotel's current local time is {stamp} ({source}). When the guest gives a "
        f"relative date or time (e.g. 'tomorrow', 'this Friday', 'tonight at 8'), "
        f"resolve it against this and confirm the ABSOLUTE date back to them.]"
    )
