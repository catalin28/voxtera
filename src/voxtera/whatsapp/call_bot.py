"""WhatsApp voice-call bot — answers an inbound WhatsApp call as the travel agent.

When a WhatsApp user taps "call" on the business number, Meta sends a `calls`
connect webhook with an SDP offer. Pipecat's ``WhatsAppClient`` terminates the
WebRTC media (generates the SDP answer, pre-accepts + accepts the call) and hands
us a live ``SmallWebRTCConnection``. ``run_call_bot`` wraps that connection in a
``SmallWebRTCTransport`` and runs a Pipecat voice pipeline:

    transport.in → [raw-rec] → VAD → STT → [transcript] → context.user
        → TravelAgentBrain → [tracer] → TTS → transport.out → [call-rec] → context.assistant

The answering brain is the SAME ``TravelAgentBrain`` used by the web voice orb and
WhatsApp text — it forwards each turn to the shared ``/api/concierge`` endpoint, so
voice calls, web voice, and chat all behave identically (one source of truth).

Observability (WAV + transcript + trace), mirroring the hotel pipeline:
  * ``RawInputRecorder`` → ``logs/calls/<session_id>/input_raw.wav`` (caller audio)
  * ``CallAudioRecorder`` → ``logs/calls/<session_id>/call.wav`` (full stereo call)
  * ``TranscriptStageTimer`` (user turns) + ``PipelineTracer`` (bot turns) →
    ``logs/calls/<session_id>/record.json`` (transcript + per-turn timings)
  * ``TraceForwarder`` ships trace events to serve.py so the call shows up in the
    Voxtera Trace dashboard, tagged channel ``wa``.

CONCURRENCY CAVEAT: the recording/trace layer uses process-global singletons
(``call_record``, env vars) designed for "one call = one subprocess". This service
is "one process = many calls", so recording/trace is correct for ONE call at a
time (matches the box's 1–2 call ceiling and how the demo is used). Truly
simultaneous calls would mix recordings — that needs a per-call refactor. The call
audio itself is per-pipeline and unaffected; only the recording/trace artifacts
are shared. All observability is best-effort: a failure here never breaks the call.

This is a self-contained pipeline (it does NOT go through ``build_pipeline``) to
keep the WhatsApp call path fully isolated from the tuned hotel/Daily pipeline.
The WebRTC media path can only be exercised on a publicly reachable host
(ICE/STUN need a real public IP); full call testing happens on the droplet.
"""

from __future__ import annotations

import dataclasses
import os
import uuid

from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)

from voxtera.config import Settings, load_settings
from voxtera.stt import _build_stt
from voxtera.travel_agent_brain import TravelAgentBrain
from voxtera.tts import _TTS_BUILDERS
from voxtera.whatsapp.config import load_whatsapp_settings

# Audio rates: 16 kHz in (Silero VAD + STT expectation); 24 kHz out matches the
# TTS providers' native rate — SmallWebRTC resamples to the WebRTC wire rate.
_AUDIO_IN_RATE = 16000
_AUDIO_OUT_RATE = 24000

# Trace label/source + channel so WhatsApp calls are distinguishable in the
# Voxtera Trace dashboard (sessions tagged "wa", events sourced "whatsapp").
_TRACE_LABEL = "whatsapp"
_TRACE_CHANNEL = "wa"
# serve.py (the UI/dashboard) runs on the same droplet on :8080.
_LAUNCHER_URL = os.environ.get("VOXTERA_LAUNCHER_URL", "http://127.0.0.1:8080/api/bot-event")


def _call_settings() -> Settings:
    """Settings forced into voice + travel-agent mode for a WhatsApp call."""
    settings = load_settings()
    return dataclasses.replace(
        settings,
        input_mode="voice",  # a call always has mic audio
        bot_brain="travel_agent",  # answer via ConciergePipeline
        rag_enabled=False,  # travel brain uses /api/concierge, not local RAG
    )


