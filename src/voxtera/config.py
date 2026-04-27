"""Environment-based configuration for Voxtera.

All runtime config is read from `os.environ`. The bot entry point
(`voxtera.bot.main`) is responsible for calling `load_dotenv()` before
`load_settings()` so a developer's local `.env` file is honoured. Keeping the
`.env` side effect out of this module makes `load_settings()` a pure function
over the environment, which is essential for filesystem-isolated tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Runtime configuration loaded from environment.

    API keys are marked ``repr=False`` so they never appear in stack traces,
    log dumps, or interactive REPR output. To explicitly print them, access
    the field directly (e.g. ``settings.anthropic_api_key[:10]``).
    """

    anthropic_api_key: str = field(repr=False)
    openai_api_key: str = field(repr=False)
    log_level: str = "INFO"
    bot_name: str = "Voxtera"
    default_tts_voice: str = "nova"
    # VAD knobs. Pipecat's defaults (min_volume=0.6, confidence=0.7) are
    # tuned for headset mics; built-in laptop mics typically need both lower.
    vad_stop_secs: float = 0.8
    vad_start_secs: float = 0.2
    # Empirically, even loud speech on a built-in mic peaks around 0.05–0.07
    # RMS per chunk. 0.02 is a generous floor that still rejects pure silence.
    vad_min_volume: float = 0.02
    vad_confidence: float = 0.5
    # `auto` -> detect from OS locale; explicit code (e.g. `fr`) overrides.
    greeting_language: str = "auto"
    # voice = mic only (current default), text = keyboard only,
    # hybrid = both. In all modes the bot speaks its reply via TTS so
    # you can wear headphones in any setting.
    input_mode: str = "hybrid"


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill in your keys."
        )
    return value


def load_settings() -> Settings:
    """Load settings from `os.environ`.

    This function does NOT read `.env`. Call `dotenv.load_dotenv()` from your
    entry point first if you want `.env` honoured.
    """
    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        openai_api_key=_require("OPENAI_API_KEY"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        bot_name=os.environ.get("BOT_NAME", "Voxtera"),
        default_tts_voice=os.environ.get("DEFAULT_TTS_VOICE", "nova"),
        vad_stop_secs=float(os.environ.get("VAD_STOP_SECS", "0.8")),
        vad_start_secs=float(os.environ.get("VAD_START_SECS", "0.2")),
        vad_min_volume=float(os.environ.get("VAD_MIN_VOLUME", "0.3")),
        vad_confidence=float(os.environ.get("VAD_CONFIDENCE", "0.5")),
        greeting_language=os.environ.get("GREETING_LANGUAGE", "auto"),
        input_mode=os.environ.get("INPUT_MODE", "hybrid").lower(),
    )
