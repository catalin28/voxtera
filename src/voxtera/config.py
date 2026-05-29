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
    # vad_stop_secs is the silence window after which we declare the user
    # done. 0.2s was too aggressive — normal speech has ~200-300ms inter-word
    # gaps, causing VAD chatter and multiple STT calls per turn (+1.5s overhead).
    # 0.3s is the tuned balance: no chatter on natural speech, minimal wait
    # after the user finishes (~200ms saved vs 0.5s). Override via VAD_STOP_SECS.
    vad_stop_secs: float = 0.3
    vad_start_secs: float = 0.2
    # Empirically, even loud speech on a built-in mic peaks around 0.05–0.07
    # RMS per chunk. 0.02 is a generous floor that still rejects pure silence.
    vad_min_volume: float = 0.02
    vad_confidence: float = 0.5
    # SmartTurn ONNX end-of-turn model thread count. This stage dominates the
    # STT→LLM latency gap, and the thread count is the main lever on it: too
    # few threads and inference is slow, more threads than the box has cores
    # and they thrash. Auto-detected from the server's core count at startup
    # by _auto_smart_turn_cpu_count(); the SMART_TURN_CPU_COUNT env var is an
    # optional explicit override (mainly for testing).
    smart_turn_cpu_count: int = 2
    # SmartTurn hard fallback: max seconds of CONTINUOUS silence (counted from
    # the VAD stop event) after which the turn is force-ended even if the
    # SmartTurn ONNX model still predicts "incomplete". This is pipecat's
    # SmartTurnParams.stop_secs — its default is 3.0, which trace analysis
    # identified as the entire source of the ~3s stt->llm p95 spikes: on turns
    # where the model mispredicts "incomplete", the bot waits out this whole
    # window before the turn fires. Lowered to 2.0 to cap that worst case.
    # Override via SMART_TURN_STOP_SECS.
    smart_turn_stop_secs: float = 2.0
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
    # Interruption resume: when a guest barges in early in the bot's reply,
    # inject a context note so the answering LLM can decide whether to return
    # to the unfinished topic. Only has any effect when allow_interruptions is
    # on (no barge-in → no InterruptionFrame → the processor stays inert).
    interruption_resume_enabled: bool = True
    # A barge-in counts as "early enough to resume" only if it lands within
    # this many seconds of the reply starting. Past that the reply is treated
    # as near-complete and is not resumed.
    interruption_resume_window_secs: float = 5.0
    # Pipeline idle timeout in seconds. `None` disables idle cancellation.
    # Demo sessions often include long pauses while users reload browser UI.
    pipeline_idle_timeout_secs: float | None = None
    # RAG: inject hotel knowledge into LLM context before each turn.
    rag_enabled: bool = False
    hotel_id: str = "demo"
    # Number of chunks RAG injects into the LLM context. Each chunk costs
    # ~100-500 prefill tokens. Lowering from 5 to 3 typically saves 50-100ms
    # on llm_ttft with negligible accuracy loss for concentrated hotel KBs.
    rag_top_k: int = 3
    # Minimum cosine similarity for a chunk to be returned. 0.25 is permissive;
    # raise toward 0.4 for stricter filtering.
    rag_min_score: float = 0.25
    # STT provider selection: whisper | deepgram | google
    stt_provider: str = "whisper"
    # TTS provider selection: openai | google | cartesia | elevenlabs
    # When transport_mode=daily and the relevant credentials are configured,
    # all providers are built and the active one is selected at runtime via
    # the browser UI (voxtera-tts-provider message). This field only sets
    # the initial selection.
    tts_provider: str = "openai"
    # Set to False to skip building the Google Chirp 3 HD TTS branch entirely
    # (e.g. when the Cloud Text-to-Speech API is not yet enabled in your GCP
    # project). When False, only OpenAI TTS is available regardless of
    # GOOGLE_APPLICATION_CREDENTIALS.
    google_tts_enabled: bool = True
    # Deepgram API key (required when stt_provider=deepgram).
    deepgram_api_key: str | None = field(default=None, repr=False)
    # Gladia API key (required when stt_provider=gladia). Solaria-1 is the
    # multilingual streaming model with native code-switching support.
    gladia_api_key: str | None = field(default=None, repr=False)
    # Gladia region: "eu-west" (default) or "us-west". Pick the one closer
    # to your deployment to minimise WebSocket round-trip time.
    gladia_region: str = "eu-west"
    # Optional Gladia candidate language set. Two semantics, depending on
    # whether ``gladia_code_switching`` is enabled below:
    #
    # * Empty (default): no ``language_config`` is sent. Gladia auto-detects
    #   each utterance across all 99 supported languages.
    # * Non-empty + code_switching=False (recommended): "detect within this
    #   set" — Gladia picks ONE language per utterance from the listed codes.
    #   This is Gladia's documented best-accuracy mode and what tourism
    #   demos typically want (top 5-15 tourism languages).
    # * Non-empty + code_switching=True: enables mid-utterance switching
    #   like "where is the gare?". Requires ≥2 languages (a single-language
    #   list with code_switching=True is contradictory and Gladia silently
    #   produces no transcripts).
    gladia_languages: tuple[str, ...] = field(default_factory=tuple)
    # Enable mid-utterance code-switching. Requires gladia_languages to
    # contain at least 2 codes. Default off because constrained detection
    # alone fixes most accuracy issues without restricting to a single
    # within-utterance language.
    gladia_code_switching: bool = False
    # Google credentials file path (required when stt_provider=google or
    # tts_provider=google).
    google_application_credentials: str | None = None
    # Cartesia API key (required when tts_provider=cartesia). Sonic-3 is
    # the multilingual streaming TTS model with sub-100ms TTFA.
    cartesia_api_key: str | None = field(default=None, repr=False)
    # Cartesia TTS model. "sonic-3" is the latest (90ms TTFA, 42 languages).
    # Pin to a snapshot like "sonic-3-2026-01-12" for production stability.
    cartesia_model: str = "sonic-3"
    # ElevenLabs API key (required when tts_provider=elevenlabs). Flash v2.5
    # is the real-time-grade model with ~75ms TTFA across 32 languages.
    elevenlabs_api_key: str | None = field(default=None, repr=False)
    # ElevenLabs TTS model. "eleven_flash_v2_5" = 75ms TTFA, 32 languages
    # (cheapest at $0.05/1K chars). "eleven_turbo_v2_5" = mid-tier quality.
    # Note: only the *_v2_5 models support per-utterance language codes; v3
    # and multilingual_v2 detect language from text only.
    elevenlabs_model: str = "eleven_flash_v2_5"
    # ElevenLabs voice ID to use at boot. Single multilingual voice IDs are
    # the norm (one voice handles all 32 languages). Browse the library at
    # https://elevenlabs.io/app/voice-library — pick a voice and copy its ID.
    # Default = "Rachel" (21m00Tcm4TlvDq8ikWAM), ElevenLabs' canonical voice.
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
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
    # Path to the per-language Whisper confidence-threshold JSON file. When
    # set, low-confidence transcriptions are dropped before reaching the LLM,
    # which suppresses Whisper substitution hallucinations like "the water is
    # not running" → "the White House". When unset (or the file is missing),
    # the bot falls back to a single hardcoded threshold pair (lenient).
    # See config/stt_thresholds.json for the schema and tuned defaults.
    stt_thresholds_path: str | None = "config/stt_thresholds.json"
    # Actions feature: register the `create_ticket` LLM tool, post tickets to
    # the configured Telegram channel, and run the staff-button listener as
    # a background task. Requires TELEGRAM_BOT_TOKEN. The Telegram channel
    # ID is read from the per-hotel config in config/hotels/<hotel_id>.yaml.
    actions_enabled: bool = False
    # Instant-acknowledgment filler: when True, the bot plays a short,
    # language-matched backchannel ("One moment.", "Let me check that.") the
    # instant the guest stops speaking. It runs in parallel with the LLM and
    # masks the STT->LLM->TTS latency gap with natural speech instead of
    # silence — it never delays the real answer. See voxtera.prompts.fillers
    # and voxtera.controllers.InstantAckFiller.
    filler_enabled: bool = False
    # Call recording: write a per-call record to logs/calls/<session_id>/.
    # When True, record.json (metadata header + turn-by-turn transcript) is
    # produced for every call — the building block for the post-call CRM
    # summary webhook. See voxtera.call_record.
    call_recording_enabled: bool = True
    # When True (and call_recording_enabled is also True), the call audio is
    # additionally captured as a stereo WAV (recording.wav — guest left, bot
    # right). Set False to keep transcripts + metadata without storing audio.
    call_recording_audio: bool = True
    # Diagnostic: when True, tap a WAV at each pre-gate mic stage
    # (stage_*.wav) so the audio can be compared stage-by-stage to find
    # which processor is breaking it. Off by default — only enable when
    # debugging audio dropouts, as it writes several extra WAVs per call.
    stage_audio_debug: bool = False


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Copy .env.example to .env and fill in your keys."
        )
    return value