def _smart_turn_stop_secs(settings: Settings) -> float:
    """SmartTurn hard end-of-turn cap for calls. Higher = more patient with long
    thinking pauses (e.g. "scuba diving … and a spa"), at the cost of a longer
    wait when the caller has genuinely finished. Tune via WHATSAPP_SMART_TURN_STOP_SECS.
    """
    raw = os.environ.get("WHATSAPP_SMART_TURN_STOP_SECS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning("[whatsapp-call] bad WHATSAPP_SMART_TURN_STOP_SECS={!r}", raw)
    return settings.smart_turn_stop_secs


def _build_transport(connection: SmallWebRTCConnection) -> SmallWebRTCTransport:
    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=_AUDIO_IN_RATE,
            audio_in_channels=1,
            audio_in_passthrough=True,
            audio_out_enabled=True,
            audio_out_sample_rate=_AUDIO_OUT_RATE,
            audio_out_channels=1,
        ),
    )


def _init_call_record(settings: Settings, session_id: str) -> None:
    """Start the per-call WAV + transcript record (best-effort).

    Sets the process-global session id/channel the recorders + tracer read.
    """
    os.environ["VOXTERA_SESSION_ID"] = session_id
    os.environ["VOXTERA_CHANNEL"] = _TRACE_CHANNEL
    from voxtera import call_record

    call_record.init_call(
        enabled=True,
        hotel_id=None,
        bot_name=settings.bot_name,
        transport_mode="whatsapp",
        stt_provider=settings.stt_provider,
        tts_provider=settings.tts_provider,
        llm_model=os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001"),
    )


async def _finalize_call_record() -> None:
    """Flush WAVs + write the transcript record on hang-up (best-effort)."""
    from voxtera import call_record

    for flush in (
        call_record.flush_audio,
        call_record.flush_raw_input,
        call_record.flush_stage_recorders,
    ):
        try:
            await flush()
        except Exception as e:  # noqa: BLE001
            logger.debug("[whatsapp-call] {} failed: {}", getattr(flush, "__name__", flush), e)
    try:
        call_record.finalize()
    except Exception as e:  # noqa: BLE001
        logger.debug("[whatsapp-call] call_record.finalize failed: {}", e)


