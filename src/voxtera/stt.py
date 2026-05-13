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
from voxtera.stt_thresholds import STTThresholds

# OpenAI Whisper API model identifier.
STT_MODEL_WHISPER = "whisper-1"
# Deepgram Nova-3 multilingual streaming model.
STT_MODEL_DEEPGRAM = "nova-3-general"
# Gladia Solaria-1: streaming, 99+ languages with native code-switching.
# This is also Pipecat's default for GladiaSTTService, but we set it
# explicitly so a future Pipecat upgrade can't silently change the model.
STT_MODEL_GLADIA = "solaria-1"
# Google Speech-to-Text V2 streaming model name.
#
# `latest_long` is the conservative default: broadly available across all
# Google Cloud regions, accepts the multi-language auto-detect config
# (`languages=_GOOGLE_AUTO_LANGUAGES`), and works with every feature flag
# the builder enables. Downside: ~1000ms final-segment latency because
# the model is tuned for long-form audio rather than conversational use.
#
# Lower-latency alternatives (verify before switching):
#   - "latest_short"   — streaming, conversational, ~300-500ms final.
#                        Broadly available; safer than `chirp_2`.
#   - "chirp_2"        — Google's newest streaming foundation model,
#                        ~150-350ms final, but REGION-RESTRICTED
#                        (us-central1, europe-west4, asia-southeast1 only
#                        as of mid-2025) and has tighter feature/language
#                        constraints. May silently produce no transcripts
#                        if the project's region or config is mismatched.
#                        Confirmed not working with this builder's
#                        config on 2026-05-05.
STT_MODEL_GOOGLE = "latest_long"


