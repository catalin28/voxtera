"""Multilingual startup greetings for Voxtera — the hotel voice concierge.

Hardcoded so the bot can speak before any LLM round-trip: faster, deterministic,
no token cost. Once the guest speaks, Whisper detects their language and Claude
replies in kind — see ``src/voxtera/prompts/system_prompt.py``.

Two catalogs (loaded from ``config/greetings.json``):

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

Edit greetings: modify ``config/greetings.json`` — no code changes needed.
Add a language: add a ``"xx": "..."`` entry to the ``greetings`` key and a
matching ``"xx": {...}`` entry to the ``timed_greetings`` key.
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

# --- Load greetings from config/greetings.json ---
_GREETINGS_JSON = Path(__file__).resolve().parents[3] / "config" / "greetings.json"


def _load_greetings() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Load greetings from the JSON config file, falling back to empty dicts."""
    if _GREETINGS_JSON.is_file():
        with open(_GREETINGS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("greetings", {}), data.get("timed_greetings", {})
    return {}, {}


GREETINGS: dict[str, str]
TIMED_GREETINGS: dict[str, dict[str, str]]
GREETINGS, TIMED_GREETINGS = _load_greetings()

# Fallback: if JSON was empty/missing, ensure English always exists.
if "en" not in GREETINGS:
    GREETINGS["en"] = (
        "Hello, and a very warm welcome. "
        "It's a pleasure to have you with us — I'm your concierge. "
        "How may I help you?"
    )


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


def resolve_greeting(preference: str = "auto", *, daypart: str | None = None) -> tuple[str, str]:
    """Pick a greeting and return ``(language_code, text)``.

    Args:
        preference: ``"auto"`` to detect from the system locale, or an explicit
            language code like ``"fr"``. Unknown codes fall back to English.
        daypart: optional ``"morning"`` / ``"afternoon"`` / ``"evening"``. When
            given and a timed greeting exists for the chosen language, the
            time-of-day variant is returned; otherwise the time-neutral one is.
            Boot-time callers leave this ``None`` (the browser hasn't reported
            the guest's timezone yet) — see ``GreetingController``.

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
            return code, timed[daypart]

    return code, GREETINGS[code]
