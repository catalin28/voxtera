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
import json
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
# ElevenLabs Scribe v2 realtime: WebSocket streaming, 100+ languages.
STT_MODEL_ELEVENLABS = "scribe_v2_realtime"
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


def _is_deepgram_fatal_error(exc: Exception) -> bool:
    """Return True for errors that won't resolve by retrying (auth, bad config)."""
    msg = str(exc).lower()
    return any(k in msg for k in ("invalid api key", "401", "403", "forbidden", "unauthorized"))


def _build_deepgram_stt(settings: Settings) -> FrameProcessor | None:
    """Build the Deepgram STT service if its credentials are present."""
    if not settings.deepgram_api_key:
        return None
    try:
        from pipecat.services.deepgram.stt import DeepgramSTTService
    except Exception as exc:  # noqa: BLE001
        logger.warning("[stt] deepgram: skipping — import failed: {}", exc)
        return None

    class _InstrumentedDeepgramSTTService(DeepgramSTTService):
        """DeepgramSTTService with labelled, severity-aware error logging.

        Pipecat's built-in handler logs every connection failure as a generic
        warning with no ``[stt]`` prefix and retries forever — including on
        fatal errors like invalid API keys.  This subclass:

        * Prefixes all Deepgram log messages with ``[stt] deepgram:`` so they
          are easy to grep in bot logs.
        * Promotes auth / config errors to ``ERROR`` level so they show up
          clearly instead of being buried in ``WARNING`` noise.
        * Stops retrying on fatal errors rather than spinning forever.
        """

        async def _on_error(self, error) -> None:  # type: ignore[override]
            msg = str(error)
            if _is_deepgram_fatal_error(Exception(msg)):
                logger.error(
                    "[stt] deepgram: FATAL connection error (check API key / plan): {}", msg
                )
            else:
                logger.warning("[stt] deepgram: connection error, will retry: {}", msg)
            await super()._on_error(error)

        async def _connection_handler(self) -> None:  # type: ignore[override]
            import asyncio

            while True:
                connect_kwargs = self._build_connect_kwargs()
                try:
                    async with self._client.listen.v1.connect(**connect_kwargs) as conn:
                        self._connection = conn
                        from deepgram.core.events import EventType

                        conn.on(EventType.MESSAGE, self._on_message)
                        conn.on(EventType.ERROR, self._on_error)
                        logger.debug("[stt] deepgram: WebSocket connection initialised")

                        keepalive_task = self.create_task(
                            self._keepalive_handler(), f"{self}::keepalive"
                        )
                        try:
                            await conn.start_listening()
                        finally:
                            await self.cancel_task(keepalive_task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if _is_deepgram_fatal_error(exc):
                        logger.error(
                            "[stt] deepgram: FATAL — stopping retries. "
                            "Check DEEPGRAM_API_KEY and account status. Error: {}",
                            exc,
                        )
                        return  # Do NOT retry on fatal errors.
                    logger.warning("[stt] deepgram: connection lost, will retry: {}", exc)
                finally:
                    self._connection = None  # type: ignore[assignment]

    stt = _InstrumentedDeepgramSTTService(
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


# Custom-vocabulary file for Gladia STT. Biases recognition toward
# hotel-concierge domain terms so domain words ("breakfast", "amenities",
# venue names) stop being mis-transcribed ("breakfast" -> "blacksmith").
# Lives at <repo>/config/stt_vocabulary.json — see that file's _README.
_STT_VOCABULARY_PATH = Path(__file__).resolve().parents[2] / "config" / "stt_vocabulary.json"


def _load_stt_vocabulary(hotel_id: str) -> list[str]:
    """Load and flatten the Gladia custom-vocabulary term list.

    Reads ``config/stt_vocabulary.json``: flattens every array under
    ``categories`` and appends the proper nouns registered for ``hotel_id``
    under ``hotel_proper_nouns``. Returns a de-duplicated, order-preserving
    list of terms — or ``[]`` (logged) if the file is missing or malformed,
    so STT runs un-biased rather than failing to start.
    """
    try:
        data = json.loads(_STT_VOCABULARY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("[stt] no custom-vocabulary file at {}", _STT_VOCABULARY_PATH)
        return []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[stt] custom-vocabulary file unreadable ({}): {}", _STT_VOCABULARY_PATH, exc
        )
        return []

    terms: list[str] = []
    categories = data.get("categories")
    if isinstance(categories, dict):
        for entries in categories.values():
            if isinstance(entries, list):
                terms.extend(str(t) for t in entries)
    proper_nouns = data.get("hotel_proper_nouns")
    if isinstance(proper_nouns, dict):
        hotel_nouns = proper_nouns.get(hotel_id)
        if isinstance(hotel_nouns, list):
            terms.extend(str(t) for t in hotel_nouns)

    # De-duplicate while preserving first-seen order.
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            unique.append(term)
    logger.info("[stt] custom vocabulary: {} terms loaded (hotel_id={!r})", len(unique), hotel_id)
    return unique


def preimport_gladia() -> None:
    """Import heavy Gladia/aiohttp modules so they're cached in sys.modules.

    Call from a background thread early in bot startup so the cost (~500ms)
    overlaps with other init work. By the time _build_gladia_stt runs, the
    imports are instant cache hits.
    """
    import time as _t

    try:
        t0 = _t.perf_counter()
        import aiohttp  # noqa: F401

        logger.info("[stt] preimport aiohttp: {:.0f}ms", (_t.perf_counter() - t0) * 1000)

        t0 = _t.perf_counter()
        import pipecat.services.gladia.config  # noqa: F401

        logger.info("[stt] preimport gladia.config: {:.0f}ms", (_t.perf_counter() - t0) * 1000)

        t0 = _t.perf_counter()
        import pipecat.services.gladia.stt  # noqa: F401

        logger.info("[stt] preimport gladia.stt: {:.0f}ms", (_t.perf_counter() - t0) * 1000)

        t0 = _t.perf_counter()
        import pipecat.services.stt_service  # noqa: F401

        logger.info("[stt] preimport stt_service: {:.0f}ms", (_t.perf_counter() - t0) * 1000)
    except ImportError as exc:
        logger.warning("[stt] preimport_gladia failed: {}", exc)


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
    _imp_t0 = time.perf_counter()
    try:
        from pipecat.frames.frames import (
            Frame,
            StartFrame,
            TranscriptionFrame,
            VADUserStartedSpeakingFrame,
            VADUserStoppedSpeakingFrame,
        )
        from pipecat.processors.frame_processor import FrameDirection
        from pipecat.services.gladia.config import (
            CustomVocabularyConfig,  # noqa: F401 — needed when vocab re-enabled
            LanguageConfig,
            RealtimeProcessingConfig,  # noqa: F401 — needed when vocab re-enabled
        )
        from pipecat.services.gladia.stt import GladiaSTTService
        from pipecat.services.stt_service import WebsocketSTTService
    except ImportError:
        logger.warning(
            "[stt] gladia STT extras not installed — install with: " "uv add 'pipecat-ai[gladia]'"
        )
        return None
    logger.info("[stt] gladia imports took {:.0f}ms", (time.perf_counter() - _imp_t0) * 1000)

    # ── Gladia session management helpers ─────────────────────────────────
    # Pipecat's GladiaSTTService never calls DELETE /v2/live/{id} — it only
    # closes the WebSocket. Gladia keeps sessions in "processing" state
    # indefinitely after a plain ws.close(), exhausting the free-tier
    # 1-concurrent-session cap and silently blocking all future transcription
    # (VAD fires but Gladia returns zero transcripts; no 429 error is surfaced
    # to the bot). These helpers fix that at the voxtera level.

    async def _gladia_delete_session(api_key: str, region: str | None, session_id: str) -> None:
        """DELETE /v2/live/{session_id} to release the session slot immediately."""
        import aiohttp

        base = "https://api.gladia.io"
        params = {"region": region} if region else {}
        url = f"{base}/v2/live/{session_id}"
        try:
            async with (
                aiohttp.ClientSession() as http,
                http.delete(
                    url,
                    headers={"x-gladia-key": api_key},
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp,
            ):
                if resp.status in (202, 204):
                    logger.info(
                        "[stt] gladia: deleted session {} (HTTP {})", session_id, resp.status
                    )
                else:
                    body = await resp.text()
                    logger.warning(
                        "[stt] gladia: DELETE session {} returned {} — {}",
                        session_id,
                        resp.status,
                        body[:200],
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[stt] gladia: DELETE session {} failed (non-fatal): {}", session_id, exc
            )

    async def _gladia_purge_orphan_sessions(api_key: str, region: str | None) -> int:
        """Delete all active Gladia sessions for this API key before opening a new one.

        Called once per lazy_connect to ensure the free-tier 1-session cap is
        never exhausted by sessions that were left open by a previous bot run
        (crash, SIGTERM, hard kill) that skipped lazy_disconnect.

        Returns the number of sessions deleted.
        """
        import aiohttp

        base = "https://api.gladia.io"
        params: dict[str, str] = {"limit": "20"}
        if region:
            params["region"] = region

        deleted = 0
        offset = 0
        try:
            async with aiohttp.ClientSession() as http:
                while True:
                    params["offset"] = str(offset)
                    async with http.get(
                        f"{base}/v2/live",
                        headers={"x-gladia-key": api_key},
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if not resp.ok:
                            logger.warning(
                                "[stt] gladia: listing sessions failed (HTTP {})", resp.status
                            )
                            break
                        data = await resp.json()
                    items = data.get("items", data.get("data", []))
                    if not items:
                        break
                    for item in items:
                        sid = item.get("id") or item.get("session_id")
                        if not sid:
                            continue
                        async with http.delete(
                            f"{base}/v2/live/{sid}",
                            headers={"x-gladia-key": api_key},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as dr:
                            if dr.status in (202, 204):
                                deleted += 1
                            elif dr.status == 403:
                                # Session is locked by Gladia (e.g. just created by
                                # another live process). Leave it — starting a new
                                # session will still get a 429 but that's the correct
                                # error to surface rather than silently stalling.
                                logger.warning(
                                    "[stt] gladia: session {} is locked (403) — cannot purge",
                                    sid,
                                )
                    if len(items) < 20:
                        break
                    offset += 20
        except Exception as exc:  # noqa: BLE001
            logger.warning("[stt] gladia: orphan-purge failed (non-fatal): {}", exc)

        if deleted:
            logger.info("[stt] gladia: purged {} orphan session(s) before connecting", deleted)
        return deleted

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

        _pending_connect: bool = False

        # ── Gladia latency instrumentation ──────────────────────────────────
        # Tracks wall-clock time between VAD-start (first audio chunk of the
        # current utterance reaches Gladia's WebSocket) and Gladia emitting a
        # final TranscriptionFrame downstream. Two deltas are logged:
        #
        #   first_audio→final   — total Gladia wall-clock (includes the time
        #                         the user was still talking)
        #   user_stopped→final  — Gladia's processing tail after the user
        #                         stopped speaking; the more meaningful number
        #                         for "how long did Gladia take to answer"
        #
        # Both anchors use perf_counter (monotonic, ns precision). Reset on
        # each new VAD-start so every utterance is measured independently.
        _t_first_audio: float | None = None
        _t_user_stopped: float | None = None

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            # Mark the start of an utterance the moment Silero VAD fires.
            # Pipecat's STTGate only opens the audio path between VAD-start
            # and VAD-stop, so this is the wall-clock instant Gladia first
            # sees voiced audio for this turn.
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self._t_first_audio = time.perf_counter()
                self._t_user_stopped = None
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                # Only stamp the FIRST stop frame of the turn. VAD events can
                # echo through multiple pipeline branches; we want the earliest
                # one we see (== when Gladia last received audio).
                if self._t_user_stopped is None:
                    self._t_user_stopped = time.perf_counter()
            await super().process_frame(frame, direction)

        async def push_frame(
            self,
            frame: Frame,
            direction: FrameDirection = FrameDirection.DOWNSTREAM,
        ) -> None:
            # Log on the final TranscriptionFrame Gladia emits downstream.
            # InterimTranscriptionFrames are ignored so the log line marks the
            # moment Gladia is "done answering".
            if (
                isinstance(frame, TranscriptionFrame)
                and direction == FrameDirection.DOWNSTREAM
                and getattr(frame, "text", "")
            ):
                now = time.perf_counter()
                audio_to_final_ms = (
                    int((now - self._t_first_audio) * 1000)
                    if self._t_first_audio is not None
                    else None
                )
                stopped_to_final_ms = (
                    int((now - self._t_user_stopped) * 1000)
                    if self._t_user_stopped is not None
                    else None
                )
                logger.info(
                    "[stt] gladia latency: first_audio→final={}ms "
                    "user_stopped→final={}ms text={!r}",
                    audio_to_final_ms,
                    stopped_to_final_ms,
                    frame.text[:80],
                )
                # Mirror into the trace bus so the dashboard / exported trace
                # JSON carries the same numbers. Best-effort: failure to emit
                # must never block the transcript from flowing downstream.
                try:
                    from voxtera.trace import emit as _trace_emit
                    from voxtera.trace import tracker as _trace_tracker

                    _trace_emit(
                        "lifecycle",
                        source="stt.gladia",
                        turn_id=_trace_tracker().current(),
                        data={
                            "event": "gladia_transcript_latency",
                            "first_audio_to_final_ms": audio_to_final_ms,
                            "user_stopped_to_final_ms": stopped_to_final_ms,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — diagnostic only
                    logger.debug("[stt] gladia latency trace-emit failed: {}", exc)
                # Reset so a stray late transcript on the same utterance
                # doesn't double-count against stale anchors.
                self._t_first_audio = None
                self._t_user_stopped = None
            await super().push_frame(frame, direction)

        async def start(self, frame: StartFrame) -> None:
            # Skip GladiaSTTService.start (which immediately calls
            # ``self._connect()``); go straight to the websocket-service
            # parent so the StartFrame is still accepted and per-frame
            # initialisation completes.
            await WebsocketSTTService.start(self, frame)
            # If lazy_connect was requested before start() (e.g. STTRouter
            # activated this branch during __init__), sample_rate is now set
            # so we can safely connect.
            if self._pending_connect:
                self._pending_connect = False
                await self._connect()

        async def lazy_connect(self) -> None:
            """Open the Gladia WebSocket session. Called by STTRouter when
            this branch transitions inactive→active. Idempotent.
            """
            if self._session_url:
                return
            # If start() hasn't been called yet, sample_rate is 0 and Gladia
            # will reject the session init. Defer until start() runs.
            if self.sample_rate == 0:
                logger.info("[stt] gladia: lazy_connect deferred (awaiting StartFrame)")
                self._pending_connect = True
                return
            # Purge any sessions left open by a previous bot run that was
            # hard-killed (SIGTERM/crash) without running lazy_disconnect.
            # Pipecat never calls DELETE, so these accumulate and eventually
            # hit the concurrent-session cap, causing silent 429s.
            await _gladia_purge_orphan_sessions(self._api_key, self._region)
            logger.info("[stt] gladia: lazy_connect — opening session")
            try:
                await self._connect()
            except Exception as exc:
                msg = str(exc)
                if "429" in msg or "concurrent" in msg.lower() or "maximum" in msg.lower():
                    logger.error(
                        "[stt] gladia: CONCURRENT SESSION CAP HIT — Gladia rejected new session "
                        "(429 Too Many Requests). Check https://app.gladia.io for stuck sessions. "
                        "Error: {}",
                        exc,
                    )
                else:
                    logger.error("[stt] gladia: lazy_connect failed — {}", exc)
                raise

        async def lazy_disconnect(self) -> None:
            """Close the Gladia WebSocket session and clear session state
            so the next ``lazy_connect`` opens a fresh session. Idempotent.

            Pipecat's GladiaSTTService._disconnect() only closes the WebSocket
            — it never calls DELETE /v2/live/{session_id}. Gladia keeps the
            session in "processing" state indefinitely after a plain ws.close(),
            which exhausts the free-tier 1-session cap and silently blocks all
            future transcription. We call DELETE here to properly terminate it.
            """
            if not self._session_url:
                return
            sid = self._session_id
            logger.info("[stt] gladia: lazy_disconnect — closing session {}", sid)
            await self._disconnect()
            # Clear session URL/id so the next lazy_connect POSTs a fresh
            # /v2/live init rather than trying to reconnect to a torn-down session.
            self._session_url = None
            self._session_id = None
            # Hard-delete the session via Gladia's REST API so it releases the
            # concurrent-session slot immediately. Without this, Gladia keeps
            # the session in "processing" state indefinitely, which exhausts
            # the 1-concurrent-session cap on the Free Trial plan and silently
            # causes 429 errors on the next bot start — VAD fires but Gladia
            # returns zero transcripts.
            if sid:
                await _gladia_delete_session(self._api_key, self._region, sid)

        async def reconfigure_languages(
            self,
            languages: list[str],
            *,
            code_switching: bool = False,
        ) -> None:
            """Update Gladia's ``language_config`` and rebuild the session.

            Pipecat's :class:`GladiaSTTService._update_settings` stores a
            settings delta but does NOT reapply it to the active
            WebSocket — see the upstream TODO in
            ``pipecat/services/gladia/stt.py``. Without an explicit
            reconnect, the live session keeps transcribing against
            whatever ``language_config`` was used at session-open time, so
            a runtime ``voxtera-language`` change to ``"ro"`` would have
            no effect and Gladia would keep producing garbled English
            transcripts of Romanian audio.

            This helper closes the current Gladia session (if any),
            mutates ``self._settings.language_config`` in place, and
            reopens — the next ``/v2/live`` POST picks up the new
            languages. Honors the same ``code_switching`` guard as the
            boot-time builder (silently downgrades to ``False`` when fewer
            than 2 languages are given).
            """
            was_connected = bool(self._session_url)
            if was_connected:
                await self.lazy_disconnect()

            if languages:
                if code_switching and len(languages) < 2:
                    logger.warning(
                        "[stt] gladia.reconfigure_languages: code_switching=True "
                        "needs >=2 languages (got {!r}); disabling",
                        languages,
                    )
                    code_switching = False
                self._settings.language_config = LanguageConfig(
                    languages=languages,
                    code_switching=code_switching,
                )
            else:
                self._settings.language_config = None

            logger.info(
                "[stt] gladia.reconfigure_languages: languages={!r} code_switching={}",
                languages,
                code_switching,
            )

            if was_connected:
                await self.lazy_connect()

    languages = list(settings.gladia_languages)
    code_switching = settings.gladia_code_switching

    # If no explicit language list is configured, send an empty list to
    # Gladia so it uses full open auto-detect (all 99 languages with
    # code_switching=true). This matches Gladia's own website behavior
    # and produces the best transcription quality for languages like
    # Romanian that degrade when constrained to a large language set.
    _t_lang = time.perf_counter()
    if not languages:
        code_switching = True
    logger.info("[stt] gladia lang_config took {:.0f}ms", (time.perf_counter() - _t_lang) * 1000)

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
        # Empty list = open auto-detect across all 99 languages with
        # code_switching enabled (matches Gladia website behavior).
        language_config = LanguageConfig(
            languages=[],
            code_switching=True,
        )
        mode_desc = "auto-detect (all 99 languages, code_switching=true)"

    # Use Pipecat 1.0.0's canonical Settings API directly instead of the
    # deprecated ``params=GladiaInputParams(...)`` path. The deprecation path
    # silently produced no transcripts when language_config was supplied;
    # building the Settings object directly avoids that codepath entirely.
    settings_kwargs: dict[str, object] = {"model": STT_MODEL_GLADIA}
    if language_config is not None:
        settings_kwargs["language_config"] = language_config

    logger.info(
        "[stt] gladia FULL settings_kwargs sent to /v2/live: {}",
        settings_kwargs,
    )

    # Custom vocabulary — bias Gladia toward hotel-concierge domain terms so
    # domain words ("breakfast", "amenities", the hotel's venue names) stop
    # being mis-transcribed. Travels once in the session-init payload, not
    # per utterance. Skipped silently when the vocabulary file is absent.
    # TEMPORARILY DISABLED to test if vocabulary is causing "the menu" → "kids menu"
    # _t_vocab = time.perf_counter()
    # vocabulary = _load_stt_vocabulary(settings.hotel_id)
    # if vocabulary:
    #     settings_kwargs["realtime_processing"] = RealtimeProcessingConfig(
    #         custom_vocabulary=True,
    #         custom_vocabulary_config=CustomVocabularyConfig(vocabulary=vocabulary),
    #     )
    # logger.info("[stt] gladia vocab took {:.0f}ms", (time.perf_counter() - _t_vocab) * 1000)
    logger.info("[stt] custom vocabulary DISABLED for testing")

    _t_ctor = time.perf_counter()
    stt = _LazyConnectGladiaSTTService(
        api_key=settings.gladia_api_key,
        region=settings.gladia_region,
        sample_rate=16000,
        settings=GladiaSTTService.Settings(**settings_kwargs),
    )
    logger.info("[stt] gladia constructor took {:.0f}ms", (time.perf_counter() - _t_ctor) * 1000)

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


def _build_elevenlabs_stt(settings: Settings) -> FrameProcessor | None:
    """Build ElevenLabs Realtime STT (Scribe v2) if API key is present."""
    if not settings.elevenlabs_api_key:
        return None
    _imp_t0 = time.perf_counter()
    try:
        from pipecat.services.elevenlabs.stt import (
            CommitStrategy,
            ElevenLabsRealtimeSTTService,
        )
    except ImportError:
        logger.warning(
            "[stt] elevenlabs STT extras not installed — "
            "install with: uv add 'pipecat-ai[elevenlabs]'"
        )
        return None
    logger.debug("[stt] elevenlabs import took {:.0f}ms", (time.perf_counter() - _imp_t0) * 1000)

    _t_ctor = time.perf_counter()
    stt = ElevenLabsRealtimeSTTService(
        api_key=settings.elevenlabs_api_key,
        sample_rate=16000,
        # MANUAL commit = Pipecat's Silero VAD controls turn boundaries
        # (consistent with how we use Gladia/Whisper/Google).
        commit_strategy=CommitStrategy.MANUAL,
        include_language_detection=True,
    )
    logger.info(
        "[stt] elevenlabs constructor took {:.0f}ms", (time.perf_counter() - _t_ctor) * 1000
    )

    stt.last_detected_language = None  # type: ignore[attr-defined]
    logger.info("[stt] elevenlabs available (model={})", STT_MODEL_ELEVENLABS)
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
    "elevenlabs": _build_elevenlabs_stt,
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
            "Use one of: whisper, deepgram, gladia, elevenlabs, google, google-chirp2."
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
    Language.RO_RO,
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
