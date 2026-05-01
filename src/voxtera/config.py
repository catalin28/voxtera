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
    vad_stop_secs: float = 0.2
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
    # Runtime transport: local laptop audio or Daily WebRTC room.
    transport_mode: str = "local"
    # Optional mic-side denoiser for demo environments.
    rnnoise_enabled: bool = False
    # Whether user speech can interrupt/cancel in-flight bot responses.
    # Default False for noisy speaker scenarios to avoid <empty> replies.
    allow_interruptions: bool = False
    # Pipeline idle timeout in seconds. `None` disables idle cancellation.
    # Demo sessions often include long pauses while users reload browser UI.
    pipeline_idle_timeout_secs: float | None = None
    # RAG: inject hotel knowledge into LLM context before each turn.
    rag_enabled: bool = False
    hotel_id: str = "demo"
    # STT provider selection: whisper | deepgram | google
    stt_provider: str = "whisper"
    # TTS provider selection: openai | google
    # When transport_mode=daily and Google credentials are configured, both
    # providers are built and the active one is selected at runtime via the
    # browser UI (voxtera-tts-provider message). This field only sets the
    # initial selection.
    tts_provider: str = "openai"
    # Set to False to skip building the Google Chirp 3 HD TTS branch entirely
    # (e.g. when the Cloud Text-to-Speech API is not yet enabled in your GCP
    # project). When False, only OpenAI TTS is available regardless of
    # GOOGLE_APPLICATION_CREDENTIALS.
    google_tts_enabled: bool = True
    # Deepgram API key (required when stt_provider=deepgram).
    deepgram_api_key: str | None = field(default=None, repr=False)
    # Google credentials file path (required when stt_provider=google or
    # tts_provider=google).
    google_application_credentials: str | None = None
    # Daily WebRTC config for browser-based transport.
    daily_api_key: str | None = field(default=None, repr=False)
    daily_domain: str | None = None
    daily_room_name: str | None = None
    # STT prompt for language detection and transcription hints.
    # Disabled by default: the English prompt biases Whisper toward English
    # even when the user speaks another language, causing garbled transcripts.
    # Enable via STT_PROMPT_ENABLED=true only if you need English hotel vocab
    # disambiguation and your users exclusively speak English.
    # Override the prompt text via STT_PROMPT env var.
    stt_prompt_enabled: bool = False
    stt_prompt: str = (
        "Hotel concierge conversation. Guest asking about rooms, breakfast, "
        "spa treatments, pool hours, restaurant menu, dishes, check-in, "
        "check-out, wifi password, taxi, museum, airport, Paris, Louvre."
    )
    # Actions feature: register the `create_ticket` LLM tool, post tickets to
    # the configured Telegram channel, and run the staff-button listener as
    # a background task. Requires TELEGRAM_BOT_TOKEN. The Telegram channel
    # ID is read from the per-hotel config in config/hotels/<hotel_id>.yaml.
    actions_enabled: bool = False


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
    idle_timeout_raw = os.environ.get("PIPELINE_IDLE_TIMEOUT_SECS", "none").strip().lower()
    if idle_timeout_raw in {"", "none", "null", "off", "false", "0"}:
        idle_timeout_secs: float | None = None
    else:
        idle_timeout_secs = float(idle_timeout_raw)

    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        openai_api_key=_require("OPENAI_API_KEY"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        bot_name=os.environ.get("BOT_NAME", "Voxtera"),
        default_tts_voice=os.environ.get("DEFAULT_TTS_VOICE", "nova"),
        vad_stop_secs=float(os.environ.get("VAD_STOP_SECS", "0.2")),
        vad_start_secs=float(os.environ.get("VAD_START_SECS", "0.2")),
        vad_min_volume=float(os.environ.get("VAD_MIN_VOLUME", "0.02")),
        vad_confidence=float(os.environ.get("VAD_CONFIDENCE", "0.5")),
        greeting_language=os.environ.get("GREETING_LANGUAGE", "auto"),
        input_mode=os.environ.get("INPUT_MODE", "hybrid").lower(),
        transport_mode=os.environ.get("TRANSPORT_MODE", "local").lower(),
        rnnoise_enabled=os.environ.get("RNNOISE_ENABLED", "false").lower() in ("1", "true", "yes"),
        allow_interruptions=os.environ.get("ALLOW_INTERRUPTIONS", "false").lower()
        in ("1", "true", "yes"),
        pipeline_idle_timeout_secs=idle_timeout_secs,
        rag_enabled=os.environ.get("RAG_ENABLED", "false").lower() in ("1", "true", "yes"),
        hotel_id=os.environ.get("HOTEL_ID", "demo"),
        stt_provider=os.environ.get("STT_PROVIDER", "whisper").lower(),
        tts_provider=os.environ.get("TTS_PROVIDER", "openai").lower(),
        google_tts_enabled=os.environ.get("GOOGLE_TTS_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY"),
        google_application_credentials=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        daily_api_key=os.environ.get("DAILY_API_KEY"),
        daily_domain=os.environ.get("DAILY_DOMAIN"),
        daily_room_name=os.environ.get("DAILY_ROOM_NAME"),
        stt_prompt_enabled=os.environ.get("STT_PROMPT_ENABLED", "false").lower()
        in ("1", "true", "yes"),
        stt_prompt=os.environ.get(
            "STT_PROMPT",
            (
                "Hotel concierge conversation. Guest asking about rooms, breakfast, "
                "spa treatments, pool hours, restaurant menu, dishes, check-in, "
                "check-out, wifi password, taxi, museum, airport, Paris, Louvre."
            ),
        ),
        actions_enabled=os.environ.get("ACTIONS_ENABLED", "false").lower() in ("1", "true", "yes"),
    )
