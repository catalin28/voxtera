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
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService


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


try:
    from pipecat.transports.daily.transport import DailyParams, DailyTransport
except Exception:  # daily-python not available on Windows
    DailyParams = None  # type: ignore[assignment,misc]
    DailyTransport = None  # type: ignore[assignment,misc]
from pipecat.transports.local.audio import (  # noqa: E402
    LocalAudioTransport,
    LocalAudioTransportParams,
)

from voxtera.audio import (  # noqa: E402
    AudioLevelMonitor,
    BotActiveUserFrameSuppressor,
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
    LanguageSwitcher,
    LLMRunGuard,
    ModelSwitcher,
)
from voxtera.observability import (  # noqa: E402
    DemoEventBroadcaster,
    PipelineTracer,
    UserTranscriptBroadcaster,
)
from voxtera.prompts import SYSTEM_PROMPT, resolve_greeting  # noqa: E402
from voxtera.routing import STTGate, STTRouter, TTSGate, TTSRouter  # noqa: E402
from voxtera.stt import _STT_BUILDERS, _build_stt  # noqa: E402
from voxtera.tts import _TTS_BUILDERS  # noqa: E402


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
                ("DAILY_ROOM_NAME", settings.daily_room_name),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Daily transport requires these environment variables: " + ", ".join(missing)
            )

        room_url = f"https://{settings.daily_domain}/{settings.daily_room_name}"
        _eject_stale_bots(settings)
        transport = DailyTransport(
            room_url,
            None,
            settings.bot_name,
            DailyParams(
                api_key=settings.daily_api_key,
                audio_in_enabled=mic_enabled,
                audio_in_sample_rate=16000,
                audio_in_channels=1,
                audio_in_passthrough=True,
                audio_out_enabled=True,
                # 48 kHz = WebRTC native. The OpenAI TTS service is pinned
                # to 24 kHz at construction (see ``voxtera/tts.py``), and
                # Pipecat's BaseOutputTransport resampler upsamples 24 → 48
                # before this transport hands audio to daily-python. Setting
                # this to 24 kHz instead gives chipmunk playback in browsers
                # because daily-python's WebRTC layer doesn't always
                # negotiate non-native rates correctly.
                audio_out_sample_rate=48000,
                audio_out_channels=1,
                camera_out_enabled=False,
                microphone_out_enabled=True,
            ),
        )
        logger.info("[daily] transport enabled for room {}", room_url)

        @transport.event_handler("on_app_message")
        async def _on_app_message(transport, message, sender):
            logger.info("[daily] app-message from {}: {}", sender, message)
            # DEBUG: check input transport state
            inp = transport.input()
            logger.info(
                "[daily] input._next={} input._started={}",
                inp._next,
                inp._FrameProcessor__started if hasattr(inp, "_FrameProcessor__started") else "N/A",
            )

        # On-demand spawn handshake: when this bot was spawned by the launcher
        # (``serve.py`` /api/start-session), it knows its ``VOXTERA_SESSION_ID``
        # and the launcher's callback URL via env vars. Posting "ready" here
        # unblocks the launcher's ``q.get()`` and lets the browser proceed to
        # ``callObject.join``. When those env vars are unset (e.g. ``make run``
        # or the legacy always-on bot), ``post_event`` is a silent no-op and
        # this handler costs nothing.
        from voxtera import launcher_client

        # Note: Pipecat's DailyTransport names this event ``on_joined`` (not
        # ``on_joined_meeting`` — the latter is the daily-python low-level
        # callback name). Using the wrong name silently does nothing: Pipecat
        # accepts the registration but logs a warning at transport init and
        # never fires the handler. Source of truth for valid event names is
        # ``pipecat/transports/daily/transport.py`` near ``_register_event_handler``.
        @transport.event_handler("on_joined")
        async def _on_joined(transport, data):
            logger.info(
                "[daily] joined as {} (launcher_callback={}) data={}",
                settings.bot_name,
                "enabled" if launcher_client.is_enabled() else "disabled",
                data,
            )
            await launcher_client.post_event("ready")

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
                os._exit(0)

            t = threading.Timer(5.0, _force_exit)
            t.daemon = True
            t.start()

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
    # In Daily mode we build *all* STT providers whose credentials are
    # configured, run them in parallel branches gated by STTRouter, and let
    # the browser flip the active branch at runtime via voxtera-stt
    # messages. In local mode we keep the simple single-STT path because
    # the CLI has no UI to switch from.
    stt = None
    needs_vad = True
    stt_branches: dict[str, dict] = {}
    stt_router: STTRouter | None = None
    if mic_enabled:
        if settings.transport_mode == "daily":
            for name, builder in _STT_BUILDERS.items():
                try:
                    built = builder(settings)
                except Exception as exc:  # noqa: BLE001 — log and skip the branch
                    logger.warning("[stt-router] failed to build {}: {}", name, exc)
                    built = None
                if built is not None:
                    stt_branches[name] = {
                        "stt": built,
                        "input_gate": STTGate(kind="input", label=name),
                        "output_gate": STTGate(kind="output", label=name),
                        "noise_filter": TranscriptionNoiseFilter(stt=built),
                    }
            if not stt_branches:
                raise RuntimeError(
                    "No STT provider could be built. Set at least one of "
                    "OPENAI_API_KEY, DEEPGRAM_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS."
                )
            initial_provider = (
                settings.stt_provider
                if settings.stt_provider in stt_branches
                else next(iter(stt_branches))
            )
            if initial_provider != settings.stt_provider:
                logger.warning(
                    "[stt-router] requested provider {!r} not available — " "starting with {!r}",
                    settings.stt_provider,
                    initial_provider,
                )
            stt_router = STTRouter(stt_branches, initial=initial_provider)
            stt = stt_branches[initial_provider]["stt"]
            # Always run Silero VAD: at least one branch (Whisper or Google)
            # needs it, and Deepgram tolerates redundant VAD events.
            needs_vad = True
        else:
            stt, needs_vad = _build_stt(settings)

    vad_processor = None
    if mic_enabled and needs_vad:
        vad_processor = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    stop_secs=settings.vad_stop_secs,
                    start_secs=settings.vad_start_secs,
                    min_volume=settings.vad_min_volume,
                    confidence=settings.vad_confidence,
                ),
            )
        )
    if mic_enabled and not needs_vad:
        logger.info("[vad] external VAD skipped (STT provides its own)")

    llm = AnthropicLLMService(
        api_key=settings.anthropic_api_key,
        settings=AnthropicLLMService.Settings(model=LLM_MODEL),
    )

    # Build TTS providers. In Daily mode we build every provider with valid
    # credentials and run them as gated parallel branches like STT, so the
    # browser can flip between OpenAI tts-1 and Google Chirp 3 HD instantly.
    # In local mode we keep a single TTS instance.
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
                "[tts-router] requested provider {!r} not available — " "starting with {!r}",
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
                f"Unknown TTS_PROVIDER={settings.tts_provider!r}. " "Use one of: openai, google."
            )
        built = builder(settings)
        if built is None:
            raise RuntimeError(
                f"TTS_PROVIDER={settings.tts_provider!r} is missing required credentials."
            )
        tts = built

    # Conversation context. The system prompt does the heavy lifting on the
    # multilingual requirement — see src/voxtera/prompts/system_prompt.py.
    # When the actions feature is enabled, the per-hotel actions fragment
    # is appended (categories, confirmation rule, language split, etc.)
    # before the prompt is handed to the LLM.
    if action_runtime is not None:
        from voxtera.actions import compose_system_prompt, wire_actions

        system_text = compose_system_prompt(SYSTEM_PROMPT, action_runtime.hotel_config)
    else:
        system_text = SYSTEM_PROMPT
    messages: list[dict[str, str]] = [{"role": "system", "content": system_text}]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    if action_runtime is not None:
        # Register the create_ticket tool on the LLM service AND attach
        # its schema to the LLMContext so Claude sees it in every turn.
        wire_actions(
            llm=llm,
            context=context,
            hotel_config=action_runtime.hotel_config,
            sink=action_runtime.sink,
        )
        logger.info(
            "[actions] enabled for hotel={!r} channel={}",
            action_runtime.hotel_config.hotel_name,
            action_runtime.hotel_config.telegram_channel_id,
        )

    # Build the pipeline list. In text-only mode the mic-side processors
    # (audio level monitor, VAD, STT) are skipped entirely.
    processors: list = [transport.input()]
    # --- Diagnostic probes ---
    _probing = True
    if _probing:
        processors.append(PipelineProbe("after_transport_in"))
    if mic_enabled:
        if settings.rnnoise_enabled:
            if _RNNoise is None:
                logger.warning(
                    "[rnnoise] enabled but pyrnnoise is unavailable; running without denoiser"
                )
            else:
                processors.append(RNNoiseDenoiser(sample_rate=16000))
                if _probing:
                    processors.append(PipelineProbe("after_rnnoise"))
                logger.info("[rnnoise] enabled")

        processors.extend(
            [
                PlaybackLeakageGuard(allow_interruptions=settings.allow_interruptions),
            ]
        )
        if _probing:
            processors.append(PipelineProbe("after_leakage_guard"))
        processors.append(AudioLevelMonitor())
        if _probing:
            processors.append(PipelineProbe("after_audio_monitor"))
        if vad_processor:
            processors.append(vad_processor)
            if _probing:
                processors.append(PipelineProbe("after_vad"))
            if health_monitor is not None:
                processors.append(ActivityNotifier(health_monitor))
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
            processors.append(
                BotActiveUserFrameSuppressor(allow_interruptions=settings.allow_interruptions)
            )
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
            processors.extend(
                [
                    BotActiveUserFrameSuppressor(allow_interruptions=settings.allow_interruptions),
                ]
            )
            if _probing:
                processors.append(PipelineProbe("after_suppressor"))
        if settings.transport_mode == "daily":
            processors.append(UserTranscriptBroadcaster())
    processors.append(context_aggregator.user())
    if _probing:
        processors.append(PipelineProbe("after_ctx_user"))
    processors.append(LLMRunGuard())
    if _probing:
        processors.append(PipelineProbe("after_llm_guard"))
    if settings.transport_mode == "daily":
        processors.append(BrowserTextInputController())

    # RAG: optionally inject hotel knowledge before the LLM sees the context.
    if settings.rag_enabled:
        # Warm up the embedding model now so the first query doesn't
        # cold-start inside the 500ms retrieval timeout.
        from voxtera.rag.embeddings import embed_sync
        from voxtera.rag.injector import RAGContextInjector
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        embed_sync(["warmup"])

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = ChunksStore(db_path)
        store.init_schema()
        retriever = Retriever(store)
        rag_injector = RAGContextInjector(retriever, hotel_id=settings.hotel_id)
        processors.append(rag_injector)
        if _probing:
            processors.append(PipelineProbe("after_rag"))
        logger.info("[rag] enabled for hotel_id={!r}", settings.hotel_id)

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
        if tts_router is not None:
            # Automatically update Google TTS language when STT detects the
            # guest has switched language. Chirp 3 HD is multilingual so the
            # same voice character works across locales.
            processors.append(AutoTTSLanguageSwitcher(tts_router=tts_router))
        # Greeting controller: defers the startup greeting until the browser
        # sends {type: 'voxtera-ready'} after the transcript page is visible,
        # so the bot never speaks into an empty room.
        greeting_lang, greeting_text = resolve_greeting(settings.greeting_language)
        logger.info(
            "[greeting] language={} (preference={}) — awaiting voxtera-ready from browser",
            greeting_lang,
            settings.greeting_language,
        )
        processors.append(GreetingController(greeting_text=greeting_text))
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
    processors.extend(
        [
            transport.output(),
        ]
    )
    if _probing:
        processors.append(PipelineProbe("after_transport_out"))
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
        ),
        enable_rtvi=False,
        cancel_on_idle_timeout=settings.pipeline_idle_timeout_secs is not None,
        idle_timeout_secs=settings.pipeline_idle_timeout_secs,
    )

    runner = PipelineRunner(handle_sigint=True)
    return task, runner
