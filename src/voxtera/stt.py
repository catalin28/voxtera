"""Speech-to-text services, builders, and language maps.

Houses everything STT-specific that used to live in ``voxtera.bot``:

- :class:`_MultilingualWhisperSTT` — Whisper subclass with auto language detection.
- :class:`_ResilientGoogleSTTService` — mixin that quietly recovers from
  transient Google INTERNAL errors and from 409 stream timeouts.
- ``_build_whisper_stt`` / ``_build_deepgram_stt`` / ``_build_google_stt``
  builders, plus the :data:`_STT_BUILDERS` registry and :func:`_build_stt`
  factory used by local mode.
- Language tables (:data:`_VALID_STT_LANGUAGES`,
  :data:`_GOOGLE_AUTO_LANGUAGES`, :data:`_GOOGLE_LANGUAGE_MAP`) and the
  :func:`_google_languages_for_selection` helper used by the routing layer
  and the runtime language switcher.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Callable
from pathlib import Path

from loguru import logger
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.whisper.base_stt import Transcription
from pipecat.transcriptions.language import Language

from voxtera.config import Settings

# OpenAI Whisper API model identifier.
STT_MODEL_WHISPER = "whisper-1"
# Deepgram Nova-3 multilingual streaming model.
STT_MODEL_DEEPGRAM = "nova-3-general"
# Google Speech-to-Text V2 streaming model name.
STT_MODEL_GOOGLE = "latest_long"


class _MultilingualWhisperSTT(OpenAISTTService):
    """Whisper STT with language auto-detection (omits the language param).

    Uses verbose_json to capture the detected language for downstream
    logging and consistency checks.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_detected_language: str | None = None

    async def _transcribe(self, audio: bytes) -> Transcription:
        kwargs: dict = {
            "file": ("audio.wav", audio, "audio/wav"),
            "model": self._settings.model,
        }
        if self._settings.prompt is not None:
            kwargs["prompt"] = self._settings.prompt
        if self._settings.temperature is not None:
            kwargs["temperature"] = self._settings.temperature
        kwargs["response_format"] = "verbose_json"
        # Pass a language hint when the user has explicitly selected one.
        # Avoids Whisper hallucinating English text from non-English speech.
        lang_hint = getattr(self._settings, "language", None)
        if lang_hint and lang_hint not in ("multi", "auto", ""):
            kwargs["language"] = lang_hint
        result = await self._client.audio.transcriptions.create(**kwargs)
        detected_lang = getattr(result, "language", None)
        if detected_lang:
            self.last_detected_language = detected_lang
            logger.info("[stt] detected language: {}", detected_lang)
        return result