class _MultilingualWhisperSTT(OpenAISTTService):
    """Whisper STT with language auto-detection (omits the language param).

    Uses verbose_json to capture the detected language for downstream
    logging and consistency checks, and to read per-segment confidence
    signals (``avg_logprob``, ``no_speech_prob``) for the low-confidence
    drop filter.

    The optional ``thresholds`` argument enables per-language confidence
    filtering. When set, transcriptions whose worst segment falls below
    the configured ``avg_logprob_min`` (or above ``no_speech_prob_max``)
    are dropped before reaching the LLM by clearing ``result.text``. This
    suppresses Whisper substitution hallucinations like
    "the water is not running" → "the White House" without language bias.
    """

    def __init__(
        self,
        *,
        thresholds: STTThresholds | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.last_detected_language: str | None = None
        # If no thresholds object is supplied, build one with the hardcoded
        # fallback so call sites don't need to special-case None.
        self._thresholds: STTThresholds = thresholds or STTThresholds.load(None)

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
        text = (getattr(result, "text", "") or "").strip()

        # Confidence gate. Only run when there's actually text to evaluate;
        # an already-empty result needs no further filtering.
        if text:
            segments = getattr(result, "segments", None) or []
            if segments:
                # Use the WORST segment-level scores so a single noisy span
                # in a longer utterance can still trigger a drop. avg_logprob
                # is most-negative-wins (lower = less confident);
                # no_speech_prob is highest-wins.
                worst_logprob = min(
                    (float(getattr(s, "avg_logprob", 0.0) or 0.0) for s in segments),
                    default=0.0,
                )
                worst_no_speech = max(
                    (float(getattr(s, "no_speech_prob", 0.0) or 0.0) for s in segments),
                    default=0.0,
                )
                t = self._thresholds.for_language(detected_lang)
                if worst_logprob < t.avg_logprob_min or worst_no_speech > t.no_speech_prob_max:
                    logger.warning(
                        "[stt] dropped low-confidence transcription "
                        "(lang={!r}, avg_logprob={:.2f} threshold={:.2f}, "
                        "no_speech_prob={:.2f} threshold={:.2f}): {!r}",
                        detected_lang,
                        worst_logprob,
                        t.avg_logprob_min,
                        worst_no_speech,
                        t.no_speech_prob_max,
                        text,
                    )
                    # Clear the text so the base class doesn't emit a
                    # TranscriptionFrame downstream. Critically, do NOT
                    # update last_detected_language here — the language
                    # detection itself isn't trustworthy when confidence
                    # is low, and a stale value avoids the
                    # AutoTTSLanguageSwitcher flicking to a misdetected
                    # language on noise.
                    try:
                        result.text = ""
                    except Exception:  # pragma: no cover - defensive
                        # If the result object is somehow immutable, we
                        # still want the bot to keep working — fall through
                        # and let downstream filters handle it.
                        logger.debug(
                            "[stt] could not clear result.text; relying on "
                            "downstream noise filter"
                        )
                    return result

        # Accepted (or empty): only update language tracking on a result we
        # trust enough to forward.
        if detected_lang and text:
            self.last_detected_language = detected_lang
            logger.info(
                "[stt] detected language: {} (avg_logprob ok)",
                detected_lang,
            )
        return result

    def reload_thresholds(self) -> None:
        """Reload the confidence-threshold JSON file at runtime.

        Useful during a demo: edit ``config/stt_thresholds.json`` and call
        this to pick up the change without restarting the bot. No-op if
        the STT was constructed without a path-backed STTThresholds.
        """
        self._thresholds.reload()


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
    thresholds = STTThresholds.load(settings.stt_thresholds_path)
    stt = _MultilingualWhisperSTT(
        api_key=settings.openai_api_key,
        settings=OpenAISTTService.Settings(
            model=STT_MODEL_WHISPER,
            prompt=settings.stt_prompt if settings.stt_prompt_enabled else None,
            temperature=0.0,
        ),
        thresholds=thresholds,
    )
    logger.info(
        "[stt] whisper available (model={}, thresholds_languages={})",
        STT_MODEL_WHISPER,
        thresholds.configured_languages() or "default-only",
    )
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


def _build_gladia_stt(settings: Settings) -> FrameProcessor | None:
    """Build the Gladia Solaria-1 STT service if its credentials are present.

    Three operating modes, selected by ``settings.gladia_languages`` and
    ``settings.gladia_code_switching``:

    * **Auto-detect across all languages** (default, empty list): no
      ``language_config`` is passed. Gladia evaluates each utterance against
      all 99 supported languages. Like-for-like with Whisper auto-detect, but
      with true streaming. Note: per Gladia's own docs and observed behavior,
      this mode is more prone to mistranscription than a constrained set —
      the model spends compute on language ID rather than transcription
      quality.
    * **Detect within a constrained set** (``gladia_languages`` non-empty,
      ``gladia_code_switching=False``): Gladia picks ONE language per
      utterance from the supplied list. This is Gladia's documented
      best-accuracy mode and the recommended default for tourism (top
      5-15 languages).
    * **Code-switching within a constrained set** (``gladia_languages``
      non-empty AND ``gladia_code_switching=True``): allows mid-utterance
      language changes like "where is the *gare*?". Requires the list to
      contain ≥2 codes — Gladia silently emits no transcripts if
      code_switching is enabled with a single-language list (a contradictory
      config the API accepts but cannot honor).

    Gladia's server-side VAD (``enable_vad``) is left off; the pipeline's
    Silero VAD handles end-of-utterance detection, matching how the
    Whisper and Google builders are wired.
    """
    if not settings.gladia_api_key:
        return None
    try:
        from pipecat.frames.frames import StartFrame
        from pipecat.services.gladia.config import LanguageConfig
        from pipecat.services.gladia.stt import GladiaSTTService
        from pipecat.services.stt_service import WebsocketSTTService
    except ImportError:
        logger.warning(
            "[stt] gladia STT extras not installed — install with: " "uv add 'pipecat-ai[gladia]'"
        )
        return None

    class _LazyConnectGladiaSTTService(GladiaSTTService):
        """Gladia STT that defers the WebSocket connection until activated.

        Why this exists: Voxtera's daily-mode pipeline builds *all* STT
        providers with valid credentials as parallel branches so the
        browser can switch between them at runtime. Pipecat requires every
        FrameProcessor to receive ``StartFrame`` first, so we can't gate
        StartFrame at the branch level (that breaks the invariant for
        inactive branches and produces "StartFrame not received yet"
        errors on every other frame type).

        Instead, this subclass accepts ``StartFrame`` normally (satisfies
        Pipecat) but skips the implicit ``_connect()`` call. The
        ``STTRouter`` then explicitly calls ``lazy_connect`` /
        ``lazy_disconnect`` when this branch becomes active / inactive,
        so only one Gladia session is open at any time. This dodges the
        Free Trial's 1-concurrent-session cap and is also better resource
        hygiene on paid plans.
        """

        async def start(self, frame: StartFrame) -> None:
            # Skip GladiaSTTService.start (which immediately calls
            # ``self._connect()``); go straight to the websocket-service
            # parent so the StartFrame is still accepted and per-frame
            # initialisation completes.
            await WebsocketSTTService.start(self, frame)

        async def lazy_connect(self) -> None:
            """Open the Gladia WebSocket session. Called by STTRouter when
            this branch transitions inactive→active. Idempotent.
            """
            if self._session_url:
                return
            logger.info("[stt] gladia: lazy_connect — opening session")
            await self._connect()

        async def lazy_disconnect(self) -> None:
            """Close the Gladia WebSocket session and clear session state
            so the next ``lazy_connect`` opens a fresh session. Idempotent.
            """
            if not self._session_url:
                return
            logger.info(
                "[stt] gladia: lazy_disconnect — closing session {}",
                self._session_id,
            )
            await self._disconnect()
            # Clear session URL/id so the next ``lazy_connect`` POSTs a
            # fresh /v2/live init rather than trying to reconnect to a
            # session Gladia has already torn down.
            self._session_url = None
            self._session_id = None

    languages = list(settings.gladia_languages)
    code_switching = settings.gladia_code_switching
    if code_switching and len(languages) < 2:
        # Silently degrade rather than ship a config that produces no
        # transcripts. Gladia accepts but doesn't honor code_switching=True
        # with a single-language list; we'd rather have working detection.
        logger.warning(
            "[stt] gladia: code_switching=True requires >=2 languages "
            "(got {!r}); disabling code_switching",
            languages,
        )
        code_switching = False

    language_config: LanguageConfig | None = None
    if languages:
        language_config = LanguageConfig(
            languages=languages,
            code_switching=code_switching,
        )
        mode_desc = f"detect-within={languages}" + (" + code-switching" if code_switching else "")
    else:
        mode_desc = "auto-detect (all 99 languages)"

    # Use Pipecat 1.0.0's canonical Settings API directly instead of the
    # deprecated ``params=GladiaInputParams(...)`` path. The deprecation path
    # silently produced no transcripts when language_config was supplied;
    # building the Settings object directly avoids that codepath entirely.
    settings_kwargs: dict[str, object] = {"model": STT_MODEL_GLADIA}
    if language_config is not None:
        settings_kwargs["language_config"] = language_config

    stt = _LazyConnectGladiaSTTService(
        api_key=settings.gladia_api_key,
        region=settings.gladia_region,
        settings=GladiaSTTService.Settings(**settings_kwargs),
    )

    # Explicit connect/disconnect logging. Pipecat's GladiaSTTService logs
    # an ERROR-level message itself if the /v2/live init returns a non-2xx
    # status (see GladiaSTTService._setup_gladia), so we don't duplicate
    # that. These two handlers cover the *silent* failure modes — auth
    # revoked mid-session, WebSocket dropped, server-side timeout — that
    # otherwise just stop producing transcripts with no obvious cause.
    @stt.event_handler("on_connected")
    async def _gladia_on_connected(service):  # noqa: ARG001 — required signature
        logger.info("[stt] gladia connected (session_id={})", stt._session_id)

    @stt.event_handler("on_disconnected")
    async def _gladia_on_disconnected(service):  # noqa: ARG001 — required signature
        logger.warning(
            "[stt] gladia disconnected (session_id={}); Pipecat will attempt "
            "to reconnect on the next audio frame",
            stt._session_id,
        )

    # One-shot diagnostic: log the JSON Pipecat will POST to Gladia's
    # /v2/live init endpoint. If transcripts are still missing after this
    # refactor, this line tells us whether the request body is wrong
    # (client side) or whether Gladia is accepting it but not emitting
    # transcripts (server side). Safe to keep — only fires at boot.
    try:
        prepared = stt._prepare_settings()
        logger.info("[stt] gladia init payload preview: {}", prepared)
    except Exception as exc:  # noqa: BLE001 — diagnostic only, never block startup
        logger.debug("[stt] could not preview gladia init payload: {}", exc)
    # Initialise the language-tracking slot. Pipecat's GladiaSTTService
    # exposes per-utterance language in transcript frames; downstream
    # (AutoTTSLanguageSwitcher in routing.py) reads this attribute the
    # same way it does for Deepgram and Google.
    stt.last_detected_language = None  # type: ignore[attr-defined]
    logger.info(
        "[stt] gladia available (model={}, region={}, mode={})",
        STT_MODEL_GLADIA,
        settings.gladia_region,
        mode_desc,
    )
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


def _build_google_chirp2_stt(settings: Settings) -> FrameProcessor | None:
    """Build a Google STT service using the chirp_2 model in us-central1.

    chirp_2 is Google's lowest-latency streaming model (~150-350ms final)
    but requires a regional endpoint (not available in ``global``).
    """
    if not settings.google_application_credentials:
        return None
    creds_path = Path(settings.google_application_credentials).expanduser()
    if not creds_path.is_absolute():
        creds_path = Path.cwd() / creds_path
    if not creds_path.exists():
        logger.warning(
            "[stt] google credentials path does not exist: {} — google-chirp2 STT disabled",
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
        location="us-central1",
        settings=GoogleSTTService.Settings(
            model="chirp_2",
            # chirp_2 in us-central1 does NOT support multi-language auto-detect;
            # pass a single language only.
            languages=["en-US"],
            enable_interim_results=True,
            enable_voice_activity_events=True,
            enable_automatic_punctuation=False,
        ),
    )
    stt.last_detected_language = None  # type: ignore[attr-defined]
    logger.info("[stt] google-chirp2 available (model=chirp_2, location=us-central1)")
    return stt


_STT_BUILDERS: dict[str, Callable[[Settings], FrameProcessor | None]] = {
    "whisper": _build_whisper_stt,
    "deepgram": _build_deepgram_stt,
    "gladia": _build_gladia_stt,
    "google": _build_google_stt,
    "google-chirp2": _build_google_chirp2_stt,
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
            f"Unknown STT_PROVIDER={provider!r}. "
            "Use one of: whisper, deepgram, gladia, google, google-chirp2."
        )
    stt = builder(settings)
    if stt is None:
        raise RuntimeError(f"STT_PROVIDER={provider!r} is missing required credentials.")
    # Deepgram is the only provider with built-in turn detection that fully
    # replaces Silero VAD. Gladia offers server-side VAD too (enable_vad=True)
    # but we deliberately don't use it — Voxtera's Silero VAD is tuned per-mic
    # via VAD_MIN_VOLUME/VAD_CONFIDENCE, so Gladia goes through the same path
    # as Whisper and Google.
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
