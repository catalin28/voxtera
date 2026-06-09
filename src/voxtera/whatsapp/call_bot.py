"""WhatsApp voice-call bot — answers an inbound WhatsApp call as the travel agent.

When a WhatsApp user taps "call" on the business number, Meta sends a `calls`
connect webhook with an SDP offer. Pipecat's ``WhatsAppClient`` terminates the
WebRTC media (generates the SDP answer, pre-accepts + accepts the call) and hands
us a live ``SmallWebRTCConnection``. ``run_call_bot`` wraps that connection in a
``SmallWebRTCTransport`` and runs a Pipecat voice pipeline:

    transport.in → VAD → STT → context.user → TravelAgentBrain → TTS → transport.out

The answering brain is the SAME ``TravelAgentBrain`` used by the web voice orb and
WhatsApp text — it forwards each turn to the shared ``/api/concierge`` endpoint, so
voice calls, web voice, and chat all behave identically (one source of truth).

This is a self-contained pipeline (it does NOT go through ``build_pipeline``) to
keep the WhatsApp call path fully isolated from the tuned hotel/Daily pipeline —
a bug here can never destabilise the production voice paths.

Note: the WebRTC media path can only be exercised on a publicly reachable host
(ICE/STUN need a real public IP); import/compile is checkable in dev, full call
testing happens on the deployed droplet.
"""

from __future__ import annotations

import dataclasses
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


def _call_settings() -> Settings:
    """Settings forced into voice + travel-agent mode for a WhatsApp call."""
    settings = load_settings()
    return dataclasses.replace(
        settings,
        input_mode="voice",  # a call always has mic audio
        bot_brain="travel_agent",  # answer via ConciergePipeline
        rag_enabled=False,  # travel brain uses /api/concierge, not local RAG
    )


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


async def run_call_bot(connection: SmallWebRTCConnection) -> None:
    """Run a Pipecat voice pipeline for one WhatsApp call. Returns on hang-up.

    Invoked as the ``connection_callback`` of ``WhatsAppClient.handle_webhook_request``.
    Each concurrent call gets its own pipeline + STT/TTS streams.
    """
    settings = _call_settings()
    session_id = f"wacall:{uuid.uuid4().hex[:12]}"
    logger.info("[whatsapp-call] starting bot session={}", session_id)

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
        params=SmartTurnParams(stop_secs=settings.smart_turn_stop_secs),
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

    processors: list = [transport.input()]
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
    processors.extend(
        [
            stt,
            context_aggregator.user(),
            brain,
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

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

    # Greet the caller the moment the WebRTC media connects, so they hear the
    # agent immediately instead of dead air. Queued on connect (not at build)
    # because the audio track isn't live until the peer connection is up.
    greeting_text = (
        "Hello! You've reached Voxtera, your travel concierge. "
        "How can I help you plan your trip today?"
    )

    @transport.event_handler("on_client_connected")
    async def _on_client_connected(_transport, _client) -> None:  # noqa: ANN001
        logger.info("[whatsapp-call] client connected — greeting (session={})", session_id)
        await task.queue_frames([TTSSpeakFrame(text=greeting_text)])

    # handle_sigint=False: the bot runs inside the async webhook service, not as a
    # dedicated process, so it must not install signal handlers.
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        logger.info("[whatsapp-call] bot session={} ended", session_id)
