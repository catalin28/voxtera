"""Pipeline assembly.

:func:`build_pipeline` is the only public symbol — it wires every processor
described in the sibling modules into a Pipecat :class:`Pipeline` and returns
a runnable :class:`PipelineTask` plus its :class:`PipelineRunner`.

Two transports are supported:

- ``transport_mode='local'`` — single STT, single TTS, no parallel branches.
  Used for the CLI demo and tests.
- ``transport_mode='daily'`` — every STT/TTS provider with valid creds is
  built and runs as a parallel branch behind the routers in
  :mod:`voxtera.routing`. The browser flips the active branch via
  ``voxtera-stt`` / ``voxtera-tts-provider`` app-messages.

A small ``_eject_stale_bots`` helper is included to clean up any bot
participants left over in the Daily room from a previous crash.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from voxtera.actions import ActionRuntime

import time as _time

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.openai.llm import OpenAILLMService
from voxtera.turns import TailAwareTurnStopStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)


class PipelineProbe(FrameProcessor):
    """Diagnostic probe that logs significant frames passing through a point."""

    # Only log these frame types to avoid flooding with every audio frame.
    _INTERESTING = (
        TranscriptionFrame,
        InterimTranscriptionFrame,
        LLMFullResponseStartFrame,
        LLMFullResponseEndFrame,
        LLMTextFrame,
        TTSStartedFrame,
        TTSStoppedFrame,
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )

    # Probes at these positions log RMS stats to diagnose audio level issues.
    _RMS_PROBES = {"after_leakage_guard", "after_vad"}

    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label
        self._audio_in_count = 0
        self._audio_out_count = 0
        self._last_audio_log = 0.0
        self._other_frame_count = 0
        self._rms_peak = 0.0
        self._rms_sum = 0.0
        self._rms_frames = 0

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            self._audio_in_count += 1
            # Track RMS at designated probes
            if self._label in self._RMS_PROBES and frame.audio:
                import numpy as _np

                samples = _np.frombuffer(frame.audio, dtype=_np.int16)
                if samples.size:
                    rms = float(_np.sqrt(_np.mean(samples.astype(_np.float32) ** 2))) / 32768.0
                    self._rms_peak = max(self._rms_peak, rms)
                    self._rms_sum += rms
                    self._rms_frames += 1
            now = _time.monotonic()
            # Log audio flow every 5 seconds to confirm it's passing
            if now - self._last_audio_log >= 5.0:
                if self._label in self._RMS_PROBES and self._rms_frames > 0:
                    avg_rms = self._rms_sum / self._rms_frames
                    logger.info(
                        "[probe:{}] audio_in: {} frames/5s | RMS avg={:.4f} peak={:.4f}",
                        self._label,
                        self._audio_in_count,
                        avg_rms,
                        self._rms_peak,
                    )
                    self._rms_peak = 0.0
                    self._rms_sum = 0.0
                    self._rms_frames = 0
                else:
                    logger.info(
                        "[probe:{}] audio_in flowing: {} frames in last 5s",
                        self._label,
                        self._audio_in_count,
                    )
                self._audio_in_count = 0
                self._last_audio_log = now
        elif isinstance(frame, OutputAudioRawFrame):
            self._audio_out_count += 1
            now = _time.monotonic()
            if now - self._last_audio_log >= 5.0:
                logger.info(
                    "[probe:{}] audio_out flowing: {} frames in last 5s",
                    self._label,
                    self._audio_out_count,
                )
                self._audio_out_count = 0
                self._last_audio_log = now
        elif isinstance(frame, self._INTERESTING):
            logger.info(
                "[probe:{}] {} (dir={})",
                self._label,
                frame.__class__.__name__,
                direction.name,
            )
            # Push to the trace bus so the dashboard sees the same flow we
            # log. Cheap (just a deque append + queue.put_nowait per
            # subscriber) and lossy by design — the bus drops oldest events
            # on a full subscriber rather than blocking the pipeline.
            _trace_emit(
                "frame",
                source=self._label,
                turn_id=_trace_tracker().current(),
                data={
                    "frame_type": frame.__class__.__name__,
                    "direction": direction.name.lower(),
                },
            )
        else:
            # Log all other non-audio frames to catch DailyInputTransportMessageFrame etc.
            self._other_frame_count += 1
            fname = frame.__class__.__name__
            if (
                "Message" in fname
                or "Start" in fname
                or "End" in fname
                or "Run" in fname
                or "Transcription" in fname
            ):
                logger.info(
                    "[probe:{}] OTHER: {} (dir={})",
                    self._label,
                    fname,
                    direction.name,
                )

        await self.push_frame(frame, direction)


class PstnIdleWatcher(FrameProcessor):
    """Detects prolonged caller silence on PSTN calls and triggers hangup.

    Inserted after VAD so it sees VADUserStartedSpeakingFrame.  A background
    asyncio task checks whether speech has occurred within `idle_timeout` secs.
    On first timeout it queues a TTS prompt ("Are you still there?").  If a
    second, shorter timeout elapses with no speech, it queues an EndFrame.
    """

    def __init__(
        self,
        idle_timeout: float = 45.0,
        followup_timeout: float = 15.0,
    ) -> None:
        super().__init__()
        self._idle_timeout = idle_timeout
        self._followup_timeout = followup_timeout
        self._last_speech_ts: float = 0.0  # monotonic; 0 = never spoken
        self._watchdog_task: asyncio.Task | None = None
        self._task_ref: object | None = None  # set to PipelineTask externally
        self._stopped = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._last_speech_ts = _time.monotonic()
        await self.push_frame(frame, direction)

    def start_watchdog(self, pipeline_task) -> None:
        """Call once the pipeline task exists and dial-in is connected."""
        self._task_ref = pipeline_task
        self._last_speech_ts = _time.monotonic()  # grace period from connect
        self._watchdog_task = asyncio.create_task(self._watch_loop())

    async def _watch_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(2.0)
                elapsed = _time.monotonic() - self._last_speech_ts
                if elapsed >= self._idle_timeout:
                    logger.info(
                        "[pstn-idle] no speech for {:.0f}s — prompting caller",
                        elapsed,
                    )
                    await self._task_ref.queue_frame(TTSSpeakFrame(text="Are you still there?"))
                    # Wait for followup speech
                    await self._wait_for_speech_or_timeout(self._followup_timeout)
                    if _time.monotonic() - self._last_speech_ts >= self._followup_timeout:
                        logger.info("[pstn-idle] no response after prompt — ending call")
                        await self._task_ref.queue_frame(TTSSpeakFrame(text="Goodbye."))
                        await asyncio.sleep(1.5)
                        await self._task_ref.queue_frame(EndFrame())
                        return
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.opt(exception=True).warning("[pstn-idle] watchdog error")

    async def _wait_for_speech_or_timeout(self, timeout: float) -> None:
        """Sleep in short increments, returning early if speech is detected."""
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if (
                self._last_speech_ts > deadline - timeout
                and _time.monotonic() - self._last_speech_ts < 2.0
            ):
                # Speech happened after we started waiting and is recent
                return

    def stop(self) -> None:
        self._stopped = True
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()


try:
    from pipecat.transports.daily.transport import (
        DailyDialinSettings,
        DailyParams,
        DailyTransport,
    )
except Exception:  # daily-python not available on Windows
    DailyParams = None  # type: ignore[assignment,misc]
    DailyTransport = None  # type: ignore[assignment,misc]
    DailyDialinSettings = None  # type: ignore[assignment,misc]
from pipecat.transports.local.audio import (  # noqa: E402
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from voxtera.audio import (  # noqa: E402
    AudioLevelMonitor,
    BotActiveUserFrameSuppressor,
    HighShelfPreEmphasis,
    PlaybackLeakageGuard,
    RNNoiseDenoiser,
    TranscriptionNoiseFilter,
)
from voxtera.health import ActivityNotifier, TransportHealthMonitor  # noqa: E402

try:
    from pyrnnoise import RNNoise as _RNNoise
except Exception:  # pragma: no cover - optional dependency at runtime
    _RNNoise = None
from voxtera.config import Settings  # noqa: E402
from voxtera.controllers import (  # noqa: E402
    LLM_MODEL,
    AutoTTSLanguageSwitcher,
    BrowserTextInputController,
    GreetingController,
    InstantAckFiller,
    InterruptionResumer,
    LanguageSwitcher,
    LLMRunGuard,
    ModelSwitcher,
)
from voxtera.observability import (  # noqa: E402
    DemoEventBroadcaster,
    PipelineTracer,
    TranscriptStageTimer,
    TTSStageTimer,
    UserTranscriptBroadcaster,
)
from voxtera.prompts import SYSTEM_PROMPT, resolve_greeting  # noqa: E402
from voxtera.prompts.greetings import apply_hotel_name  # noqa: E402
from voxtera.routing import STTGate, STTRouter, TTSGate, TTSRouter  # noqa: E402
from voxtera.stt import _STT_BUILDERS, _build_stt  # noqa: E402
from voxtera.trace import emit as _trace_emit  # noqa: E402
from voxtera.trace import tracker as _trace_tracker  # noqa: E402
from voxtera.tts import _TTS_BUILDERS  # noqa: E402
from voxtera.tunables import register_pipeline_knobs  # noqa: E402


async def _warmup_tts_providers(settings: Settings) -> None:
    """Fire a silent request to the active TTS provider right after the bot
    joins the room to eliminate the cold-start penalty on the first real reply.

    Currently warms OpenAI tts-1 (~1-3 s saved on first turn).
    Google Chirp 3 HD already has low first-call latency (~400 ms) so no
    warmup request is sent for that provider.
    Runs as a fire-and-forget background task — failure is non-fatal.
    """
    if settings.tts_provider != "openai":
        logger.debug("[tts-warmup] provider={!r} — no warmup needed", settings.tts_provider)
        return

    if not settings.openai_api_key:
        logger.debug("[tts-warmup] no OpenAI API key — skipping")
        return

    voice = settings.default_tts_voice

    def _call() -> None:
        body = json.dumps({"model": "tts-1", "voice": voice, "input": "."}).encode()
        req = Request(
            "https://api.openai.com/v1/audio/speech",
            data=body,
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            resp.read()  # consume + discard the audio bytes

    try:
        await asyncio.to_thread(_call)
        logger.info("[tts-warmup] OpenAI tts-1/{} pre-warmed", voice)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tts-warmup] warmup skipped (non-fatal): {}", exc)


async def _warmup_anthropic_llm(settings: Settings, model: str) -> None:
    """Fire a minimal request to Anthropic at session start to:

    1. Establish the TLS connection + warm the httpx connection pool.
    2. Trigger Anthropic's per-model cold-start (first request after idle is
       ~100-200 ms slower than steady-state).

    Note: this does NOT pre-populate the prompt cache for our system prompt,
    because the cache key includes the full prefix and the RAG/user messages
    differ on real turns. The benefit is purely TLS/connection-pool warmup
    plus model wake — measurable savings on the first real turn (~150-250 ms),
    negligible after that.
    """
    if not settings.anthropic_api_key:
        logger.debug("[llm-warmup] no Anthropic API key — skipping")
        return

    def _call() -> None:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            }
        ).encode()
        req = Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        with urlopen(req, timeout=10) as resp:  # noqa: S310
            resp.read()

    try:
        await asyncio.to_thread(_call)
        logger.info("[llm-warmup] Anthropic {} pre-warmed", model)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[llm-warmup] warmup skipped (non-fatal): {}", exc)


def _eject_stale_bots(settings: Settings) -> None:
    """Remove leftover participants from previous runs via the Daily REST API.

    Ejects ALL participants in the room — both stale bots and zombie guest
    sessions. This prevents echo caused by orphaned browser sessions whose
    audio tracks linger on the SFU.
    """
    room_name = settings.daily_room_name
    api_key = settings.daily_api_key
    base = "https://api.daily.co/v1"
    try:
        req = Request(
            f"{base}/presence",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        participants = data.get(room_name, [])
        stale_ids = [p["id"] for p in participants]
        if not stale_ids:
            return
        logger.info(
            "[daily] found {} stale participants, ejecting all",
            len(stale_ids),
        )

        ereq = Request(
            f"{base}/rooms/{room_name}/eject",
            data=json.dumps({"ids": stale_ids}).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(ereq, timeout=5) as resp:
            result = json.loads(resp.read())
        ejected = result.get("ejectedIds", [])
        for pid in ejected:
            logger.info("[daily] ejected stale participant {}", pid)
    except Exception as exc:
        logger.debug("[daily] could not check for stale participants: {}", exc)


def build_pipeline(
    settings: Settings,
    *,
    action_runtime: ActionRuntime | None = None,
) -> tuple[PipelineTask, PipelineRunner]:
    """Construct the Pipecat pipeline and return a runnable task + runner.

    When ``action_runtime`` is provided, the actions feature is wired in:
    the system prompt is augmented with the per-hotel actions fragment,
    and the ``create_ticket`` tool is registered on the LLM service.
    """
    _pipeline_t0 = _time.perf_counter()
    mic_enabled = settings.input_mode in ("voice", "hybrid")

    if settings.transport_mode not in {"local", "daily"}:
        raise RuntimeError("TRANSPORT_MODE must be either 'local' or 'daily'.")

    if settings.transport_mode == "daily" and DailyTransport is None:
        raise RuntimeError(
            "TRANSPORT_MODE=daily requires the 'daily-python' package, "
            "which is not available on Windows. Use TRANSPORT_MODE=local "
            "or run the bot on Linux/macOS."
        )

    # In Pipecat 1.0 the transport's vad_* params are dead code. VAD must be
    # an explicit pipeline step (VADProcessor) that emits
    # VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame. The STT
    # service listens for those to know when to commit audio to the API.
    if settings.transport_mode == "daily":
        missing = [
            name
            for name, value in (
                ("DAILY_API_KEY", settings.daily_api_key),
                ("DAILY_DOMAIN", settings.daily_domain),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Daily transport requires these environment variables: " + ", ".join(missing)
            )
        if not settings.daily_room_name:
            raise RuntimeError(
                "DAILY_ROOM_NAME is not set. With dynamic rooms enabled, bots are "
                "spawned on-demand by serve.py — run 'uv run python demo-hotel/serve.py' "
                "instead of 'make run'."
            )

        room_url = f"https://{settings.daily_domain}/{settings.daily_room_name}"

        # Start pre-importing heavy Gladia modules in a background thread.
        # _eject_stale_bots() below does ~290ms of network I/O which releases
        # the GIL, giving the background thread time to do CPU-bound imports.
        _preimport_thread: threading.Thread | None = None
        if settings.stt_provider == "gladia":
            from voxtera.stt import preimport_gladia

            _preimport_thread = threading.Thread(target=preimport_gladia, daemon=True)
            _preimport_thread.start()

        _t0 = _time.perf_counter()
        _eject_stale_bots(settings)
        logger.info(
            "[startup] _eject_stale_bots took {:.0f}ms", (_time.perf_counter() - _t0) * 1000
        )

        # Ensure preimport finished before we hit _build_gladia_stt.
        if _preimport_thread is not None:
            _preimport_thread.join(timeout=5.0)

        _t0 = _time.perf_counter()

        # PSTN dial-in: if the bot was spawned for a phone call, pass the
        # call routing info so Pipecat calls pinlessCallUpdate automatically
        # when the 'dialin-ready' event fires.
        _dialin_call_id = os.environ.get("DIALIN_CALL_ID", "")
        _dialin_call_domain = os.environ.get("DIALIN_CALL_DOMAIN", "")
        _dialin_settings = None
        if _dialin_call_id and _dialin_call_domain and DailyDialinSettings is not None:
            _dialin_settings = DailyDialinSettings(
                call_id=_dialin_call_id,
                call_domain=_dialin_call_domain,
            )
            logger.info(
                "[daily] PSTN dial-in mode: call_id={}, call_domain={}",
                _dialin_call_id[:12] + "...",
                _dialin_call_domain[:12] + "...",
            )

        _is_pstn = _dialin_settings is not None
        # Daily's media server resamples PSTN 8kHz to the requested rate,
        # so we always request 16kHz in (matching Silero VAD & STT expectations).
        # Output is 8kHz for PSTN (telephony) or 48kHz for browser WebRTC.
        _audio_in_rate = 16000
        _audio_out_rate = 8000 if _is_pstn else 48000

        transport = DailyTransport(
            room_url,
            None,
            settings.bot_name,
            DailyParams(
                api_key=settings.daily_api_key,
                dialin_settings=_dialin_settings,
                audio_in_enabled=mic_enabled,
                audio_in_sample_rate=_audio_in_rate,
                audio_in_channels=1,
                audio_in_passthrough=True,
                audio_out_enabled=True,
                audio_out_sample_rate=_audio_out_rate,
                audio_out_channels=1,
                camera_out_enabled=False,
                microphone_out_enabled=True,
            ),
        )
        logger.info(
            "[startup] DailyTransport creation took {:.0f}ms",
            (_time.perf_counter() - _t0) * 1000,
        )
        logger.info("[daily] transport enabled for room {}", room_url)

        @transport.event_handler("on_app_message")
        async def _on_app_message(transport, message, sender):
            logger.info("[daily] app-message from {}: {}", sender, message)
            # Handle device-kind: enable spectral pre-emphasis for Bluetooth
            if isinstance(message, dict) and message.get("type") == "voxtera-device-kind":
                device_kind = message.get("device_kind", "")
                logger.info("[daily] device_kind reported: {}", device_kind)
                if pre_emphasis_ref is not None:
                    pre_emphasis_ref.set_enabled(device_kind == "bluetooth")

        # On-demand spawn handshake: when this bot was spawned by the launcher
        # (``serve.py`` /api/start-session), it knows its ``VOXTERA_SESSION_ID``
        # and the launcher's callback URL via env vars. Posting "ready" here
        # unblocks the launcher's ``q.get()`` and lets the browser proceed to
        # ``callObject.join``. When those env vars are unset (e.g. ``make run``
        # or the legacy always-on bot), ``post_event`` is a silent no-op and
        # this handler costs nothing.
        from voxtera import launcher_client

        # Holds the RAG retriever if rag_enabled — set later in run_bot() after
        # the RAG block. _on_joined is a late-binding closure so it will see the
        # updated value at call time.
        _rag_retriever: Retriever | None = None  # noqa: F821

        @transport.event_handler("on_joined")
        async def _on_joined(transport, data):
            logger.info(
                "[daily] joined as {} (launcher_callback={}) data={}",
                settings.bot_name,
                "enabled" if launcher_client.is_enabled() else "disabled",
                data,
            )
            await launcher_client.post_event("ready")
            asyncio.create_task(_warmup_tts_providers(settings))
            asyncio.create_task(_warmup_anthropic_llm(settings, LLM_MODEL))
            if _rag_retriever is not None:
                # Three-phase RAG warm-up (all in background, doesn't block bot):
                # (0) Load the embedding ONNX model (was previously on the
                #     critical path — 3-8s cold, ~200ms cached). Moved here
                #     so the bot posts "ready" before the model is loaded.
                # (1) chunk_cache: loads + normalises the embedding matrix
                #     so the first real query skips the SQLite read.
                # (2) result_cache: pre-runs DEFAULT_WARMUP_QUERIES so the
                #     bot answers common questions in ~0ms RAG time instead
                #     of paying 1.5s for the first CPU embedding pass.
                # Both (1) and (2) depend on the model being loaded, so chain.
                async def _do_rag_warmup() -> None:
                    from voxtera.rag.embeddings import embed_sync

                    _wt0 = _time.perf_counter()
                    await asyncio.to_thread(embed_sync, ["warmup"])
                    logger.info(
                        "[startup] RAG embed model warmup took {:.0f}ms",
                        (_time.perf_counter() - _wt0) * 1000,
                    )
                    _wt1 = _time.perf_counter()
                    await _rag_retriever.warmup(hotel_id=settings.hotel_id)
                    logger.info(
                        "[startup] RAG chunk_cache warmup took {:.0f}ms",
                        (_time.perf_counter() - _wt1) * 1000,
                    )
                    _wt2 = _time.perf_counter()
                    await _rag_retriever.warmup_queries(hotel_id=settings.hotel_id)
                    logger.info(
                        "[startup] RAG warmup_queries took {:.0f}ms",
                        (_time.perf_counter() - _wt2) * 1000,
                    )

                asyncio.create_task(_do_rag_warmup())

        # Fast-exit on Guest leave — only for on-demand mode.
        #
        # In on-demand mode (launcher spawned this bot) we want the process to
        # exit promptly when the human hangs up so the launcher can release the
        # session slot and Daily participant-minutes stop accruing. Without
        # this handler we would either wait for ``PIPELINE_IDLE_TIMEOUT_SECS``
        # or sit in the room indefinitely (idle timeout is disabled by default).
        #
        # In legacy / always-on mode (launcher disabled) we keep the current
        # behaviour: the bot stays in the room across Guest sessions. Adding
        # an early-exit there would break ``make run`` workflows where the
        # developer reconnects multiple times to the same long-running bot.
        @transport.event_handler("on_participant_left")
        async def _on_participant_left(transport, participant, reason):
            user_name = participant.get("info", {}).get("userName", "")
            logger.info("[daily] participant_left userName={!r} reason={!r}", user_name, reason)
            # Filter on the *human* leaving. Multi-Guest is out of scope; we
            # treat any non-bot leave as "the call is over."
            if user_name == settings.bot_name:
                return
            if not launcher_client.is_enabled():
                logger.debug("[daily] launcher disabled — staying in room (legacy mode)")
                return
            logger.info("[daily] guest left — queuing EndFrame to exit process cleanly")
            await launcher_client.post_event("exiting", reason=f"guest_left:{reason}")
            # ``task`` is created later in this function, but Python closures
            # bind names lazily — by the time this handler fires the bot is
            # already running, so ``task`` is defined.
            await task.queue_frame(EndFrame())

            # Force-exit watchdog. The Telegram action listener thread (and
            # asyncio's default thread pool) can take up to 300 s to join on
            # shutdown, which keeps the subprocess alive long after Daily
            # has been left — the launcher's ``Popen.wait()`` reaper stays
            # blocked, the registry slot stays "busy," and the next Start
            # click is rejected with 409.
            #
            # We give the pipeline 5 s to drain TTS / LLM frames cleanly,
            # then force-exit via ``os._exit(0)`` — bypasses asyncio cleanup,
            # bypasses the thread pool join, immediately releases the OS
            # resources. The launcher's reaper observes the exit and frees
            # the registry slot within ~100 ms.
            #
            # 5 s headroom: in the working baseline the bot finished
            # ``Left https://...`` 2 s after participant_left, so 5 s is
            # comfortable. Daemon Timer thread = does NOT prevent process
            # exit if the clean drain happens to win the race.
            def _force_exit() -> None:
                logger.warning(
                    "[daily] EndFrame drain watchdog: forcing process exit "
                    "(os._exit) after 5s — Telegram listener / asyncio "
                    "executor likely still draining"
                )
                # Best-effort synchronous Gladia session DELETE before hard
                # exit. os._exit bypasses asyncio cleanup so lazy_disconnect
                # may never run — this ensures the session slot is released
                # and Gladia doesn't see a lingering open WebSocket.
                try:
                    _gladia_branch = stt_branches.get("gladia", {})
                    _gladia_stt = _gladia_branch.get("stt")
                    if _gladia_stt is not None:
                        _sid = getattr(_gladia_stt, "_session_id", None)
                        _key = getattr(_gladia_stt, "_api_key", None)
                        _reg = getattr(_gladia_stt, "_region", None)
                        if _sid and _key:
                            _url = f"https://api.gladia.io/v2/live/{_sid}"
                            if _reg:
                                _url += f"?region={_reg}"
                            _req = Request(
                                _url,
                                method="DELETE",
                                headers={"x-gladia-key": _key},
                            )
                            with urlopen(_req, timeout=3) as _resp:  # noqa: S310
                                _resp.read()
                            logger.info("[daily] force_exit: deleted Gladia session {}", _sid)
                except Exception as _exc:  # noqa: BLE001
                    logger.debug(
                        "[daily] force_exit: Gladia session cleanup failed (non-fatal): {}",
                        _exc,
                    )
                # Best-effort call-record flush: write the WAV and stamp
                # ended_at before os._exit kills the process. flush_audio
                # is async but stop_recording's core (memoryview concat +
                # wave.open) is synchronous — run it on a throwaway loop.
                try:
                    import asyncio as _aio

                    from voxtera import call_record as _cr

                    _loop = _aio.new_event_loop()
                    _loop.run_until_complete(_cr.flush_audio())
                    _loop.run_until_complete(_cr.flush_raw_input())
                    _loop.run_until_complete(_cr.flush_stage_recorders())
                    _loop.close()
                    _cr.finalize()
                    logger.info("[daily] force_exit: call record flushed")
                except Exception as _exc2:  # noqa: BLE001
                    logger.debug(
                        "[daily] force_exit: call record flush failed (non-fatal): {}",
                        _exc2,
                    )
                os._exit(0)

            t = threading.Timer(5.0, _force_exit)
            t.daemon = True
            t.start()

        # PSTN dial-in event handlers: log connection events and terminate
        # cleanly on stop/error so the process exits and the launcher frees
        # the session slot.
        if _is_pstn:
            _pstn_webhook_ts = float(os.environ.get("PSTN_WEBHOOK_TS", "0"))

            @transport.event_handler("on_dialin_ready")
            async def _on_dialin_ready(transport, sip_endpoint):
                _ready_elapsed = _time.time() - _pstn_webhook_ts if _pstn_webhook_ts else 0
                logger.info(
                    "[pstn] dial-in ready: sip_endpoint={} (bot_startup={:.1f}s)",
                    sip_endpoint,
                    _ready_elapsed,
                )

            @transport.event_handler("on_dialin_connected")
            async def _on_dialin_connected(transport, data):
                _hold_time = _time.time() - _pstn_webhook_ts if _pstn_webhook_ts else 0
                logger.info(
                    "[pstn] dial-in connected — hold_time={:.1f}s data={}",
                    _hold_time,
                    data,
                )
                # Short delay to let codec negotiation settle before greeting
                await asyncio.sleep(0.5)
                # Greet the PSTN caller (no browser to send voxtera-ready)
                _greeting_lang, _greeting_text = resolve_greeting(settings.greeting_language)
                logger.info(
                    "[pstn] greeting caller (lang={}) — {} chars",
                    _greeting_lang,
                    len(_greeting_text),
                )
                await task.queue_frame(TTSSpeakFrame(text=_greeting_text))
                # Start idle silence watchdog now that the caller is live
                _pstn_idle_watcher.start_watchdog(task)

            @transport.event_handler("on_dialin_stopped")
            async def _on_dialin_stopped(transport, data):
                logger.info("[pstn] dial-in stopped: {}", data)
                _pstn_idle_watcher.stop()
                await task.queue_frame(EndFrame())

            @transport.event_handler("on_dialin_warning")
            async def _on_dialin_warning(transport, data):
                logger.warning("[pstn] dial-in warning: {}", data)

            @transport.event_handler("on_dialin_error")
            async def _on_dialin_error(transport, data):
                logger.error("[pstn] dial-in error: {}", data)
                await task.queue_frame(EndFrame())

        # Health monitor: exits the process when the room is empty for too
        # long, preventing transport degradation from idle WebRTC sessions.
        health_monitor: TransportHealthMonitor | None = TransportHealthMonitor(
            bot_name=settings.bot_name
        )
        health_monitor.register(transport)
    else:
        health_monitor = None
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=mic_enabled,
                audio_in_sample_rate=16000,  # Silero VAD requires 8kHz or 16kHz
                audio_in_channels=1,
                audio_in_passthrough=True,
                audio_out_enabled=True,
                audio_out_sample_rate=24000,
                audio_out_channels=1,
            )
        )

    # The mic-side processors only matter when audio input is enabled. In
    # text-only mode we skip building them — keeps the pipeline lean and
    # avoids loading the Silero ONNX model unnecessarily.
    #
    # Build only the configured STT provider (STT_PROVIDER env var) to
    # minimize startup time. Runtime switching is not supported.
    stt = None
    needs_vad = True
    stt_branches: dict[str, dict] = {}
    stt_router: STTRouter | None = None
    if mic_enabled:
        _t0 = _time.perf_counter()
        if settings.transport_mode == "daily":
            name = settings.stt_provider
            builder = _STT_BUILDERS.get(name)
            if builder is None:
                raise RuntimeError(
                    f"Unknown STT_PROVIDER={name!r}. Use one of: {', '.join(_STT_BUILDERS)}."
                )
            _t_build = _time.perf_counter()
            built = builder(settings)
            logger.info(
                "[startup] STT builder() took {:.0f}ms", (_time.perf_counter() - _t_build) * 1000
            )
            if built is None:
                raise RuntimeError(f"STT_PROVIDER={name!r} is missing required credentials.")
            _t_router = _time.perf_counter()
            _ig = STTGate(kind="input", label=name)
            logger.info(
                "[startup]   STTGate(input) {:.0f}ms", (_time.perf_counter() - _t_router) * 1000
            )
            _t2 = _time.perf_counter()
            _og = STTGate(kind="output", label=name)
            logger.info("[startup]   STTGate(output) {:.0f}ms", (_time.perf_counter() - _t2) * 1000)
            _t2 = _time.perf_counter()
            _nf = TranscriptionNoiseFilter(stt=built)
            logger.info("[startup]   NoiseFilter {:.0f}ms", (_time.perf_counter() - _t2) * 1000)
            stt_branches[name] = {
                "stt": built,
                "input_gate": _ig,
                "output_gate": _og,
                "noise_filter": _nf,
            }
            logger.info(
                "[startup] STT branches dict took {:.0f}ms",
                (_time.perf_counter() - _t_router) * 1000,
            )
            _t_router2 = _time.perf_counter()
            initial_provider = name
            stt_router = STTRouter(stt_branches, initial=initial_provider)
            stt = stt_branches[initial_provider]["stt"]
            logger.info(
                "[startup] STTRouter() took {:.0f}ms", (_time.perf_counter() - _t_router2) * 1000
            )
            logger.info(
                "[startup] STT router/gates took {:.0f}ms",
                (_time.perf_counter() - _t_router) * 1000,
            )
            # Always run Silero VAD: at least one branch (Whisper or Google)
            # needs it, and Deepgram tolerates redundant VAD events.
            needs_vad = True
        else:
            stt, needs_vad = _build_stt(settings)
        logger.info("[startup] STT providers took {:.0f}ms", (_time.perf_counter() - _t0) * 1000)

    _t0 = _time.perf_counter()
    vad_processor = None
    if mic_enabled and needs_vad:
        _vad_stop_secs = settings.pstn_vad_stop_secs if _is_pstn else settings.vad_stop_secs
        vad_processor = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    stop_secs=_vad_stop_secs,
                    start_secs=settings.vad_start_secs,
                    min_volume=settings.vad_min_volume,
                    confidence=settings.vad_confidence,
                ),
            )
        )
        logger.info(
            "[vad] stop_secs={} start_secs={} min_volume={} confidence={} transport={}",
            _vad_stop_secs,
            settings.vad_start_secs,
            settings.vad_min_volume,
            settings.vad_confidence,
            "pstn" if _is_pstn else settings.transport_mode,
        )
    if mic_enabled and not needs_vad:
        logger.info("[vad] external VAD skipped (STT provides its own)")
    logger.info("[startup] VAD (Silero) took {:.0f}ms", (_time.perf_counter() - _t0) * 1000)

    _t0 = _time.perf_counter()
    if LLM_MODEL in ("gpt-4o-mini",):
        llm: FrameProcessor = OpenAILLMService(
            api_key=settings.openai_api_key,
            model=LLM_MODEL,
        )
        logger.info("[llm] using OpenAI model {}", LLM_MODEL)
    else:
        llm = AnthropicLLMService(
            api_key=settings.anthropic_api_key,
            settings=AnthropicLLMService.Settings(
                model=LLM_MODEL,
                # Anthropic prompt caching: caches the system prompt + tool
                # schemas across turns for the duration of a 5-minute window.
                # The system prompt is ~660 words (plus the actions fragment
                # ~250 words and any hotel addendum), which is ~1900 tokens.
                # Without caching every turn re-tokenizes and re-processes
                # that prefix; with caching, TTFT drops ~100-200ms after the
                # first turn warms the cache.
                enable_prompt_caching=True,
                # Bound the reply length at 250 tokens (~180 words). Combined
                # with the brevity rules in the system prompt, real replies
                # land at 8-30 words; this cap mainly prevents pathological
                # runaway generation. Lower than the default 4096 cuts the
                # tail-latency variance on rare long responses.
                max_tokens=250,
            ),
        )
        logger.info(
            "[llm] using Anthropic model {} (prompt-caching=on, max_tokens=250)",
            LLM_MODEL,
        )
    logger.info("[startup] LLM service took {:.0f}ms", (_time.perf_counter() - _t0) * 1000)

    # Build TTS providers. In Daily mode we build every provider with valid
    # credentials and run them as gated parallel branches like STT, so the
    # browser can flip between OpenAI tts-1 and Google Chirp 3 HD instantly.
    # In local mode we keep a single TTS instance.
    _t0 = _time.perf_counter()
    tts_branches: dict[str, dict] = {}
    tts_router: TTSRouter | None = None
    if settings.transport_mode == "daily":
        for name, builder in _TTS_BUILDERS.items():
            try:
                built = builder(settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[tts-router] failed to build {}: {}", name, exc)
                built = None
            if built is not None:
                tts_branches[name] = {
                    "tts": built,
                    "input_gate": TTSGate(kind="input", label=name),
                    "output_gate": TTSGate(kind="output", label=name),
                }
        if not tts_branches:
            raise RuntimeError(
                "No TTS provider could be built. Set OPENAI_API_KEY or "
                "GOOGLE_APPLICATION_CREDENTIALS."
            )
        initial_tts_provider = (
            settings.tts_provider
            if settings.tts_provider in tts_branches
            else next(iter(tts_branches))
        )
        if initial_tts_provider != settings.tts_provider:
            logger.warning(
                "[tts-router] requested provider {!r} not available — starting with {!r}",
                settings.tts_provider,
                initial_tts_provider,
            )
        tts_router = TTSRouter(tts_branches, initial=initial_tts_provider)
        tts = tts_branches[initial_tts_provider]["tts"]
    else:
        # Local mode: build the single configured provider only.
        builder = _TTS_BUILDERS.get(settings.tts_provider)
        if builder is None:
            raise RuntimeError(
                f"Unknown TTS_PROVIDER={settings.tts_provider!r}. "
                f"Use one of: {', '.join(_TTS_BUILDERS)}."
            )
        built = builder(settings)
        if built is None:
            raise RuntimeError(
                f"TTS_PROVIDER={settings.tts_provider!r} is missing required credentials."
            )
        tts = built
    logger.info("[startup] TTS providers took {:.0f}ms", (_time.perf_counter() - _t0) * 1000)

    # Conversation context. The system prompt does the heavy lifting on the
    # multilingual requirement — see src/voxtera/prompts/system_prompt.py.
    # When the actions feature is enabled, the per-hotel actions fragment
    # is appended (categories, confirmation rule, language split, etc.)
    # before the prompt is handed to the LLM.
    if action_runtime is not None:
        from voxtera.actions import compose_system_prompt, wire_actions, wire_web_search

        system_text = compose_system_prompt(
            SYSTEM_PROMPT, action_runtime.hotel_config, web_search_enabled=True
        )
    else:
        system_text = SYSTEM_PROMPT
    messages: list[dict[str, str]] = [{"role": "system", "content": system_text}]
    context = LLMContext(messages)

    # Override Pipecat's default user-turn-stop strategy on two axes:
    #
    # 1. cpu_count — Pipecat's default builds LocalSmartTurnAnalyzerV3() with
    #    cpu_count=1. The ONNX model runs faster with more intra-op threads;
    #    settings.smart_turn_cpu_count auto-detects a sane value per host.
    # 2. stop_secs — SmartTurnParams.stop_secs is the hard fallback: after
    #    this many seconds of continuous silence (counted from the VAD stop
    #    event) the turn is force-ended even if the model still predicts
    #    "incomplete". Pipecat's default is 3.0s, and trace analysis showed
    #    this single value is the entire source of the ~3s stt->llm p95
    #    spikes — turns where the model mispredicts "incomplete" wait out the
    #    full window. settings.smart_turn_stop_secs (default 2.0) caps it.
    #
    # For PSTN calls we skip SmartTurn entirely and use a simple speech
    # timeout strategy instead. Phone callers speak in complete sentences;
    # the ONNX model adds 400-1400ms of deliberation per turn with no benefit
    # on narrow-band telephony audio. SpeechTimeoutUserTurnStopStrategy waits
    # user_speech_timeout (0.4s) after VAD silence before triggering — fast
    # and predictable.
    _t0 = _time.perf_counter()
    if _is_pstn:
        _user_turn_stop = [
            SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=settings.pstn_user_speech_timeout)
        ]
        logger.info(
            "[turn-detector] PSTN mode — using SpeechTimeoutUserTurnStopStrategy "
            "(user_speech_timeout={}s, no SmartTurn)",
            settings.pstn_user_speech_timeout,
        )
    else:
        _smart_turn = LocalSmartTurnAnalyzerV3(
            cpu_count=settings.smart_turn_cpu_count,
            params=SmartTurnParams(stop_secs=settings.smart_turn_stop_secs),
        )
        # Tail-aware (subclass of TurnAnalyzerUserTurnStopStrategy): stops the
        # turn the moment the STT's trailing final lands instead of always
        # paying the fixed p99 timeout, and holds for an in-flight utterance
        # so a split sentence isn't cut in half (see voxtera/turns.py).
        _user_turn_stop = [TailAwareTurnStopStrategy(turn_analyzer=_smart_turn)]
        logger.info(
            "[turn-detector] LocalSmartTurnAnalyzerV3 cpu_count={} stop_secs={}",
            settings.smart_turn_cpu_count,
            settings.smart_turn_stop_secs,
        )
    logger.info("[startup] Turn detector took {:.0f}ms", (_time.perf_counter() - _t0) * 1000)
    _user_turn_strategies = UserTurnStrategies(
        start=default_user_turn_start_strategies(),
        stop=_user_turn_stop,
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=_user_turn_strategies),
    )

    if action_runtime is not None:
        # Register the create_ticket tool on the LLM service AND attach
        # its schema to the LLMContext so Claude sees it in every turn.
        wire_actions(
            llm=llm,
            context=context,
            hotel_config=action_runtime.hotel_config,
            sink=action_runtime.sink,
        )
        wire_web_search(llm=llm, context=context)
        logger.info(
            "[actions] enabled for hotel={!r} channel={}",
            action_runtime.hotel_config.hotel_name,
            action_runtime.hotel_config.telegram_channel_id,
        )

    # Build the pipeline list. In text-only mode the mic-side processors
    # (audio level monitor, VAD, STT) are skipped entirely.
    processors: list = [transport.input()]
    # Raw input recorder: captures exactly what the browser sends, before any
    # processing. Written to logs/calls/<session_id>/input_raw.wav on shutdown.
    if settings.call_recording_enabled and settings.call_recording_audio and mic_enabled:
        from voxtera.call_record import RawInputRecorder

        processors.append(RawInputRecorder(sample_rate=16000))
        logger.info("[call-record] raw input recorder attached to pipeline")

    # Per-stage audio taps (diagnostic). When STAGE_AUDIO_DEBUG is on, drop a
    # StageRecorder after each pre-gate mic stage so every stage writes a
    # full-length, time-aligned stage_<label>.wav. Comparing them pinpoints
    # which processor introduces silence/dropouts. The helper appends a tap
    # only when enabled and mic-side processors exist.
    _stage_tap = None
    if settings.stage_audio_debug and mic_enabled:
        from voxtera.call_record import StageRecorder

        def _stage_tap(label: str) -> None:  # noqa: F811
            processors.append(StageRecorder(label, sample_rate=16000))

        logger.info("[stage-record] per-stage audio taps ENABLED (STAGE_AUDIO_DEBUG)")
    # --- Diagnostic probes ---
    _probing = True
    if _probing:
        processors.append(PipelineProbe("after_transport_in"))
    # Track live processor refs so we can register live-tunable knobs at
    # the end of build_pipeline (see register_pipeline_knobs below).
    rnnoise_denoiser_ref = None
    leakage_guard_ref = None
    user_frame_suppressor_ref = None
    interruption_resumer_ref = None
    # Spectral pre-emphasis for Bluetooth devices — disabled by default,
    # enabled at runtime when browser reports device_kind == "bluetooth".
    pre_emphasis_ref: HighShelfPreEmphasis | None = None
    if mic_enabled:
        pre_emphasis_ref = HighShelfPreEmphasis(sample_rate=16000, gain_db=5.0, frequency=2000.0)
        processors.append(pre_emphasis_ref)
        if _probing:
            processors.append(PipelineProbe("after_pre_emphasis"))
        if _stage_tap:
            _stage_tap("1_pre_emphasis")

        if settings.rnnoise_enabled:
            if _RNNoise is None:
                logger.warning(
                    "[rnnoise] enabled but pyrnnoise is unavailable; running without denoiser"
                )
            else:
                rnnoise_denoiser_ref = RNNoiseDenoiser(sample_rate=16000)
                processors.append(rnnoise_denoiser_ref)
                if _probing:
                    processors.append(PipelineProbe("after_rnnoise"))
                if _stage_tap:
                    _stage_tap("2_rnnoise")
                logger.info("[rnnoise] enabled")

        leakage_guard_ref = PlaybackLeakageGuard(allow_interruptions=settings.allow_interruptions)
        processors.append(leakage_guard_ref)
        if _probing:
            processors.append(PipelineProbe("after_leakage_guard"))
        if _stage_tap:
            _stage_tap("3_leakage_guard")
        processors.append(AudioLevelMonitor())
        if _probing:
            processors.append(PipelineProbe("after_audio_monitor"))
        if _stage_tap:
            _stage_tap("4_audio_monitor")
        if vad_processor:
            processors.append(vad_processor)
            if _probing:
                processors.append(PipelineProbe("after_vad"))
            if health_monitor is not None:
                processors.append(ActivityNotifier(health_monitor))
            # PSTN idle silence detection: ends call if caller is silent too long
            if _is_pstn:
                _idle_timeout = float(os.environ.get("PSTN_IDLE_TIMEOUT_SECS", "45"))
                _followup_timeout = float(os.environ.get("PSTN_IDLE_FOLLOWUP_SECS", "15"))
                _pstn_idle_watcher = PstnIdleWatcher(
                    idle_timeout=_idle_timeout,
                    followup_timeout=_followup_timeout,
                )
                processors.append(_pstn_idle_watcher)
                logger.info(
                    "[pstn-idle] watcher enabled: idle={}s followup={}s",
                    _idle_timeout,
                    _followup_timeout,
                )
        if stt_router is not None:
            # Daily mode: parallel STT branches gated by the router. Each
            # branch is [input_gate, stt, noise_filter, output_gate]; only
            # the active branch's gates let frames through.
            processors.append(stt_router)
            if _probing:
                processors.append(PipelineProbe("after_stt_router"))
            processors.append(
                ParallelPipeline(
                    *[
                        [
                            branch["input_gate"],
                            branch["stt"],
                            branch["noise_filter"],
                            branch["output_gate"],
                        ]
                        for branch in stt_branches.values()
                    ]
                )
            )
            if _probing:
                processors.append(PipelineProbe("after_parallel_stt"))
            user_frame_suppressor_ref = BotActiveUserFrameSuppressor(
                allow_interruptions=settings.allow_interruptions
            )
            processors.append(user_frame_suppressor_ref)
            if _probing:
                processors.append(PipelineProbe("after_suppressor"))
        else:
            processors.extend(
                [
                    stt,
                ]
            )
            if _probing:
                processors.append(PipelineProbe("after_stt"))
            processors.extend(
                [
                    TranscriptionNoiseFilter(stt=stt),
                ]
            )
            if _probing:
                processors.append(PipelineProbe("after_noise_filter"))
            user_frame_suppressor_ref = BotActiveUserFrameSuppressor(
                allow_interruptions=settings.allow_interruptions
            )
            processors.append(user_frame_suppressor_ref)
            if _probing:
                processors.append(PipelineProbe("after_suppressor"))
        if settings.transport_mode == "daily":
            processors.append(UserTranscriptBroadcaster())
        # TranscriptStageTimer must come BEFORE context_aggregator.user(): the
        # aggregator consumes TranscriptionFrame to fold the text into the LLM
        # context messages, so anything downstream of it never sees the frame.
        # This processor stamps the ``transcript`` anchor on TurnTracker and
        # emits the ``stt`` stage event read by the trace dashboard.
        processors.append(
            TranscriptStageTimer(
                "voxtera",
                hotel_id=settings.hotel_id if settings.rag_enabled else None,
            )
        )
        if _probing:
            processors.append(PipelineProbe("after_transcript_timer"))
    # AutoTTSLanguageSwitcher must observe TranscriptionFrame BEFORE the
    # context aggregator + LLM consume it. Previously this was placed
    # later in the pipeline (after the LLM) and silently never fired —
    # leaving Google Chirp 3 HD speaking Romanian text with the en-US
    # voice on every turn. Placing it here means: STT emits transcript →
    # switcher sees frame.language → updates TTS voice ID locale → THEN
    # context aggregator + LLM consume the frame and produce the reply,
    # which now goes to the locale-correct TTS.
    if settings.transport_mode == "daily" and tts_router is not None:
        processors.append(AutoTTSLanguageSwitcher(tts_router=tts_router))
    # Instant-acknowledgment filler: plays a short, varied backchannel the
    # moment the guest stops speaking, masking the LLM+TTS latency gap with
    # natural speech instead of dead silence. It runs in parallel with the
    # LLM — it does NOT delay the real answer. Placement is load-bearing: it
    # must come AFTER AutoTTSLanguageSwitcher (so the TTS is already on the
    # guest's language when the filler's TTSSpeakFrame reaches it) and BEFORE
    # context_aggregator.user() (which consumes TranscriptionFrame, so nothing
    # downstream of it ever sees the trigger). Gated on mic_enabled because
    # the trigger only exists for voice turns — typed turns produce no
    # TranscriptionFrame.
    if settings.filler_enabled and mic_enabled:
        processors.append(InstantAckFiller())
        if _probing:
            processors.append(PipelineProbe("after_filler"))
        logger.info("[filler] instant-acknowledgment filler enabled")
    # Interruption resume: observes barge-ins and, when the guest cuts the bot
    # off early, injects a note so the answering LLM can decide whether to
    # return to the unfinished answer. Built whenever the mic is live — NOT
    # gated on allow_interruptions (itself a live knob), so it is ready if
    # interruptions are toggled on mid-session; it stays inert until a real
    # InterruptionFrame arrives. Must sit upstream of context_aggregator.user()
    # so the injected note lands in context before the LLM run.
    if mic_enabled:
        interruption_resumer_ref = InterruptionResumer(
            enabled=settings.interruption_resume_enabled,
            resume_window_secs=settings.interruption_resume_window_secs,
        )
        processors.append(interruption_resumer_ref)
        if _probing:
            processors.append(PipelineProbe("after_interruption_resumer"))
        logger.info(
            "[interruption-resumer] enabled={} window={}s",
            settings.interruption_resume_enabled,
            settings.interruption_resume_window_secs,
        )
    ctx_user_stage_state = None
    if mic_enabled:
        from voxtera.observability import (
            ContextUserStageEndTimer,
            ContextUserStageStartTimer,
            _ContextUserStageState,
        )

        ctx_user_stage_state = _ContextUserStageState()
        processors.append(ContextUserStageStartTimer("voxtera", state=ctx_user_stage_state))
        if _probing:
            processors.append(PipelineProbe("after_ctx_user_stage_start"))
    rag_language_tracker = None
    if settings.rag_enabled:
        from voxtera.rag.injector import CurrentTurnLanguageTracker

        rag_language_tracker = CurrentTurnLanguageTracker()
        processors.append(rag_language_tracker)
        if _probing:
            processors.append(PipelineProbe("after_rag_language_tracker"))
    processors.append(context_aggregator.user())
    if ctx_user_stage_state is not None:
        from voxtera.observability import ContextUserStageEndTimer

        processors.append(ContextUserStageEndTimer("voxtera", state=ctx_user_stage_state))
        if _probing:
            processors.append(PipelineProbe("after_ctx_user_stage_end"))
    if _probing:
        processors.append(PipelineProbe("after_ctx_user"))
    processors.append(LLMRunGuard())
    if _probing:
        processors.append(PipelineProbe("after_llm_guard"))
    if settings.transport_mode == "daily":
        processors.append(BrowserTextInputController())

    # RAG: optionally inject hotel knowledge before the LLM sees the context.
    if settings.rag_enabled:
        _t0 = _time.perf_counter()
        # The embedding model load (3-8s cold) is deferred to _on_joined so
        # it doesn't block the "ready" event. The Retriever/Store/Injector are
        # cheap to create (object init + SQLite schema) and are wired in now.
        # The first real RAG query will still work even if the model hasn't
        # finished loading — it will just be slower on that one turn (the
        # retriever lazily loads chunks from SQLite if the cache is empty).
        from voxtera.rag.injector import RAGContextInjector
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = ChunksStore(db_path)
        store.init_schema()
        retriever = Retriever(store, top_k=settings.rag_top_k, min_score=settings.rag_min_score)
        _rag_retriever = retriever  # expose to _on_joined closure for warmup
        language_getter = None
        if rag_language_tracker is not None:

            def language_getter() -> str | None:
                return rag_language_tracker.current_language

        rag_injector = RAGContextInjector(
            retriever,
            hotel_id=settings.hotel_id,
            language_getter=language_getter,
        )
        logger.info(
            "[rag] retriever top_k={} min_score={}",
            settings.rag_top_k,
            settings.rag_min_score,
        )
        processors.append(rag_injector)
        if _probing:
            processors.append(PipelineProbe("after_rag"))
        logger.info("[rag] enabled for hotel_id={!r}", settings.hotel_id)
        logger.info(
            "[startup] RAG init (store+retriever) took {:.0f}ms",
            (_time.perf_counter() - _t0) * 1000,
        )

    # Time context: give the LLM the guest's current local time on every turn,
    # so it can answer time-sensitive questions (opening hours, checkout time,
    # "is the spa still open?"). Like RAGContextInjector it appends to the user
    # message — never the cached system prompt — so Anthropic prompt caching
    # still hits. Added in every transport mode; with no browser timezone it
    # falls back to the server's own clock.
    #
    # Skipped for the travel_agent brain: its TravelAgentBrain reads the raw
    # utterance out of the LLMContext and feeds it to ConciergePipeline, which
    # does its own decomposition — an appended "[current time …]" note would
    # pollute that utterance. The concierge has its own time handling where it
    # matters.
    if settings.bot_brain != "travel_agent":
        from voxtera.time_context import TimeContextInjector

        processors.append(TimeContextInjector())

    # Answering brain: the hotel LLM, or the ConciergePipeline-backed
    # TravelAgentBrain when BOT_BRAIN=travel_agent. The brain emits the same
    # LLMFullResponseStart/LLMText/LLMFullResponseEnd frames the LLM does, so
    # everything downstream (TTS, DemoEventBroadcaster, assistant aggregator)
    # is unchanged. ``llm`` is still built above but simply not appended here.
    if settings.bot_brain == "travel_agent":
        from voxtera.travel_agent_brain import TravelAgentBrain

        processors.append(TravelAgentBrain())
        logger.info("[brain] BOT_BRAIN=travel_agent → ConciergePipeline answers each turn")
    else:
        processors.extend(
            [
                llm,
            ]
        )
    if _probing:
        processors.append(PipelineProbe("after_llm"))
    processors.append(
        PipelineTracer("voxtera", hotel_id=settings.hotel_id if settings.rag_enabled else None),
    )
    if settings.transport_mode == "daily":
        processors.append(DemoEventBroadcaster())
        if stt:
            if stt_router is not None:
                processors.append(LanguageSwitcher(router=stt_router))
            else:
                processors.append(LanguageSwitcher(stt=stt, provider=settings.stt_provider))
        processors.append(
            ModelSwitcher(
                llm=llm,
                tts=tts,
                initial_llm_model=LLM_MODEL,
                initial_tts_voice=settings.default_tts_voice,
                tts_router=tts_router,
            )
        )
        # NOTE: AutoTTSLanguageSwitcher used to be added here. It was moved
        # earlier in the pipeline (right after the STT branches, before
        # context_aggregator.user()) because the LLM was consuming
        # TranscriptionFrame before this position, leaving the switcher
        # silently inactive across the entire session. See the comment at
        # the earlier insertion point for the full rationale.
        # Greeting controller: defers the startup greeting until the browser
        # sends {type: 'voxtera-ready'} after the transcript page is visible,
        # so the bot never speaks into an empty room.
        # Resolve the {hotel_name} placeholder once, up front — "Welcome to
        # Casa Dell Arte" when a hotel config is loaded, "our hotel" otherwise.
        _hotel_name = action_runtime.hotel_config.hotel_name if action_runtime else None
        greeting_lang, greeting_text = resolve_greeting(
            settings.greeting_language, hotel_name=_hotel_name
        )
        _greetings_map, _timed_greetings = apply_hotel_name(_hotel_name)
        logger.info(
            "[greeting] language={} (preference={}, hotel={}) — "
            "awaiting voxtera-ready from browser",
            greeting_lang,
            settings.greeting_language,
            _hotel_name or "generic",
        )
        # Pass both greeting catalogs so the controller can pick the greeting
        # by the guest's selected language AND local time of day, from the
        # voxtera-language / voxtera-timezone messages the browser sends
        # before voxtera-ready (see GreetingController).
        processors.append(
            GreetingController(
                greeting_text=greeting_text,
                greetings_map=_greetings_map,
                timed_greetings=_timed_greetings,
                default_language=greeting_lang,
            )
        )
    if tts_router is not None:
        # Daily mode: parallel TTS branches gated by the router.
        processors.append(tts_router)
        processors.append(
            ParallelPipeline(
                *[
                    [
                        branch["input_gate"],
                        branch["tts"],
                        branch["output_gate"],
                    ]
                    for branch in tts_branches.values()
                ]
            )
        )
    else:
        processors.append(tts)
        if _probing:
            processors.append(PipelineProbe("after_tts"))
    # TTSStageTimer must come AFTER the TTS service: TTSStartedFrame is
    # emitted downstream by TTS and never bubbles back upstream. Placing
    # the timer here lets it stamp the ``tts_started`` anchor that
    # PipelineTracer reads when measuring tts_ttft on BotStartedSpeaking.
    processors.append(TTSStageTimer("voxtera"))
    if _probing:
        processors.append(PipelineProbe("after_tts_timer"))
    processors.extend(
        [
            transport.output(),
        ]
    )
    if _probing:
        processors.append(PipelineProbe("after_transport_out"))
    # Call recording: capture the whole call as a stereo WAV. Placement is
    # load-bearing — it must sit AFTER transport.output() so it sees both the
    # guest's input audio (passed through the pipeline; audio_in_passthrough
    # is on) and the bot's output audio. It auto-starts on StartFrame and the
    # base AudioBufferProcessor writes the WAV on EndFrame/CancelFrame. See
    # voxtera.call_record.CallAudioRecorder. Transcript + metadata recording
    # is independent of this and stays on whenever call_recording_enabled.
    if settings.call_recording_enabled and settings.call_recording_audio:
        from voxtera.call_record import CallAudioRecorder

        processors.append(CallAudioRecorder())
        logger.info("[call-record] audio recorder attached to pipeline")
    processors.append(context_aggregator.assistant())

    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=settings.allow_interruptions,
            audio_in_sample_rate=16000,
            # Must match the Daily transport rate above — Pipecat resamples
            # the 24 kHz TTS frames up to 48 kHz before hitting the transport.
            audio_out_sample_rate=48000,
            # Enable usage metrics so LLM services emit MetricsFrame with
            # prompt-cache stats (cache_read/cache_creation_input_tokens).
            # PipelineTracer logs these to verify caching effectiveness.
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        enable_rtvi=False,
        cancel_on_idle_timeout=settings.pipeline_idle_timeout_secs is not None,
        idle_timeout_secs=settings.pipeline_idle_timeout_secs,
    )

    runner = PipelineRunner(handle_sigint=True)

    # Register live-tunable knobs against the running processors. This must
    # come after every processor exists but before the runner starts, so the
    # initial /knobs snapshot reflects the actual built state. Knobs whose
    # processor wasn't built (e.g. RNNoise when disabled) are left in
    # display-only mode (no apply handler).
    register_pipeline_knobs(
        settings=settings,
        vad_processor=vad_processor,
        leakage_guard=leakage_guard_ref,
        user_frame_suppressor=user_frame_suppressor_ref,
        interruption_resumer=interruption_resumer_ref,
        rnnoise_denoiser=rnnoise_denoiser_ref,
        stt_router=stt_router,
        tts_router=tts_router,
        llm=llm,
        tts=tts,
    )

    logger.info(
        "[startup] build_pipeline TOTAL took {:.0f}ms",
        (_time.perf_counter() - _pipeline_t0) * 1000,
    )
    return task, runner