def _google_exception_status_code(exc: Exception) -> str | None:
    code_attr = getattr(exc, "code", None)
    if not callable(code_attr):
        return None
    try:
        code = code_attr()
    except Exception:
        return None
    if code is None:
        return None
    name = getattr(code, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(code)


def _is_google_recoverable_internal_error(exc: Exception) -> bool:
    message = str(exc)
    if "Internal server error, code=13" in message:
        return True
    if "500 Internal error from Core" in message:
        return True
    if "Message in Status proto" in message and "Internal error encountered" in message:
        return True
    return _google_exception_status_code(exc) == "INTERNAL"


class _ResilientGoogleSTTService:
    """Mixin that treats transient Google INTERNAL errors as reconnectable.

    Pipecat's Google adapter currently pushes the same transient backend
    exception from both `_process_responses()` and `_stream_audio()`, which
    turns a single upstream hiccup into multiple `ErrorFrame`s and loud
    pipeline warnings. For Google `INTERNAL` errors we prefer to reconnect the
    stream quietly and keep the session alive.

    For non-INTERNAL errors (e.g. 409 "Stream timed out" which Google sends
    when no audio is received for ~30 s), the original in-loop reconnect is
    broken: the old `_request_generator` coroutine is still alive and competes
    with the new one for queue items.  Instead we exit `_stream_audio`
    cleanly and restart the queue + task on the next `run_stt` call so there
    is always exactly one live generator consuming audio.
    """

    _last_recoverable_error_at: float

    def _log_recoverable_google_error(self, exc: Exception) -> None:
        now = time.monotonic()
        last = getattr(self, "_last_recoverable_error_at", 0.0)
        if now - last >= 5.0:
            logger.warning(
                "[stt] Google STT transient INTERNAL error; reconnecting stream: {}",
                exc,
            )
            self._last_recoverable_error_at = now

    async def _process_responses(self, streaming_recognize: AsyncIterable) -> None:
        try:
            await super()._process_responses(streaming_recognize)
        except Exception as exc:
            if _is_google_recoverable_internal_error(exc):
                self._log_recoverable_google_error(exc)
                raise
            raise

    async def _stream_audio(self) -> None:
        try:
            while True:
                try:
                    if self._request_queue.empty():
                        await asyncio.sleep(0.01)
                        continue

                    streaming_recognize = await self._client.streaming_recognize(
                        requests=self._request_generator()
                    )

                    await self._process_responses(streaming_recognize)

                    if (int(time.time() * 1000) - self._stream_start_time) > self.STREAMING_LIMIT:
                        logger.debug("Reconnecting stream after timeout")
                        self._stream_start_time = int(time.time() * 1000)
                    else:
                        break

                except Exception as exc:
                    if _is_google_recoverable_internal_error(exc):
                        # INTERNAL errors: reconnect in-loop (same queue/generator).
                        self._log_recoverable_google_error(exc)
                        await asyncio.sleep(1)
                        self._stream_start_time = int(time.time() * 1000)
                    else:
                        # Non-INTERNAL errors (e.g. 409 stream timeout): push the
                        # error frame and EXIT cleanly.  run_stt() will restart the
                        # task + queue on the next audio chunk so there is no stale
                        # generator competing for queue items.
                        logger.warning(
                            "[stt] Google STT non-recoverable error, "
                            "will restart on next audio: {}",
                            exc,
                        )
                        await self.push_error(
                            error_msg=f"Unknown error occurred: {exc}", exception=exc
                        )
                        return

        except Exception as exc:
            if _is_google_recoverable_internal_error(exc):
                self._log_recoverable_google_error(exc)
                return
            await self.push_error(error_msg=f"Unknown error occurred: {exc}", exception=exc)

    async def run_stt(self, audio: bytes):
        """Override to auto-restart the stream if it exited after a 409 timeout.

        Uses _disconnect() + _connect() so that _stream_start_time, _config and
        all other stream state are fully reset — not just the queue and task.
        """
        if self._streaming_task is not None and self._streaming_task.done():
            logger.info("[stt] Google STT stream was dead — reconnecting with fresh state")
            await self._disconnect()
            await self._connect()
        async for frame in super().run_stt(audio):
            yield frame


def _build_whisper_stt(settings: Settings) -> FrameProcessor | None:
    """Build the Whisper STT service if its credentials are present."""
    if not settings.openai_api_key:
        return None
    stt = _MultilingualWhisperSTT(
        api_key=settings.openai_api_key,
        settings=OpenAISTTService.Settings(
            model=STT_MODEL_WHISPER,
            prompt=settings.stt_prompt if settings.stt_prompt_enabled else None,
            temperature=0.0,
        ),
    )
    logger.info("[stt] whisper available (model={})", STT_MODEL_WHISPER)
    return stt


def _build_deepgram_stt(settings: Settings) -> FrameProcessor | None:
    """Build the Deepgram STT service if its credentials are present."""
    if not settings.deepgram_api_key:
        return None
    from pipecat.services.deepgram.stt import DeepgramSTTService

    stt = DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        ttfs_p99_latency=0.8,
        settings=DeepgramSTTService.Settings(
            model=STT_MODEL_DEEPGRAM,
            language="multi",
            endpointing=300,
            interim_results=True,
        ),
    )
    stt.last_detected_language = None  # type: ignore[attr-defined]
    logger.info("[stt] deepgram available (model={})", STT_MODEL_DEEPGRAM)
    return stt


