"""Multilingual startup greetings for Voxtera — the hotel voice concierge.

The greeting TEXT lives in ``greetings.json`` next to this module and is
loaded ONCE at import time — so runtime behaviour is identical to the old
hardcoded dicts (no LLM round-trip, no token cost, deterministic), but the
text is editable from the admin page (Voice Concierge Prompts) without
touching Python. Edits apply from the NEXT call: the bot subprocess imports
this module at startup. Once the guest speaks, the STT detects their language
and Claude replies in kind — see ``src/voxtera/prompts/system_prompt.py``.

Two catalogs (top-level keys in ``greetings.json``):

* ``GREETINGS`` — one time-neutral concierge greeting per language. This is the
  safe default: used at bot boot (before the browser connects) and whenever the
  guest's local time is unknown (phone line, Telegram, an older widget).
* ``TIMED_GREETINGS`` — morning / afternoon / evening variants per language.
  Used when the browser reports the guest's timezone via the ``voxtera-timezone``
  app-message; :class:`~voxtera.controllers.GreetingController` computes the
  daypart and picks the matching variant.

Why the browser's timezone and not the server clock: the bot runs on a server
whose clock is UTC and tells us nothing about the guest's local time. The widget
knows it (``Intl.DateTimeFormat().resolvedOptions().timeZone``) and sends it.

Resolution order in :func:`resolve_greeting`:

    1. Explicit preference (e.g. ``"fr"``)
    2. System locale (``locale.getlocale()``)
    3. English fallback

Add a language: add a ``"xx": "..."`` entry to ``greetings`` and a matching
``"xx": {...}`` entry to ``timed_greetings`` in ``greetings.json``. A language
present in ``greetings`` but missing from ``timed_greetings`` simply never gets
a time-of-day greeting — it falls back to the neutral one, which is always safe.

Where a language does not lexically distinguish a daypart (French has no
separate "good afternoon"; Korean and Hindi barely daypart greetings at all),
the variants intentionally repeat — that is correct usage, not an oversight.
"""

from __future__ import annotations

import json
import locale
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_LANGUAGE = "en"

# Daypart bucket keys used throughout (TIMED_GREETINGS, GreetingController).
DAYPARTS = ("morning", "afternoon", "evening")

# ── Greeting text: loaded once at import from greetings.json ────────────────
# Read as bytes + explicit decode (same convention as system_prompt.py) so we
# get exactly what's on disk. json.loads validates; a malformed file fails
# LOUDLY at import (bot refuses to start) rather than greeting guests wrongly.
_DATA_PATH = Path(__file__).parent / "greetings.json"
_data = json.loads(_DATA_PATH.read_bytes().decode("utf-8"))

# Time-neutral concierge greeting per language. Always safe to fall back to.
GREETINGS: dict[str, str] = _data["greetings"]

# Morning / afternoon / evening variants. Same body as the neutral greeting,
# only the opening time-of-day phrase changes. Languages that do not lexically
# daypart (French afternoon, Korean, Hindi) intentionally repeat the neutral
# phrasing — correct usage, not an oversight.
TIMED_GREETINGS: dict[str, dict[str, str]] = _data["timed_greetings"]

if DEFAULT_LANGUAGE not in GREETINGS:  # the English fallback must always exist
    raise ValueError(f"greetings.json: missing required '{DEFAULT_LANGUAGE}' entry")

# Per-language generic used when a greeting contains "{hotel_name}" but the bot
# runs without a hotel config (e.g. plain demo) — "our hotel" and friends.
GENERIC_HOTEL: dict[str, str] = _data.get("generic_hotel", {})


def fill_hotel_name(text: str, hotel_name: str | None = None, lang: str = DEFAULT_LANGUAGE) -> str:
    """Resolve the optional ``{hotel_name}`` placeholder in a greeting.

    Uses the actual hotel name when known, otherwise the per-language generic
    from ``generic_hotel`` ("our hotel"). Plain ``str.replace`` — not
    ``str.format`` — so any other braces in the text are harmless.
    """
    if "{hotel_name}" not in text:
        return text
    name = (hotel_name or "").strip()
    if not name:
        name = GENERIC_HOTEL.get(lang) or GENERIC_HOTEL.get(DEFAULT_LANGUAGE) or "our hotel"
    return text.replace("{hotel_name}", name)