async def run_call_bot(connection: SmallWebRTCConnection) -> None:
    """Run a Pipecat voice pipeline for one WhatsApp call. Returns on hang-up.

    Invoked as the ``connection_callback`` of ``WhatsAppClient.handle_webhook_request``.
    """
    settings = _call_settings()
    # Filesystem-safe id (no colon) — used as the logs/calls/<id>/ folder name.
    session_id = f"wacall_{uuid.uuid4().hex[:12]}"
    logger.info("[whatsapp-call] starting bot session={}", session_id)

    # Observability: WAV + transcript record. Best-effort — never break the call.
    record_enabled = True
    try:
        _init_call_record(settings, session_id)
    except Exception as e:  # noqa: BLE001
        record_enabled = False
        logger.error("[whatsapp-call] call_record init failed (recording off): {}", e)

    transport = _build_transport(connection)

    # STT (single provider, as in local mode). needs_vad is False for Deepgram
    # (built-in VAD); otherwise we add a Silero VAD processor.
    stt, needs_vad = _build_stt(settings)

    # TTS (single configured provider).
    tts_builder = _TTS_BUILDERS.get(settings.tts_provider)
    if tts_builder is None:
        raise RuntimeError(
            f"Unknown TTS_PROVIDER={settings.tts_provider!r}. "
            f"Use one of: {', '.join(_TTS_BUILDERS)}."
        )
    tts = tts_builder(settings)
    if tts is None:
        raise RuntimeError(f"TTS_PROVIDER={settings.tts_provider!r} is missing credentials.")

    # Conversation context + turn-taking. TravelAgentBrain reads the latest user
    # utterance off the LLMContext the aggregator emits, so the context starts empty.
    context = LLMContext([])
    smart_turn = LocalSmartTurnAnalyzerV3(
        cpu_count=settings.smart_turn_cpu_count,
        params=SmartTurnParams(stop_secs=_smart_turn_stop_secs(settings)),
    )
    user_turn_strategies = UserTurnStrategies(
        start=default_user_turn_start_strategies(),
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=smart_turn)],
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=user_turn_strategies),
    )

    # Region comes from the WhatsApp channel config (WHATSAPP_DEFAULT_REGION);
    # empty → None → the concierge asks the caller which region on the first turn.
    wa_region = load_whatsapp_settings().default_region
    brain = TravelAgentBrain(region=wa_region or None, session_id=session_id)

    # Optional observability processors (WAV + transcript). Imported lazily and
    # guarded so a recording problem can never stop a call from connecting.
    raw_recorder = None
    transcript_timer = None
    tracer = None
    call_recorder = None
    if record_enabled:
        try:
            from voxtera.call_record import CallAudioRecorder, RawInputRecorder
            from voxtera.observability import PipelineTracer, TranscriptStageTimer

            raw_recorder = RawInputRecorder(sample_rate=_AUDIO_IN_RATE)
            transcript_timer = TranscriptStageTimer(label=_TRACE_LABEL)
            tracer = PipelineTracer(label=_TRACE_LABEL)
            call_recorder = CallAudioRecorder()
        except Exception as e:  # noqa: BLE001
            logger.error("[whatsapp-call] observability processors unavailable: {}", e)

    # Assemble the pipeline in the same order as build_pipeline: raw recorder
    # early, transcript timer between STT and the user aggregator, tracer after
    # the brain, call recorder after transport.output().
    processors: list = [transport.input()]
    if raw_recorder is not None:
        processors.append(raw_recorder)
    if needs_vad:
        processors.append(
            VADProcessor(
                vad_analyzer=SileroVADAnalyzer(
                    sample_rate=_AUDIO_IN_RATE,
                    params=VADParams(
                        stop_secs=settings.vad_stop_secs,
                        start_secs=settings.vad_start_secs,
                        min_volume=settings.vad_min_volume,
                        confidence=settings.vad_confidence,
                    ),
                )
            )
        )
    processors.append(stt)
    if transcript_timer is not None:
        processors.append(transcript_timer)
    processors.append(context_aggregator.user())
    processors.append(brain)
    if tracer is not None:
        processors.append(tracer)
    processors.append(tts)
    processors.append(transport.output())
    if call_recorder is not None:
        processors.append(call_recorder)
    processors.append(context_aggregator.assistant())

    task = PipelineTask(
        Pipeline(processors),
        params=PipelineParams(
            allow_interruptions=settings.allow_interruptions,
            audio_in_sample_rate=_AUDIO_IN_RATE,
            audio_out_sample_rate=_AUDIO_OUT_RATE,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # End the pipeline when the caller hangs up (connection closes).
        cancel_on_idle_timeout=settings.pipeline_idle_timeout_secs is not None,
        idle_timeout_secs=settings.pipeline_idle_timeout_secs,
    )

    # Ship trace events to serve.py so the call appears in the dashboard (tagged
    # channel "wa"). Best-effort — a forwarder failure must not break the call.
    trace_forwarder = None
    if record_enabled:
        try:
            from voxtera.trace import TraceForwarder

            trace_forwarder = TraceForwarder(launcher_url=_LAUNCHER_URL, session_id=session_id)
            await trace_forwarder.start()
        except Exception as e:  # noqa: BLE001
            logger.error("[whatsapp-call] trace forwarder start failed: {}", e)
            trace_forwarder = None

    # Greet the caller the moment the WebRTC media connects, so they hear the
    # agent immediately instead of dead air. Queued on connect (not at build)
    # because the audio track isn't live until the peer connection is up.
    greeting_text = (
        "Hello! You've reached Voxtera, your travel concierge. "
        "How can I help you plan your trip today?"
    )

    @transport.event_handler("on_client_connected")
    async def _on_client_connected(_transport, _client) -> None:  # noqa: ANN001
        logger.info("[whatsapp-call] client connected (session={})", session_id)
        # Activate the lazy Gladia STT session. The _LazyConnectGladiaSTTService
        # skips its own connect and waits for an explicit lazy_connect() (normally
        # the STTRouter's job in the Daily pipeline). Without a router here, we
        # trigger it ourselves or no transcripts are ever produced. Safe no-op for
        # STT providers that don't have the method (e.g. Deepgram).
        lazy_connect = getattr(stt, "lazy_connect", None)
        if lazy_connect is not None:
            try:
                await lazy_connect()
                logger.info("[whatsapp-call] STT lazy_connect triggered")
            except Exception as e:  # noqa: BLE001
                logger.error("[whatsapp-call] STT lazy_connect failed: {}", e)
        await task.queue_frames([TTSSpeakFrame(text=greeting_text)])

    # handle_sigint=False: the bot runs inside the async webhook service, not as a
    # dedicated process, so it must not install signal handlers.
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        if trace_forwarder is not None:
            try:
                await trace_forwarder.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("[whatsapp-call] trace forwarder stop failed: {}", e)
        if record_enabled:
            await _finalize_call_record()
        logger.info("[whatsapp-call] bot session={} ended", session_id)