def _effective_cpu_count() -> int:
    """Number of CPU cores actually available to *this process*.

    Prefers ``os.sched_getaffinity`` (Linux) because it respects CPU-affinity
    and cgroup limits — so it stays correct when the bot runs inside a
    constrained container (e.g. Pipecat Cloud, Docker with a CPU quota).
    ``os.sched_getaffinity`` does not exist on macOS or Windows, so on those
    platforms (e.g. a local M1 dev box) this falls back to ``os.cpu_count``.
    The ``OSError`` arm is belt-and-braces in case the syscall itself fails.
    """
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return os.cpu_count() or 2


def _auto_smart_turn_cpu_count() -> int:
    """Choose a SmartTurn ONNX thread count from the host's core count.

    Detected fresh at startup so the bot self-tunes to whatever server it is
    deployed on. SmartTurn inference is a short per-turn burst, so it can use
    most of the cores while it runs:

    * 1 core   -> 1
    * 2 cores  -> 2  (verified sweet spot on a 2-vCPU droplet — using both
                      cores roughly halves inference vs. a single thread)
    * 3+ cores -> cores - 1  (leave one core free for the audio loop)
    * capped at 4 — the model stops getting faster past ~4 threads and extra
      threads only add context-switch contention.
    """
    cores = _effective_cpu_count()
    if cores <= 2:
        return max(1, cores)
    return min(4, cores - 1)


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
        vad_stop_secs=float(os.environ.get("VAD_STOP_SECS", "0.3")),
        vad_start_secs=float(os.environ.get("VAD_START_SECS", "0.2")),
        vad_min_volume=float(os.environ.get("VAD_MIN_VOLUME", "0.02")),
        vad_confidence=float(os.environ.get("VAD_CONFIDENCE", "0.5")),
        # SmartTurn thread count — auto-detected from the server's core count
        # at startup (see _auto_smart_turn_cpu_count). SMART_TURN_CPU_COUNT
        # still works as an explicit override, but leaving it unset is
        # recommended so the bot self-tunes to whatever box it runs on.
        smart_turn_cpu_count=int(
            os.environ.get("SMART_TURN_CPU_COUNT") or _auto_smart_turn_cpu_count()
        ),
        smart_turn_stop_secs=float(os.environ.get("SMART_TURN_STOP_SECS", "2.0")),
        greeting_language=os.environ.get("GREETING_LANGUAGE", "auto"),
        input_mode=os.environ.get("INPUT_MODE", "hybrid").lower(),
        transport_mode=os.environ.get("TRANSPORT_MODE", "local").lower(),
        rnnoise_enabled=os.environ.get("RNNOISE_ENABLED", "false").lower() in ("1", "true", "yes"),
        allow_interruptions=os.environ.get("ALLOW_INTERRUPTIONS", "false").lower()
        in ("1", "true", "yes"),
        interruption_resume_enabled=os.environ.get("INTERRUPTION_RESUME_ENABLED", "true").lower()
        in ("1", "true", "yes"),
        interruption_resume_window_secs=float(
            os.environ.get("INTERRUPTION_RESUME_WINDOW_SECS", "5.0")
        ),
        pipeline_idle_timeout_secs=idle_timeout_secs,
        rag_enabled=os.environ.get("RAG_ENABLED", "false").lower() in ("1", "true", "yes"),
        hotel_id=os.environ.get("HOTEL_ID", "demo"),
        rag_top_k=int(os.environ.get("RAG_TOP_K", "3")),
        rag_min_score=float(os.environ.get("RAG_MIN_SCORE", "0.25")),
        stt_provider=os.environ.get("STT_PROVIDER", "whisper").lower(),
        tts_provider=os.environ.get("TTS_PROVIDER", "openai").lower(),
        google_tts_enabled=os.environ.get("GOOGLE_TTS_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY"),
        gladia_api_key=os.environ.get("GLADIA_API_KEY"),
        gladia_region=os.environ.get("GLADIA_REGION", "eu-west").lower(),
        # Accept the new name first; fall back to the legacy
        # GLADIA_CODE_SWITCH_LANGUAGES so already-deployed .env files keep
        # working until they're rotated.
        gladia_languages=tuple(
            code.strip().lower()
            for code in (
                os.environ.get("GLADIA_LANGUAGES")
                or os.environ.get("GLADIA_CODE_SWITCH_LANGUAGES", "")
            ).split(",")
            if code.strip()
        ),
        gladia_code_switching=os.environ.get("GLADIA_CODE_SWITCHING", "false").lower()
        in ("1", "true", "yes"),
        google_application_credentials=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        cartesia_api_key=os.environ.get("CARTESIA_API_KEY"),
        cartesia_model=os.environ.get("CARTESIA_MODEL", "sonic-3"),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY"),
        elevenlabs_model=os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
        elevenlabs_voice_id=os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
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
        filler_enabled=os.environ.get("FILLER_ENABLED", "false").lower() in ("1", "true", "yes"),
        call_recording_enabled=os.environ.get("CALL_RECORDING_ENABLED", "true").lower()
        not in ("0", "false", "no"),
        call_recording_audio=os.environ.get("CALL_RECORD_AUDIO", "true").lower()
        not in ("0", "false", "no"),
        stage_audio_debug=os.environ.get("STAGE_AUDIO_DEBUG", "false").lower()
        in ("1", "true", "yes"),
        stt_thresholds_path=os.environ.get("STT_THRESHOLDS_PATH", "config/stt_thresholds.json")
        or None,
    )
