"""Text-to-speech services, builders, and voice catalogs.

Houses the OpenAI ``tts-1`` and Google ``Chirp 3 HD`` builder functions, the
:data:`_TTS_BUILDERS` registry consumed by :mod:`voxtera.routing` and
:mod:`voxtera.pipeline`, and the per-provider voice catalogs the runtime
controllers use to validate user-selected voices.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openai.tts import OpenAITTSService

from voxtera.config import Settings
from voxtera.lang_config import (
    chirp3_voice_characters,
    google_locale_for,
    language_codes,
)

# tts-1 is faster; tts-1-hd is higher quality. Used by `_build_openai_tts`
# and surfaced in startup logs.
TTS_MODEL = "tts-1"
# Google Chirp 3 HD streaming TTS — neural multilingual voices.
# Voice IDs follow the pattern "<locale>-Chirp3-HD-<character>".
# See https://cloud.google.com/text-to-speech/docs/chirp3-hd for the full list.
# TTS_MODEL_GOOGLE is a display label (Google Chirp 3 HD isn't a single
# "model" parameter — the voice ID encodes the model family); it's surfaced
# in the dashboard's session_providers panel via voxtera.observability.
TTS_MODEL_GOOGLE = "chirp3-hd"
TTS_GOOGLE_DEFAULT_VOICE = "en-US-Chirp3-HD-Charon"
TTS_GOOGLE_DEFAULT_LANGUAGE = "en-US"

# Cartesia Sonic-3 streaming TTS — 90 ms time-to-first-audio, 42 languages.
# Model is set from settings.cartesia_model (default "sonic-3"); the constant
# below is the display label surfaced in the dashboard's session_providers
# panel via voxtera.observability.
TTS_MODEL_CARTESIA = "sonic-3"
# Default Cartesia voice: "Katie" (American English, stable). Cartesia's
# own docs recommend her for voice agents (calm, realistic). The HTML
# voice combo overrides this at runtime via voxtera-voice messages.
TTS_CARTESIA_DEFAULT_VOICE = "f786b574-daa5-4673-aa0c-cbe3e8534c02"

# ElevenLabs streaming TTS — ~75 ms time-to-first-audio with Flash v2.5,
# 32 languages. Model is set from settings.elevenlabs_model (default
# "eleven_flash_v2_5"); the constant below is the display label surfaced
# in the dashboard's session_providers panel via voxtera.observability.
TTS_MODEL_ELEVENLABS = "eleven_flash_v2_5"
# Default ElevenLabs voice: "Rachel" — ElevenLabs' canonical multilingual
# voice, widely used in voice-agent demos. Like Cartesia, ElevenLabs voices
# are inherently multilingual: a single voice ID produces audio in any of
# Flash v2.5's 32 supported languages when ``language`` is set per-utterance.
# Override at runtime via the HTML voice combo (voxtera-voice messages).
TTS_ELEVENLABS_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"

# Gemini 2.5 Flash TTS — Google's controllable, expressive TTS model.
# Unlike Chirp 3 HD (locale-encoded voice IDs), Gemini voices are bare
# character names (Kore, Charon, …) that are inherently multilingual across
# the model's ~24 supported languages; the locale is set per-utterance via
# the ``language`` setting. Same Google service-account credentials as Chirp.
# Chosen for Turkish: empirically the strongest TTS for tr-TR (see
# feat Turkish demo). NOT a token-stream engine, so first-audio latency is
# higher than Chirp/ElevenLabs — acceptable where voice quality is the goal.
TTS_MODEL_GEMINI = "gemini-2.5-flash-tts"
TTS_GEMINI_DEFAULT_VOICE = "Charon"
TTS_GEMINI_DEFAULT_LANGUAGE = "en-US"

# OpenAI tts-1 voices (provider-namespaced).
_VALID_OPENAI_TTS_VOICES: frozenset[str] = frozenset(
    {"alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"}
)


# Google Chirp 3 HD voice IDs we expose in the UI. The set is derived from
# config/languages.json (single source of truth shared with the demo
# frontend): for every language that flags ``tts.google.supported = true``,
# we generate ``{locale}-Chirp3-HD-{character}`` for each character in
# ``voices.google_chirp3_characters``. Adding a new language/voice happens
# in one JSON file, not here.
#
# NOTE: AutoTTSLanguageSwitcher (controllers.py) dynamically reconstructs
# voice IDs at runtime when the guest's language changes (e.g.
# en-US-Chirp3-HD-Charon → tr-TR-Chirp3-HD-Charon on Turkish detection),
# so this set is only consulted on manual selection via the UI dropdown
# or the DEFAULT_TTS_VOICE env var, NOT on automatic language switching.
def _build_valid_google_tts_voices() -> frozenset[str]:
    characters = [c["id"] for c in chirp3_voice_characters()]
    voices: set[str] = set()
    for code in language_codes():
        locale = google_locale_for(code)
        if locale is None:
            continue
        for character in characters:
            voices.add(f"{locale}-Chirp3-HD-{character}")
    return frozenset(voices)


_VALID_GOOGLE_TTS_VOICES: frozenset[str] = _build_valid_google_tts_voices()


# Gemini 2.5 Flash TTS voices: the same character names as Chirp 3 HD, but
# bare (no locale prefix) since Gemini voices are multilingual. Derived from
# the shared config so adding a character in languages.json covers both.
def _build_valid_gemini_tts_voices() -> frozenset[str]:
    return frozenset(str(c["id"]).strip() for c in chirp3_voice_characters() if c.get("id"))


_VALID_GEMINI_TTS_VOICES: frozenset[str] = _build_valid_gemini_tts_voices()
# Cartesia Sonic-3 voice UUIDs. Starter set — extend as we curate from
# https://play.cartesia.ai/voices. Adding a voice here is the only place
# you need to register it on the Python side; the HTML demo.html voice
# combo independently lists what's shown in the UI.
# NOTE: Cartesia Sonic-3 voices are multilingual — a single voice ID
# produces audio in any of the model's 42 supported languages when the
# ``language`` parameter is set per-utterance. Unlike Google Chirp 3 HD,
# we DO NOT need a separate voice ID per locale.
_VALID_CARTESIA_TTS_VOICES: frozenset[str] = frozenset(
    {
        # Katie — American English, stable (recommended for voice agents).
        "f786b574-daa5-4673-aa0c-cbe3e8534c02",
    }
)
# ElevenLabs voice IDs. Like Cartesia, ElevenLabs voices are multilingual:
# a single voice ID handles all of Flash v2.5's 32 supported languages
# when the per-utterance ``language`` is set. Starter set — extend as we
# curate from https://elevenlabs.io/app/voice-library. The HTML demo.html
# voice combo independently lists what's shown in the UI.
_VALID_ELEVENLABS_TTS_VOICES: frozenset[str] = frozenset(
    {
        # Rachel — calm, warm American English; ElevenLabs' canonical default.
        "21m00Tcm4TlvDq8ikWAM",
        # Adam — deep, mature American English; useful as a male counterpart.
        "pNInz6obpgDQGcFmaJgB",
        # Edward — British, dark, seductive, low.
        "goT3UYdM9bhm0n2lmKQx",
        # Bella — curated multilingual voice.
        "hpp4J3VqNfWAUOO0d1Us",
        # Jessica — curated multilingual voice.
        "cgSgspJ2msm6clMCkdW9",
        # Chris — curated multilingual voice.
        "iP95p4xoKVk53GoZ742B",
    }
)


def _build_openai_tts(settings: Settings) -> FrameProcessor | None:
    """Build the OpenAI tts-1 service if its credentials are present."""
    if not settings.openai_api_key:
        return None
    voice = settings.default_tts_voice
    if voice not in _VALID_OPENAI_TTS_VOICES:
        logger.warning(
            "[tts] DEFAULT_TTS_VOICE={!r} is not an OpenAI voice; falling back to 'nova'",
            voice,
        )
        voice = "nova"
    # Pin the TTS service to 24 kHz — OpenAI's tts-1 only returns 24 kHz PCM
    # regardless of what rate you ask for. Without this pin, the service
    # inherits the pipeline's audio_out_sample_rate (typically 48 kHz for
    # WebRTC) and tags every TTSAudioRawFrame as 48 kHz, even though the
    # actual bytes are still 24 kHz audio. The downstream
    # BaseOutputTransport resampler then sees ``frame.sample_rate == target``
    # and skips resampling — so 24 kHz audio gets played at 48 kHz, i.e.
    # chipmunk speed (observed in Brave + Safari, 2026-05-05).
    #
    # With sample_rate=24000 here, frames are correctly tagged 24 kHz,
    # the resampler kicks in to upsample 24 → 48 kHz before Daily sends
    # the audio over WebRTC, and the browser hears normal-speed speech.
    tts = OpenAITTSService(
        api_key=settings.openai_api_key,
        sample_rate=24000,
        settings=OpenAITTSService.Settings(model=TTS_MODEL, voice=voice),
    )
    logger.info("[tts] openai available (model={}, voice={})", TTS_MODEL, voice)
    return tts


def _build_google_tts(settings: Settings) -> FrameProcessor | None:
    """Build the Google Chirp 3 HD streaming TTS if its credentials are valid."""
    if not settings.google_tts_enabled:
        logger.info("[tts] google TTS disabled via GOOGLE_TTS_ENABLED=false")
        return None
    if not settings.google_application_credentials:
        return None
    creds_path = Path(settings.google_application_credentials).expanduser()
    if not creds_path.is_absolute():
        creds_path = Path.cwd() / creds_path
    if not creds_path.exists():
        logger.warning(
            "[tts] google credentials path does not exist: {} — google TTS disabled",
            creds_path,
        )
        return None
    try:
        from pipecat.services.google.tts import GoogleTTSService
        from pipecat.services.tts_service import TextAggregationMode
    except ImportError:
        logger.warning(
            "[tts] google TTS extras not installed — install with: uv add 'pipecat-ai[google]'"
        )
        return None

    voice = settings.default_tts_voice
    if voice not in _VALID_GOOGLE_TTS_VOICES:
        # When DEFAULT_TTS_VOICE is an OpenAI voice (e.g. 'nova'), fall back to
        # the default Chirp 3 HD voice so the Google branch starts in a valid
        # state. The browser is free to pick a different one immediately.
        voice = TTS_GOOGLE_DEFAULT_VOICE

    tts = GoogleTTSService(
        credentials_path=str(creds_path),
        # text_aggregation_mode=TOKEN streams individual LLM tokens to the
        # Google Chirp 3 HD gRPC endpoint as they arrive, instead of the
        # default SENTENCE mode which waits for a period/period before flushing.
        # Measured impact: cuts the LLM→TTS gap from ~500ms (sentence wait)
        # to <100ms (immediate stream). Chirp 3 HD's gRPC streaming
        # synthesize handles partial input cleanly, so audio starts arriving
        # at transport_out within ~250ms of the LLM's first token rather than
        # waiting for the full sentence. Total perceived-latency saving
        # ~400ms on a typical one-sentence reply.
        text_aggregation_mode=TextAggregationMode.TOKEN,
        settings=GoogleTTSService.Settings(
            voice=voice,
            language=TTS_GOOGLE_DEFAULT_LANGUAGE,
        ),
    )
    logger.info("[tts] google chirp3-hd available (voice={}, mode=token-stream)", voice)
    return tts


def _build_gemini_tts(settings: Settings) -> FrameProcessor | None:
    """Build the Gemini 2.5 Flash TTS branch if Google credentials are valid.

    Reuses the same Google service-account JSON as the Chirp 3 HD branch
    (``GOOGLE_APPLICATION_CREDENTIALS``) — no separate key. Gemini TTS is a
    prompt-controllable, expressive model: an optional ``GEMINI_TTS_PROMPT``
    env var sets a persistent style instruction (e.g. the concierge persona's
    "warm, polished" register).

    Notes:
    * Output is fixed at 24 kHz — pinned here to match the rest of the
      pipeline and silence pipecat's sample-rate-mismatch warning.
    * Unlike Chirp 3 HD's gRPC token-stream, Gemini synthesises from buffered
      text, so first-audio latency is higher. We therefore keep SENTENCE
      aggregation (the default) rather than TOKEN — token-by-token offers no
      latency win here and risks choppier prosody.
    """
    if not settings.google_tts_enabled:
        return None
    if not settings.google_application_credentials:
        return None
    creds_path = Path(settings.google_application_credentials).expanduser()
    if not creds_path.is_absolute():
        creds_path = Path.cwd() / creds_path
    if not creds_path.exists():
        logger.warning(
            "[tts] google credentials path does not exist: {} — gemini TTS disabled",
            creds_path,
        )
        return None
    try:
        from pipecat.services.google.tts import GeminiTTSService
    except ImportError:
        logger.warning(
            "[tts] gemini TTS not available in this pipecat build — needs a recent "
            "pipecat-ai[google] (GeminiTTSService). Skipping gemini branch."
        )
        return None

    import os

    voice = settings.default_tts_voice
    if voice not in _VALID_GEMINI_TTS_VOICES:
        voice = TTS_GEMINI_DEFAULT_VOICE

    prompt = (os.environ.get("GEMINI_TTS_PROMPT") or "").strip()

    settings_kwargs: dict = {
        "model": TTS_MODEL_GEMINI,
        "voice": voice,
        "language": TTS_GEMINI_DEFAULT_LANGUAGE,
    }
    if prompt:
        settings_kwargs["prompt"] = prompt

    tts = GeminiTTSService(
        credentials_path=str(creds_path),
        # Google TTS always emits 24 kHz; pin it to match the pipeline and
        # avoid the resampling-mismatch warning (same reasoning as ElevenLabs).
        sample_rate=24000,
        settings=GeminiTTSService.Settings(**settings_kwargs),
    )
    logger.info(
        "[tts] gemini available (model={}, voice={}, prompt={})",
        TTS_MODEL_GEMINI,
        voice,
        "set" if prompt else "none",
    )
    return tts


def _build_cartesia_tts(settings: Settings) -> FrameProcessor | None:
    """Build the Cartesia Sonic-3 streaming TTS if its credentials are present.

    Sonic-3 is Cartesia's flagship streaming TTS model with ~90 ms
    time-to-first-audio across 42 languages — the fastest TTS shipping
    today. Voices are multilingual: a single voice ID produces audio in
    any supported language when ``language`` is set per-utterance.

    Pipecat 1.0.0's canonical API is used (``settings=CartesiaTTSService
    .Settings(...)``); the older ``voice_id=`` / ``model=`` init kwargs
    are deprecated and emit warnings. Sample rate is pinned to 24 kHz
    to match the rest of the pipeline (same reason as OpenAI tts-1 —
    avoid downstream resampling mismatch).
    """
    if not settings.cartesia_api_key:
        return None
    try:
        from pipecat.services.cartesia.tts import CartesiaTTSService
        from pipecat.services.tts_service import TextAggregationMode
        from pipecat.transcriptions.language import Language
    except ImportError:
        logger.warning(
            "[tts] cartesia TTS extras not installed — install with: "
            "uv add 'pipecat-ai[cartesia]'"
        )
        return None

    # Voice selection priority:
    # 1. DEFAULT_TTS_VOICE env, if it's a known Cartesia UUID
    # 2. Hardcoded fallback (Katie)
    # The HTML voice combo overrides at runtime via voxtera-voice messages
    # regardless of which one is used at boot.
    voice = settings.default_tts_voice
    if voice not in _VALID_CARTESIA_TTS_VOICES:
        voice = TTS_CARTESIA_DEFAULT_VOICE

    tts = CartesiaTTSService(
        api_key=settings.cartesia_api_key,
        # Pin sample rate to 24 kHz to match the rest of the pipeline.
        # Cartesia's streaming endpoint produces audio at the requested
        # rate; mis-matching this with the pipeline causes the same
        # "chipmunk speed" resampling bug we hit with OpenAI tts-1 in
        # the 2026-05-05 Brave/Safari incident (see _build_openai_tts).
        sample_rate=24000,
        # text_aggregation_mode=TOKEN streams LLM tokens to Cartesia as
        # they arrive instead of waiting for sentence-ending punctuation.
        # Saves ~200-300 ms of perceived latency per turn — same trick
        # we use in the Google builder.
        text_aggregation_mode=TextAggregationMode.TOKEN,
        settings=CartesiaTTSService.Settings(
            model=settings.cartesia_model,
            voice=voice,
            language=Language.EN,
        ),
    )
    logger.info(
        "[tts] cartesia available (model={}, voice={}, mode=token-stream)",
        settings.cartesia_model,
        voice,
    )
    return tts


def _build_elevenlabs_tts(settings: Settings) -> FrameProcessor | None:
    """Build the ElevenLabs streaming TTS if its credentials are present.

    ElevenLabs Flash v2.5 (the default) provides ~75 ms time-to-first-audio
    across 32 languages — the lowest-latency premium TTS available. Like
    Cartesia, voices are inherently multilingual: a single voice ID produces
    audio in any supported language when ``language`` is set per-utterance.

    Uses Pipecat's WebSocket-streaming variant (``ElevenLabsTTSService``),
    not the HTTP variant — streaming is required to hit the advertised
    sub-100ms TTFA. Sample rate is pinned to 24 kHz to match the rest of
    the pipeline (same reason as OpenAI/Cartesia — avoid the chipmunk-speed
    resampling bug we hit on 2026-05-05).
    """
    if not settings.elevenlabs_api_key:
        return None
    try:
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
        from pipecat.services.tts_service import TextAggregationMode
    except ImportError:
        logger.warning(
            "[tts] elevenlabs TTS extras not installed — install with: "
            "uv add 'pipecat-ai[elevenlabs]'"
        )
        return None

    # Voice selection priority:
    # 1. DEFAULT_TTS_VOICE env, if it's a known ElevenLabs voice ID
    # 2. ELEVENLABS_VOICE_ID env (the dedicated knob)
    # 3. Hardcoded fallback (Rachel)
    # The HTML voice combo overrides at runtime via voxtera-voice messages
    # regardless of which one is used at boot.
    voice = settings.default_tts_voice
    if voice not in _VALID_ELEVENLABS_TTS_VOICES:
        voice = (
            settings.elevenlabs_voice_id
            if settings.elevenlabs_voice_id in _VALID_ELEVENLABS_TTS_VOICES
            else TTS_ELEVENLABS_DEFAULT_VOICE
        )

    tts = ElevenLabsTTSService(
        api_key=settings.elevenlabs_api_key,
        voice_id=voice,
        model=settings.elevenlabs_model,
        # Pin sample rate to 24 kHz to match the rest of the pipeline.
        # ElevenLabs supports pcm_8000..pcm_48000; 24 kHz keeps parity with
        # OpenAI/Cartesia and avoids downstream resampling mismatches.
        sample_rate=24000,
        # text_aggregation_mode=TOKEN streams LLM tokens to ElevenLabs as
        # they arrive instead of waiting for sentence-ending punctuation.
        # Saves ~200-300 ms of perceived latency per turn — same trick we
        # use in the Google and Cartesia builders.
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )
    logger.info(
        "[tts] elevenlabs available (model={}, voice={}, mode=token-stream)",
        settings.elevenlabs_model,
        voice,
    )
    return tts


_TTS_BUILDERS: dict[str, Callable[[Settings], FrameProcessor | None]] = {
    "openai": _build_openai_tts,
    "google": _build_google_tts,
    "gemini": _build_gemini_tts,
    "cartesia": _build_cartesia_tts,
    "elevenlabs": _build_elevenlabs_tts,
}


def _voices_for_tts_provider(provider: str) -> frozenset[str]:
    if provider == "google":
        return _VALID_GOOGLE_TTS_VOICES
    if provider == "gemini":
        return _VALID_GEMINI_TTS_VOICES
    if provider == "cartesia":
        return _VALID_CARTESIA_TTS_VOICES
    if provider == "elevenlabs":
        return _VALID_ELEVENLABS_TTS_VOICES
    return _VALID_OPENAI_TTS_VOICES


def _default_voice_for_tts_provider(provider: str) -> str:
    if provider == "google":
        return TTS_GOOGLE_DEFAULT_VOICE
    if provider == "gemini":
        return TTS_GEMINI_DEFAULT_VOICE
    if provider == "cartesia":
        return TTS_CARTESIA_DEFAULT_VOICE
    if provider == "elevenlabs":
        return TTS_ELEVENLABS_DEFAULT_VOICE
    return "nova"


# Combined catalog used wherever we need to validate against any provider's
# voice IDs (e.g. CLI argument validation that doesn't yet know which
# provider will be active).
_VALID_TTS_VOICES: frozenset[str] = (
    _VALID_OPENAI_TTS_VOICES
    | _VALID_GOOGLE_TTS_VOICES
    | _VALID_GEMINI_TTS_VOICES
    | _VALID_CARTESIA_TTS_VOICES
    | _VALID_ELEVENLABS_TTS_VOICES
)