def _build_google_stt(settings: Settings) -> FrameProcessor | None:
    """Build the Google STT service if its credentials are present and valid."""
    if not settings.google_application_credentials:
        return None
    creds_path = Path(settings.google_application_credentials).expanduser()
    if not creds_path.is_absolute():
        creds_path = Path.cwd() / creds_path
    if not creds_path.exists():
        logger.warning(
            "[stt] google credentials path does not exist: {} — google STT disabled",
            creds_path,
        )
        return None
    try:
        from pipecat.services.google.stt import GoogleSTTService
    except ImportError:
        logger.warning(
            "[stt] google STT extras not installed — install with: uv add 'pipecat-ai[google]'"
        )
        return None

    class ResilientGoogleSTTService(_ResilientGoogleSTTService, GoogleSTTService):
        pass

    stt = ResilientGoogleSTTService(
        credentials_path=str(creds_path),
        settings=GoogleSTTService.Settings(
            model=STT_MODEL_GOOGLE,
            languages=_GOOGLE_AUTO_LANGUAGES,
            enable_interim_results=True,
            enable_voice_activity_events=True,
            enable_automatic_punctuation=False,
        ),
    )
    stt.last_detected_language = None  # type: ignore[attr-defined]
    logger.info("[stt] google available (model={})", STT_MODEL_GOOGLE)
    return stt


_STT_BUILDERS: dict[str, Callable[[Settings], FrameProcessor | None]] = {
    "whisper": _build_whisper_stt,
    "deepgram": _build_deepgram_stt,
    "google": _build_google_stt,
}


def _build_stt(settings: Settings) -> tuple[FrameProcessor, bool]:
    """Factory: build a single STT for the configured provider (local mode).

    Returns (stt_service, needs_vad). Deepgram has built-in VAD so Silero VAD
    is not needed when using it.
    """
    provider = settings.stt_provider
    builder = _STT_BUILDERS.get(provider)
    if builder is None:
        raise RuntimeError(
            f"Unknown STT_PROVIDER={provider!r}. Use one of: whisper, deepgram, google."
        )
    stt = builder(settings)
    if stt is None:
        raise RuntimeError(f"STT_PROVIDER={provider!r} is missing required credentials.")
    needs_vad = provider != "deepgram"
    return stt, needs_vad


# Valid Nova-3 language codes that the browser may request.
_VALID_STT_LANGUAGES: set[str] = {
    "multi",
    "ar",
    "be",
    "bn",
    "bs",
    "bg",
    "ca",
    "zh-HK",
    "zh",
    "zh-CN",
    "zh-Hans",
    "zh-TW",
    "zh-Hant",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "et",
    "fi",
    "nl-BE",
    "fr",
    "de",
    "de-CH",
    "el",
    "gu",
    "he",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "lv",
    "lt",
    "mk",
    "ms",
    "mr",
    "no",
    "fa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sr",
    "sk",
    "sl",
    "es",
    "sv",
    "tl",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "ur",
    "vi",
}

_GOOGLE_AUTO_LANGUAGES: list[Language] = [
    Language.EN_US,
    Language.ES_ES,
    Language.FR_FR,
]

_GOOGLE_LANGUAGE_MAP = {
    "en": Language.EN_US,
    "fr": Language.FR_FR,
    "es": Language.ES_ES,
    "de": Language.DE_DE,
    "it": Language.IT_IT,
    "pt": Language.PT_PT,
    "ro": Language.RO_RO,
    "tr": Language.TR_TR,
    "nl": Language.NL_NL,
    "ja": Language.JA_JP,
    "ko": Language.KO_KR,
    "zh": Language.ZH_CN,
    "ar": Language.AR_SA,
    "ru": Language.RU_RU,
    "hi": Language.HI_IN,
}


def _google_languages_for_selection(lang: str) -> list[Language]:
    if lang == "multi":
        return list(_GOOGLE_AUTO_LANGUAGES)
    return [_GOOGLE_LANGUAGE_MAP.get(lang, Language.EN_US)]
