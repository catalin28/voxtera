"""Environment-based configuration for Voxtera.

All runtime config is loaded from environment variables (typically via a `.env`
file in the repo root). See `.env.example` for the canonical list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Runtime configuration loaded from environment."""

    anthropic_api_key: str
    openai_api_key: str
    log_level: str
    bot_name: str
    default_tts_voice: str
    vad_stop_secs: float


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill in your keys."
        )
    return value


def load_settings() -> Settings:
    """Load settings from environment (and `.env` if present)."""
    load_dotenv()
    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        openai_api_key=_require("OPENAI_API_KEY"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        bot_name=os.environ.get("BOT_NAME", "Voxtera"),
        default_tts_voice=os.environ.get("DEFAULT_TTS_VOICE", "nova"),
        vad_stop_secs=float(os.environ.get("VAD_STOP_SECS", "0.8")),
    )
