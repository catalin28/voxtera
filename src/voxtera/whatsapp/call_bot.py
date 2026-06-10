"""WhatsApp voice-call bot — answers an inbound WhatsApp call as the travel agent.

When a WhatsApp user taps "call" on the business number, Meta sends a `calls`
connect webhook with an SDP offer. Pipecat's ``WhatsAppClient`` terminates the
WebRTC media (generates the SDP answer, pre-accepts + accepts the call) and hands
us a live ``SmallWebRTCConnection``. ``run_call_bot`` wraps that connection in a
``SmallWebRTCTransport`` and runs a Pipecat voice pipeline:

    transport.in → [raw-rec] → leakage-guard → VAD → STT → suppressor
        → [transcript] → context.user → TravelAgentBrain → [tracer] → TTS
        → transport.out → [call-rec] → context.assistant

Echo / ghost-turn protection (same processors as the main Daily pipeline):
  * ``PlaybackLeakageGuard`` zeroes mic audio while the bot is speaking or an
    LLM response is in flight, so TTS playback leaking into the caller's mic
    (the WhatsApp leg has no acoustic echo cancellation) never reaches VAD/STT.
  * ``BotActiveUserFrameSuppressor`` drops residual VAD + interim/final
    transcription frames that still arrive while the bot is active — e.g. a
    late Gladia final from the tail of the caller's question, which would
    otherwise start a ghost user turn (and, with barge-in enabled, cut the
    bot off after a word or two).
  Both honour the same WHATSAPP_ALLOW_INTERRUPTIONS flag as the pipeline's
  ``allow_interruptions``: in strict mode (default) they suppress everything
  while the bot is active; with barge-in enabled the guard's RMS gate lets
  genuine near-field speech through.

Barge-in (WHATSAPP_ALLOW_INTERRUPTIONS=true): the caller can talk over the
bot to cut it off. In this mode user turns start on VAD ONLY (not on interim
transcriptions, which would resurrect the late-Gladia-interim cutoff), and
the leakage guard switches from "always silence" to its adaptive RMS gate so
real speech opens the mic while playback echo stays suppressed.

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

CONCURRENCY: this service is "one process = many calls". Each call gets its own
``CallContext`` (created + activated at the top of ``run_call_bot``) owning the
call record, the trace turn tracker, and the recorder references, so truly
simultaneous calls keep separate WAVs, transcripts, and trace session ids. All
observability is best-effort: a failure here never breaks the call.

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
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)

from voxtera import call_context as call_context_mod
from voxtera.audio import BotActiveUserFrameSuppressor, PlaybackLeakageGuard
from voxtera.call_context import CallContext
from voxtera.config import Settings, load_settings
from voxtera.stt import _build_stt
from voxtera.travel_agent_brain import TravelAgentBrain
from voxtera.tts import _TTS_BUILDERS
from voxtera.whatsapp.config import load_whatsapp_settings, property_hotel_id

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


def _greeting_text(hotel_id: str | None) -> str:
    """Channel greeting — overridable, with per-mode defaults."""
    explicit = os.environ.get("WHATSAPP_GREETING_TEXT", "").strip()
    if explicit:
        return explicit
    if hotel_id:
        hotel_name = hotel_id
        try:
            from voxtera.actions import load_hotel_config

            hotel_name = load_hotel_config(hotel_id).hotel_name
        except Exception:  # noqa: BLE001 — greeting must never block a call
            pass
        return (
            f"Hello! You've reached the concierge at {hotel_name}. How can I help you today?"
        )
    return (
        "Hello! You've reached Voxtera, your travel concierge. "
        "How can I help you plan your trip today?"
    )


def _allow_interruptions() -> bool:
    """Whether the caller can barge in and cut off the bot mid-sentence.

    Default OFF. With WHATSAPP_ALLOW_INTERRUPTIONS=true, barge-in is enabled
    and protected against self-interruption by three pieces working together:
    PlaybackLeakageGuard's RMS gate (echo stays silenced, genuine near-field
    speech opens the mic), VAD-only user-turn-start strategies (late STT
    interims can't start a turn), and the suppressor passing frames through
    untouched. Strict mode (default) remains the safest choice for noisy
    callers/speakerphone.
    """
    return os.environ.get("WHATSAPP_ALLOW_INTERRUPTIONS", "").strip().lower() in (
        "1",
        "true",
        "yes",
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


def _init_call_record(settings: Settings, context: CallContext) -> None:
    """Start the per-call WAV + transcript record (best-effort).

    All state lives on ``context`` — nothing process-global is touched, so
    concurrent calls each get their own record/WAVs/trace session.
    """
    from voxtera import call_record

    call_record.init_call(
        enabled=True,
        hotel_id=None,
        bot_name=settings.bot_name,
        transport_mode="whatsapp",
        stt_provider=settings.stt_provider,
        tts_provider=settings.tts_provider,
        llm_model=os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001"),
        context=context,
    )


async def _finalize_call_record(context: CallContext) -> None:
    """Flush WAVs + write the transcript record on hang-up (best-effort)."""
    from voxtera import call_record

    for flush in (
        call_record.flush_audio,
        call_record.flush_raw_input,
        call_record.flush_stage_recorders,
    ):
        try:
            await flush(context)
        except Exception as e:  # noqa: BLE001
            logger.debug("[whatsapp-call] {} failed: {}", getattr(flush, "__name__", flush), e)
    try:
        call_record.finalize(context)
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

    # Per-call context: owns the call record, trace turn tracker, and recorder
    # references for THIS call only. Activated on the contextvar so every task
    # this coroutine spawns (the whole pipeline) resolves to it — concurrent
    # calls in this one process can no longer mix WAVs/transcripts/trace ids.
    call_ctx = call_context_mod.new_call_context(session_id=session_id, channel=_TRACE_CHANNEL)
    call_context_mod.activate(call_ctx)

    # Observability: WAV + transcript record. Best-effort — never break the call.
    record_enabled = True
    try:
        _init_call_record(settings, call_ctx)
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
    allow_interruptions = _allow_interruptions()
    context = LLMContext([])
    smart_turn = LocalSmartTurnAnalyzerV3(
        cpu_count=settings.smart_turn_cpu_count,
        params=SmartTurnParams(stop_secs=_smart_turn_stop_secs(settings)),
    )
    if allow_interruptions:
        # Barge-in mode: start a user turn on VAD ONLY. The default strategy
        # list also includes TranscriptionUserTurnStartStrategy, which fires on
        # ANY interim transcription while the bot speaks — including late
        # Gladia interims from the tail of the previous question — and that
        # cut the bot off after a word or two. VAD is safe here because the
        # PlaybackLeakageGuard's RMS gate only lets genuine near-field speech
        # through while the bot is active, so VAD cannot fire on echo.
        start_strategies: list = [VADUserTurnStartStrategy()]
    else:
        start_strategies = default_user_turn_start_strategies()
    user_turn_strategies = UserTurnStrategies(
        start=start_strategies,
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=smart_turn)],
    )
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=user_turn_strategies),
    )

    # Demo switch (P1.4): hotel scope set (WHATSAPP_HOTEL_ID/CONCIERGE_HOTEL_ID)
    # → answer as that property's concierge from its own guide; unset → travel
    # agent. Region comes from the WhatsApp channel config
    # (WHATSAPP_DEFAULT_REGION); empty → None → the concierge asks the caller
    # which region on the first turn.
    hotel_scope = property_hotel_id()
    wa_region = load_whatsapp_settings().default_region
    brain = TravelAgentBrain(
        region=wa_region or None, session_id=session_id, hotel_id=hotel_scope
    )

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

            raw_recorder = RawInputRecorder(sample_rate=_AUDIO_IN_RATE, context=call_ctx)
            transcript_timer = TranscriptStageTimer(label=_TRACE_LABEL, context=call_ctx)
            tracer = PipelineTracer(label=_TRACE_LABEL, context=call_ctx)
            call_recorder = CallAudioRecorder(context=call_ctx)
        except Exception as e:  # noqa: BLE001
            logger.error("[whatsapp-call] observability processors unavailable: {}", e)

    # Assemble the pipeline in the same order as build_pipeline: raw recorder
    # early, leakage guard before VAD (zeroes mic audio while the bot is
    # active), suppressor after STT (drops late VAD/transcription frames),
    # transcript timer between STT and the user aggregator, tracer after the
    # brain, call recorder after transport.output().
    processors: list = [transport.input()]
    if raw_recorder is not None:
        processors.append(raw_recorder)
    # Echo guard: the WhatsApp/WebRTC leg has no acoustic echo cancellation, so
    # TTS playback can leak back through the caller's mic. Zero the mic audio
    # while the bot is speaking/thinking so the leak never reaches VAD or STT.
    processors.append(PlaybackLeakageGuard(allow_interruptions=allow_interruptions))
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
    # Ghost-turn guard: drop residual VAD + interim/final transcriptions that
    # arrive while the bot is active (e.g. a late Gladia final from the tail
    # of the caller's question). Without this they start a new user turn and —
    # with barge-in enabled — interrupt the bot after a word or two.
    processors.append(BotActiveUserFrameSuppressor(allow_interruptions=allow_interruptions))
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
            # Off by default — the WhatsApp leg has no echo cancellation, so the
            # bot's own voice (heard through the caller's mic) would otherwise
            # trigger a false barge-in and cut the bot off after one word.
            # Same flag drives PlaybackLeakageGuard + BotActiveUserFrameSuppressor.
            allow_interruptions=allow_interruptions,
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
    greeting_text = _greeting_text(hotel_scope)

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
            await _finalize_call_record(call_ctx)
        call_context_mod.deactivate()
        logger.info("[whatsapp-call] bot session={} ended", session_id)
