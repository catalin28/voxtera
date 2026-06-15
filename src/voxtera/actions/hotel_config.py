"""Hotel configuration — the per-deployment settings for action-taking.

Each Voxtera deployment serves one hotel. ``HotelConfig`` captures the bits
that vary between hotels: display name, official language for staff
communications, the Telegram channel that receives tickets, the subset of
categories the hotel accepts, and any hotel-specific facts to inject into
the LLM's context.

Configs live as YAML files in ``config/hotels/<hotel_id>.yaml`` at the
project root. The active hotel is selected via ``Settings.hotel_id``
(default ``"demo"``) and loaded once at bot startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml
from loguru import logger

from voxtera.actions.ticket import Category

# Required fields in the YAML; absence raises ValueError on load.
_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {"hotel_name", "official_language", "telegram_channel_id", "allowed_categories"}
)


@dataclass(frozen=True)
class HotelConfig:
    """Per-hotel runtime configuration for the action layer.

    Frozen because hotel config is loaded once at startup and should not
    mutate during a call. Reload requires a process restart.
    """

    hotel_id: str
    hotel_name: str
    # BCP-47 code or the language part only (e.g. "en", "en-US", "fr").
    # Used as the target language for the LLM-translated ticket summary.
    official_language: str
    # Telegram channel ID — must include the leading `-` and `100` prefix.
    telegram_channel_id: str
    # Subset of `Category` the hotel accepts. The LLM tool will only choose
    # from this tuple when filing tickets, even though `Category` defines more.
    allowed_categories: tuple[Category, ...]
    # Optional hotel-specific facts to inject into the LLM system prompt
    # (room count, address, brand voice notes). May be None.
    system_prompt_addendum: str | None = None
    # Optional fully custom spoken greeting for this property. When set, it is
    # used verbatim as the call's opening line (WhatsApp/phone), overriding the
    # generic "You've reached the concierge at {hotel_name}" template. Keep it
    # in the channel's default language (English on the phone path, since the
    # caller's language isn't known until they speak). May be None.
    greeting: str | None = None
    # IANA timezone for the property (e.g. "Europe/Istanbul"). Anchors
    # relative-date resolution ("tomorrow 7pm") to the HOTEL's local clock,
    # which is what a booking must use — a PSTN caller's own timezone is
    # unknown and irrelevant to when the restaurant table is reserved. When
    # None, callers fall back to the server clock (correct only for local dev).
    # See voxtera.call_center.hotel_time.hotel_local_now.
    timezone: str | None = None


def _project_root() -> Path:
    """Return the project root by walking up from this file's location.

    This file lives at ``<root>/src/voxtera/actions/hotel_config.py``,
    so ``parents[3]`` is the project root.
    """
    return Path(__file__).resolve().parents[3]


def _coerce_categories(raw: Any, source: Path) -> tuple[Category, ...]:
    """Convert the YAML ``allowed_categories`` list into Category enum values."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"Hotel config at {source}: `allowed_categories` must be a non-empty list")
    try:
        return tuple(Category(c) for c in raw)
    except ValueError as e:
        valid = ", ".join(c.value for c in Category)
        raise ValueError(
            f"Hotel config at {source}: unknown category in `allowed_categories`. "
            f"Allowed values: {valid}"
        ) from e


def _coerce_timezone(raw: Any, source: Path) -> str | None:
    """Validate an optional IANA timezone string from the YAML.

    Returns the trimmed value if it names a real zone, else None with a
    warning. A bad timezone must never fail the whole config load — booking
    date resolution degrades to the server clock instead (see
    ``hotel_time.hotel_local_now``).
    """
    if raw is None:
        return None
    tz = str(raw).strip()
    if not tz:
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning(
            "[hotel_config] config at {} has unknown timezone {!r}; "
            "ignoring (booking dates will use the server clock)",
            source,
            tz,
        )
        return None
    return tz


def load_hotel_config(hotel_id: str, config_dir: Path | None = None) -> HotelConfig:
    """Load a ``HotelConfig`` from ``<config_dir>/<hotel_id>.yaml``.

    Defaults ``config_dir`` to ``<project_root>/config/hotels``. Raises
    :class:`FileNotFoundError` if the config does not exist, and
    :class:`ValueError` if the file is malformed or contains an unknown
    category.
    """
    if config_dir is None:
        config_dir = _project_root() / "config" / "hotels"
    config_path = config_dir / f"{hotel_id}.yaml"

    if not config_path.is_file():
        raise FileNotFoundError(
            f"Hotel config not found: {config_path}. "
            f"Create it or set hotel_id to a hotel that has a config file."
        )

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(
            f"Hotel config at {config_path} must be a YAML mapping, " f"got {type(data).__name__}"
        )

    missing = _REQUIRED_FIELDS - data.keys()
    if missing:
        raise ValueError(
            f"Hotel config at {config_path} is missing required fields: {sorted(missing)}"
        )

    config = HotelConfig(
        hotel_id=hotel_id,
        hotel_name=str(data["hotel_name"]),
        official_language=str(data["official_language"]),
        telegram_channel_id=str(data["telegram_channel_id"]),
        allowed_categories=_coerce_categories(data["allowed_categories"], config_path),
        system_prompt_addendum=data.get("system_prompt_addendum"),
        greeting=(str(data["greeting"]).strip() if data.get("greeting") else None),
        timezone=_coerce_timezone(data.get("timezone"), config_path),
    )
    logger.info(
        "[hotel_config] Loaded hotel_id={} name={!r} lang={} tz={} "
        "categories={} channel={}",
        config.hotel_id,
        config.hotel_name,
        config.official_language,
        config.timezone or "(server clock)",
        len(config.allowed_categories),
        config.telegram_channel_id,
    )
    return config
