"""Configuration for the website-concierge Leads API service.

This service is a *separate process* from the bot, so it carries its own small
settings object rather than bloating ``voxtera.config.Settings``. As with the
rest of the codebase, config is read purely from ``os.environ``; the entry
point (``voxtera.concierge.__main__``) calls ``load_dotenv()`` before
``load_concierge_settings()`` so a local ``.env`` is honoured while this
function stays a pure function over the environment (testable in isolation).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConciergeSettings:
    """Runtime configuration for the Leads API.

    Secrets (``db_password``, ``api_token``) are ``repr=False`` so they never
    surface in logs, stack traces, or REPL output.
    """

    db_host: str
    db_port: int
    db_user: str
    db_password: str = field(repr=False)
    db_name: str
    api_token: str = field(repr=False)
    api_host: str = "0.0.0.0"  # noqa: S104 — bind all; runs behind the Droplet's reverse proxy / private network
    api_port: int = 8080
    db_pool_min: int = 1
    db_pool_max: int = 5


def load_concierge_settings() -> ConciergeSettings:
    """Build :class:`ConciergeSettings` from ``os.environ``.

    Does NOT read ``.env`` — call ``dotenv.load_dotenv()`` first if you want a
    local file honoured. Raises ``KeyError`` (via ``_require``) when a required
    secret/connection variable is missing, so misconfiguration fails loudly at
    startup rather than at first request.
    """
    return ConciergeSettings(
        db_host=os.environ.get("LEADS_DB_HOST", "127.0.0.1"),
        db_port=int(os.environ.get("LEADS_DB_PORT", "3306")),
        db_user=_require("LEADS_DB_USER"),
        db_password=_require("LEADS_DB_PASSWORD"),
        db_name=os.environ.get("LEADS_DB_NAME", "voxtera"),
        api_token=_require("LEADS_API_TOKEN"),
        api_host=os.environ.get("LEADS_API_HOST", "0.0.0.0"),  # noqa: S104
        api_port=int(os.environ.get("LEADS_API_PORT", "8080")),
        db_pool_min=int(os.environ.get("LEADS_DB_POOL_MIN", "1")),
        db_pool_max=int(os.environ.get("LEADS_DB_POOL_MAX", "5")),
    )


def _require(name: str) -> str:
    """Return a required env var or raise a clear error naming the variable."""
    value = os.environ.get(name)
    if not value:
        raise KeyError(f"Required environment variable {name!r} is not set")
    return value
