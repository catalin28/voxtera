"""Feature flags for external information sources (Phase 4a).

Each external source the bot can consult — YouTube, Google Places (for
hotel reviews), Tavily (for general web search) — is gated by an
``*_ENABLED`` env var **and** the presence of its API key. This double
gate gives operators two ways to silence a source:

- **Operator preference / cost control:** set the flag to ``false``.
  Wiring is skipped entirely; no calls are made even if a key is set.
- **Key not yet provisioned:** leave the flag at default (``true``) but
  don't set the key. Wiring logs a friendly warning and is skipped.

The Pipecat LLM tool schema is only registered when both checks pass,
so the model will simply not see the tool at all when the source is
disabled — which is the cleanest way to prevent hallucinated calls.

This module is import-light: no network, no model load, just env reads.
That makes it safe to import from anywhere, including unit tests.
"""

from __future__ import annotations

import os

# ---------- Flag names (kept here so tests can monkeypatch by name) ----------
ENV_YOUTUBE_SEARCH = "YOUTUBE_SEARCH_ENABLED"
ENV_PLACES_SEARCH = "PLACES_SEARCH_ENABLED"
ENV_WEB_SEARCH = "WEB_SEARCH_ENABLED"

ENV_YOUTUBE_API_KEY = "YOUTUBE_API_KEY"
ENV_GOOGLE_PLACES_API_KEY = "GOOGLE_PLACES_API_KEY"
ENV_TAVILY_API_KEY = "TAVILY_API_KEY"


def _bool_env(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw == "":
        return default
    return raw not in {"false", "0", "no", "off"}


def _key_env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# ---------- Public flag helpers (one per source) ----------
def is_youtube_search_enabled() -> bool:
    """True iff YOUTUBE_SEARCH_ENABLED is truthy AND YOUTUBE_API_KEY is set."""
    return _bool_env(ENV_YOUTUBE_SEARCH, default=True) and bool(_key_env(ENV_YOUTUBE_API_KEY))


def is_places_search_enabled() -> bool:
    """True iff PLACES_SEARCH_ENABLED is truthy AND GOOGLE_PLACES_API_KEY is set."""
    return _bool_env(ENV_PLACES_SEARCH, default=True) and bool(_key_env(ENV_GOOGLE_PLACES_API_KEY))


def is_web_search_enabled() -> bool:
    """True iff WEB_SEARCH_ENABLED is truthy AND TAVILY_API_KEY is set."""
    return _bool_env(ENV_WEB_SEARCH, default=True) and bool(_key_env(ENV_TAVILY_API_KEY))


def youtube_api_key() -> str:
    return _key_env(ENV_YOUTUBE_API_KEY)


def google_places_api_key() -> str:
    return _key_env(ENV_GOOGLE_PLACES_API_KEY)


def disabled_reason(flag_env: str, key_env: str) -> str:
    """Build a one-line human reason for a wire-skip log line."""
    if not _bool_env(flag_env, default=True):
        return f"{flag_env}=false in environment"
    if not _key_env(key_env):
        return f"{key_env} not set in environment"
    return "enabled"
