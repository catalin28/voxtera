"""Language configuration loader — single source of truth for language metadata.

Reads ``config/languages.json`` at module import and exposes typed accessors
for the rest of the codebase. Adding a language anywhere in Voxtera now
means editing one JSON entry instead of five Python dicts.

The JSON has two top-level sections:

* ``voices`` — provider-wide voice catalogs (OpenAI voice names, Cartesia
  UUIDs, Google Chirp 3 HD voice characters). These are language-agnostic
  for OpenAI/Cartesia; Google Chirp 3 HD voice IDs are constructed at
  runtime as ``{locale}-Chirp3-HD-{character}``.
* ``languages`` — per-language metadata: STT codes per provider, TTS
  support flags + locale per provider, Pipecat ``Language`` enum names
  for Cartesia, display strings for the UI.

Use ``LANG_CONFIG`` for direct dict access, or the helper functions
``google_locale_for``, ``cartesia_supports``, ``translation_name_for``,
etc. for the common lookups.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

# config/languages.json lives at the project root, alongside other config.
# Resolves relative to this module (src/voxtera/lang_config.py), going up
# two parents to the project root, then into config/.
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "languages.json"


def _load() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"languages.json not found at {_CONFIG_PATH}. "
            "Voxtera language metadata is required — check the config/ "
            "directory is intact."
        )
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    logger.info(
        "[lang-config] loaded {} languages from {}",
        len(data.get("languages", {})),
        _CONFIG_PATH.name,
    )
    return data


LANG_CONFIG: dict[str, Any] = _load()


# --- Helpers ----------------------------------------------------------------


def language_codes() -> list[str]:
    """All ISO 639-1 language codes registered in the config."""
    return list(LANG_CONFIG["languages"].keys())


def is_known_language(code: str) -> bool:
    """``True`` if ``code`` exists in the language config."""
    return code in LANG_CONFIG["languages"]


def language_entry(code: str) -> dict[str, Any] | None:
    """Full entry for ``code`` or ``None`` if unknown."""
    return LANG_CONFIG["languages"].get(code)


def default_language() -> str:
    """Default language code (used when the user doesn't pick one)."""
    return LANG_CONFIG.get("default", "en")


def translation_name_for(code: str) -> str:
    """English name of a language (for LLM translation prompts).

    Falls back to the code itself when unknown — keeps callers ergonomic
    without making the loader raise on every lookup.
    """
    entry = language_entry(code)
    if entry is None:
        return code
    return entry.get("translation_name", code)


# --- STT --------------------------------------------------------------------


def stt_code_for(code: str, provider: str) -> str | None:
    """Provider-specific STT code (e.g. ``"ro-RO"`` for Google, ``"ro"`` for Gladia).

    Returns ``None`` if the provider doesn't support the language or the
    language isn't registered.
    """
    entry = language_entry(code)
    if entry is None:
        return None
    return entry.get("stt", {}).get(provider)


def stt_supports(code: str, provider: str) -> bool:
    """``True`` if the STT provider supports the language.

    A provider is "supporting" when the language entry's ``stt.<provider>``
    field is a non-null value. ``null`` explicitly marks unsupported
    (e.g. ``google-chirp2`` is null for everything except English because
    our ``_build_google_chirp2_stt()`` hardcodes ``languages=["en-US"]``).
    """
    return stt_code_for(code, provider) is not None


# --- TTS --------------------------------------------------------------------


def tts_supports(code: str, provider: str) -> bool:
    """``True`` if the TTS provider supports the language.

    Returns ``False`` for unknown languages so callers can treat that as
    "unsupported" and surface a clean error rather than crashing.
    """
    entry = language_entry(code)
    if entry is None:
        return False
    return bool(entry.get("tts", {}).get(provider, {}).get("supported", False))


def google_locale_for(code: str) -> str | None:
    """BCP-47 locale code for Google TTS (e.g. ``"ro-RO"``).

    Returns ``None`` if Google doesn't support the language.
    """
    entry = language_entry(code)
    if entry is None:
        return None
    google = entry.get("tts", {}).get("google", {})
    if not google.get("supported"):
        return None
    return google.get("locale")


def cartesia_pipecat_enum_for(code: str) -> str | None:
    """Pipecat ``Language`` enum name for Cartesia (e.g. ``"RO"``).

    Caller converts to enum via ``getattr(Language, name)``. Returns
    ``None`` when Cartesia doesn't support the language.
    """
    entry = language_entry(code)
    if entry is None:
        return None
    cartesia = entry.get("tts", {}).get("cartesia", {})
    if not cartesia.get("supported"):
        return None
    return cartesia.get("pipecat_enum")


# --- Voice catalogs ---------------------------------------------------------


def chirp3_voice_characters() -> list[dict[str, str]]:
    """Google Chirp 3 HD voice characters (locale-agnostic)."""
    return list(LANG_CONFIG.get("voices", {}).get("google_chirp3_characters", []))


def openai_voices() -> list[dict[str, str]]:
    """OpenAI tts-1 voices (locale-agnostic)."""
    return list(LANG_CONFIG.get("voices", {}).get("openai", []))


def cartesia_voices() -> list[dict[str, str]]:
    """Cartesia voices (multilingual, locale-agnostic)."""
    return list(LANG_CONFIG.get("voices", {}).get("cartesia", []))


def google_voice_id(locale: str, character: str) -> str:
    """Construct a full Chirp 3 HD voice ID from locale + character.

    Example: ``google_voice_id("ro-RO", "Charon")`` → ``"ro-RO-Chirp3-HD-Charon"``.
    """
    return f"{locale}-Chirp3-HD-{character}"


def voices_for_language(code: str, provider: str) -> list[dict[str, str]]:
    """All voice IDs available for ``code`` under ``provider``.

    Returns an empty list when the provider doesn't support the language —
    callers can use this as the "disable Start button" signal in the UI.
    """
    if not tts_supports(code, provider):
        return []

    if provider == "openai":
        return openai_voices()
    if provider == "cartesia":
        return cartesia_voices()
    if provider == "google":
        locale = google_locale_for(code)
        if locale is None:
            return []
        return [
            {"id": google_voice_id(locale, c["id"]), "name": f"{c['name']}"}
            for c in chirp3_voice_characters()
        ]
    return []