def apply_hotel_name(
    hotel_name: str | None,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Return copies of (GREETINGS, TIMED_GREETINGS) with ``{hotel_name}`` resolved.

    Called once at bot startup (pipeline.py) with the hotel's display name from
    ``HotelConfig`` — the controllers then work with plain, ready-to-speak text.
    """
    g = {lang: fill_hotel_name(t, hotel_name, lang) for lang, t in GREETINGS.items()}
    tg = {
        lang: {part: fill_hotel_name(t, hotel_name, lang) for part, t in variants.items()}
        for lang, variants in TIMED_GREETINGS.items()
    }
    return g, tg


def daypart_for_hour(hour: int) -> str:
    """Map a 24-hour clock hour (0-23) to a daypart key.

    Boundaries: 05:00-11:59 morning, 12:00-17:59 afternoon, 18:00-04:59 evening.
    """
    if 5 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 17:
        return "afternoon"
    return "evening"


def daypart_for_timezone(tz: str | None) -> str | None:
    """Return the current daypart in IANA timezone ``tz`` (e.g. ``"Europe/Paris"``).

    Returns ``None`` when ``tz`` is missing, empty, or not a recognised IANA
    name — callers should then fall back to the time-neutral greeting.
    """
    if not tz or not isinstance(tz, str):
        return None
    try:
        now = datetime.now(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # Unknown/malformed timezone — degrade to time-neutral.
        return None
    return daypart_for_hour(now.hour)


def _detect_system_language() -> str | None:
    """Return a 2-letter language code from the OS locale, or None on failure."""
    try:
        loc, _ = locale.getlocale()
    except (ValueError, locale.Error):
        loc = None

    if not loc:
        # getlocale() can be None or empty when LANG isn't set; fall back.
        try:
            loc = locale.getdefaultlocale()[0]
        except (ValueError, IndexError):
            loc = None

    if not loc:
        return None

    # Locale strings look like "en_US", "fr_FR", "ja_JP". We want the prefix.
    return loc.split("_", 1)[0].lower()


def resolve_greeting(
    preference: str = "auto",
    *,
    daypart: str | None = None,
    hotel_name: str | None = None,
) -> tuple[str, str]:
    """Pick a greeting and return ``(language_code, text)``.

    Args:
        preference: ``"auto"`` to detect from the system locale, or an explicit
            language code like ``"fr"``. Unknown codes fall back to English.
        daypart: optional ``"morning"`` / ``"afternoon"`` / ``"evening"``. When
            given and a timed greeting exists for the chosen language, the
            time-of-day variant is returned; otherwise the time-neutral one is.
            Boot-time callers leave this ``None`` (the browser hasn't reported
            the guest's timezone yet) — see ``GreetingController``.
        hotel_name: the hotel's display name, used to resolve the optional
            ``{hotel_name}`` placeholder; ``None`` falls back to the
            per-language generic ("our hotel").

    Returns:
        A ``(code, text)`` tuple — ``code`` is the language chosen (useful for
        logging), ``text`` is the greeting to speak.
    """
    pref = (preference or "auto").lower().strip()

    if pref == "auto":
        detected = _detect_system_language()
        code = detected if (detected and detected in GREETINGS) else DEFAULT_LANGUAGE
    elif pref in GREETINGS:
        code = pref
    else:
        # Unknown explicit code — fall back to English but stay observable.
        code = DEFAULT_LANGUAGE

    if daypart:
        timed = TIMED_GREETINGS.get(code)
        if timed and daypart in timed:
            return code, fill_hotel_name(timed[daypart], hotel_name, code)

    return code, fill_hotel_name(GREETINGS[code], hotel_name, code)
