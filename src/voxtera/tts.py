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

# OpenAI tts-1 voices (provider-namespaced).
_VALID_OPENAI_TTS_VOICES: frozenset[str] = frozenset(
    {"alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"}
)
# Google Chirp 3 HD voice IDs we expose in the UI. The full catalogue has
# many more; this is a curated multilingual subset that all support the same
# locales (en-US, ro-RO, fr-FR, etc.). Adding more is just an entry here.
_VALID_GOOGLE_TTS_VOICES: frozenset[str] = frozenset(
    {
        "en-US-Chirp3-HD-Charon",
        "en-US-Chirp3-HD-Aoede",
        "en-US-Chirp3-HD-Kore",
        "en-US-Chirp3-HD-Leda",
        "en-US-Chirp3-HD-Orus",
        "en-US-Chirp3-HD-Puck",
        "en-US-Chirp3-HD-Zephyr",
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
        settings=GoogleTTSService.Settings(
            voice=voice,
            language=TTS_GOOGLE_DEFAULT_LANGUAGE,
        ),
    )
    logger.info("[tts] google chirp3-hd available (voice={})", voice)
    return tts


_TTS_BUILDERS: dict[str, Callable[[Settings], FrameProcessor | None]] = {
    "openai": _build_openai_tts,
    "google": _build_google_tts,
}


def _voices_for_tts_provider(provider: str) -> frozenset[str]:
    if provider == "google":
        return _VALID_GOOGLE_TTS_VOICES
    return _VALID_OPENAI_TTS_VOICES


def _default_voice_for_tts_provider(provider: str) -> str:
    if provider == "google":
        return TTS_GOOGLE_DEFAULT_VOICE
    return "nova"


# Combined catalog used wherever we need to validate against any provider's
# voice IDs (e.g. CLI argument validation that doesn't yet know which
# provider will be active).
_VALID_TTS_VOICES: frozenset[str] = _VALID_OPENAI_TTS_VOICES | _VALID_GOOGLE_TTS_VOICES
