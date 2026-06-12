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

import asyncio
import dataclasses
import os
import uuid
from pathlib import Path

import aiohttp
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame, TTSSpeakFrame, TextFrame, TranscriptionFrame
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
from voxtera.whatsapp.config import (
    WhatsAppSettings,
    handshake_caption,
    handshake_image_path,
    load_whatsapp_settings,
    property_hotel_id,
)

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


async def _deliver_offer(
    image_id: str, *, caller_wa_id: str | None, settings: WhatsAppSettings
) -> None:
    """Upload (once) and send the image or tour link to the caller's chat."""
    from voxtera.whatsapp.client import WhatsAppClient
    from voxtera.whatsapp.image_catalog import get_tour_url, resolve_media_id

    if not caller_wa_id:
        return
    try:
        tour_url = get_tour_url(image_id)
        async with aiohttp.ClientSession() as http:
            client = WhatsAppClient(settings=settings, session=http)
            if tour_url:
                await client.send_text(to=caller_wa_id, body=tour_url)
                logger.info("[voice-offer] tour link sent: {} → {}", image_id, caller_wa_id)
            else:
                media_id = await resolve_media_id(image_id, settings=settings)
                if media_id:
                    await client.send_image(to=caller_wa_id, media_id=media_id)
                    logger.info("[voice-offer] image sent: {} → {}", image_id, caller_wa_id)
                else:
                    logger.warning("[voice-offer] no media_id for {}", image_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[voice-offer] delivery failed for {}: {}", image_id, e)


class VoiceAffirmativeDetector(FrameProcessor):
    """Deliver the pending photo offer when the caller says "yes".

    MUST sit BEFORE ``context_aggregator.user()``: in Pipecat 1.x the user
    aggregator CONSUMES ``TranscriptionFrame`` and does not push it downstream,
    so a processor placed after the brain never sees caller speech (which is
    why detection inside ``VoiceOfferProcessor`` was dead code).

    On a final ``TranscriptionFrame`` that is a clear multilingual affirmative
    while an offer is pending for this caller:
      * fire the WhatsApp send (async, never blocks the voice turn),
      * speak a short acknowledgement via ``TTSSpeakFrame``,
      * SWALLOW the transcription so the bare "yes" never becomes a concierge
        turn — mirroring webhook.py's "skip the concierge entirely" behavior
        on the text channel.
    Anything else passes through untouched.
    """

    def __init__(self, *, caller_wa_id: str | None, settings: WhatsAppSettings) -> None:
        super().__init__()
        self._caller_wa_id = caller_wa_id
        self._settings = settings

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TranscriptionFrame)
            and self._caller_wa_id
        ):
            from voxtera.whatsapp.image_catalog import is_affirmative, pop_pending_offer

            if is_affirmative(frame.text):
                pending_id = pop_pending_offer(self._caller_wa_id)
                if pending_id:
                    logger.info(
                        "[voice-offer] affirmative '{}' → delivering {}",
                        frame.text.strip(),
                        pending_id,
                    )
                    asyncio.create_task(
                        _deliver_offer(
                            pending_id,
                            caller_wa_id=self._caller_wa_id,
                            settings=self._settings,
                        )
                    )
                    # Confirm out loud, then swallow the "yes" — no concierge turn.
                    ack = os.environ.get(
                        "WHATSAPP_OFFER_ACK_TEXT",
                        "I've just sent it to your WhatsApp chat — take a look!",
                    )
                    await self.push_frame(TTSSpeakFrame(ack), FrameDirection.DOWNSTREAM)
                    return

        await self.push_frame(frame, direction)


class VoiceOfferProcessor(FrameProcessor):
    """Strip [OFFER:<id>] tags from LLM output bound for TTS and remember the
    pending offer; delivery on "yes" happens in ``VoiceAffirmativeDetector``.

    Placed between brain and TTS in the pipeline.

    Brain emits ``LLMFullResponseStartFrame`` → N×``LLMTextFrame`` → ``LLMFullResponseEndFrame``.
    The [OFFER:<id>] tag can be split across chunk boundaries, so we buffer all
    ``LLMTextFrame`` chunks and process the joined text at ``LLMFullResponseEndFrame``,
    then re-emit one clean ``LLMTextFrame`` before the end frame.
    """

    def __init__(self, *, caller_wa_id: str | None, settings: WhatsAppSettings) -> None:
        super().__init__()
        self._caller_wa_id = caller_wa_id
        self._settings = settings
        self._buffer: list[str] = []   # accumulates LLMTextFrame chunks per turn

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM:
            from pipecat.frames.frames import (
                LLMFullResponseEndFrame,
                LLMFullResponseStartFrame,
                LLMTextFrame,
            )

            if isinstance(frame, LLMFullResponseStartFrame):
                # New LLM turn — reset buffer and pass the start frame through.
                self._buffer = []
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, LLMTextFrame):
                # Buffer the chunk; suppress from downstream until turn is complete.
                self._buffer.append(frame.text)
                return  # do NOT push yet

            if isinstance(frame, LLMFullResponseEndFrame):
                # Join all chunks, strip [OFFER:<id>], emit clean text + end frame.
                full_text = "".join(self._buffer)
                self._buffer = []

                from voxtera.whatsapp.image_catalog import (
                    clear_pending_offer,
                    extract_offer_tag,
                    set_pending_offer,
                )

                clean, offered_id = extract_offer_tag(full_text)
                if offered_id and self._caller_wa_id:
                    set_pending_offer(self._caller_wa_id, offered_id)
                    logger.info(
                        "[voice-offer] offer stored: {} → {}", self._caller_wa_id, offered_id
                    )
                elif clean.strip() and self._caller_wa_id:
                    clear_pending_offer(self._caller_wa_id)

                if clean.strip():
                    await self.push_frame(LLMTextFrame(text=clean), direction)
                await self.push_frame(frame, direction)  # LLMFullResponseEndFrame
                return

        await self.push_frame(frame, direction)


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
        return f"Hello! You've reached the concierge at {hotel_name}. How can I help you today?"
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


def _ambience_mixer(hotel_scope: str | None):
    """Optional lobby room-tone mixer for the call's output audio.

    Mixes a faint, seamless room tone under (and between) the bot's speech,
    so silences feel like a real place instead of a dead line. MODE-AWARE
    default: ON for the hotel concierge (it answers from a lobby — the
    ambience itself masks the short thinking gaps), OFF for the travel
    agent (which uses spoken fillers instead). LOBBY_AMBIENCE_ENABLED
    overrides either way.

    Volume via LOBBY_AMBIENCE_VOLUME (default 0.03 — 'like a wind': felt
    more than heard. The WhatsApp leg has no echo cancellation, so loud ambience
    would feed the caller's mic). File via LOBBY_AMBIENCE_FILE (mono 16-bit
    WAV at the output rate; see scripts/generate_ambience.py).
    """
    override = os.environ.get("LOBBY_AMBIENCE_ENABLED", "").strip().lower()
    enabled = override in ("1", "true", "yes") if override else bool(hotel_scope)
    if not enabled:
        return None
    # Default track: prefer the jazz loop when present (the lobby with a
    # piano bar), fall back to the synthetic room tone. LOBBY_AMBIENCE_FILE
    # overrides both.
    audio_dir = Path(__file__).resolve().parents[3] / "assets" / "audio"
    jazz, tone = audio_dir / "lobby_jazz.wav", audio_dir / "lobby_tone.wav"
    default_file = jazz if jazz.exists() else tone
    sound_file = os.environ.get("LOBBY_AMBIENCE_FILE", "").strip() or str(default_file)
    try:
        volume = float(os.environ.get("LOBBY_AMBIENCE_VOLUME", "0.03"))
    except ValueError:
        volume = 0.03
    try:
        from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

        mixer = SoundfileMixer(
            sound_files={"lobby": sound_file},
            default_sound="lobby",
            volume=volume,
            loop=True,
        )
        logger.info("[whatsapp-call] lobby ambience on (volume={}, file={})", volume, sound_file)
        return mixer
    except Exception as e:  # noqa: BLE001 — ambience must never block a call
        logger.warning("[whatsapp-call] ambience unavailable: {}", e)
        return None


def _build_filler_player(hotel_scope: str | None):
    """Optional FillerPlayer for this call (FILLERS_ENABLED, default on).

    Clips live in FILLER_DIR (default assets/fillers/<lang>/*.wav, rendered
    with the bot's own voice by scripts/generate_fillers.py). Returns None
    when disabled or no clips are loadable — the call runs without fillers.

    Settings come from ``assets/fillers/fillers.json`` (the admin "Voice
    Fillers" page), read PER CALL — edits apply to the next call without a
    restart. Per concierge mode (hotel/travel): enabled, delay, and which
    clips are active. FILLERS_ENABLED / FILLER_DELAY_SECS env vars override
    the file (emergency switches).
    """
    try:
        from voxtera.fillers import (
            DEFAULT_FILLER_DIR,
            FillerPlayer,
            load_filler_clips,
            load_filler_settings,
        )

        clips_dir = os.environ.get("FILLER_DIR", "").strip() or DEFAULT_FILLER_DIR
        mode = "hotel" if hotel_scope else "travel"
        settings = load_filler_settings(mode, clips_dir)

        override = os.environ.get("FILLERS_ENABLED", "").strip().lower()
        enabled = override not in ("0", "false", "no") if override else settings["enabled"]
        if not enabled:
            return None
        try:
            delay = float(os.environ.get("FILLER_DELAY_SECS", "") or settings["delay_secs"])
        except ValueError:
            delay = settings["delay_secs"]

        clips = load_filler_clips(clips_dir, sample_rate=_AUDIO_OUT_RATE, only=settings["clips"])
        if not clips:
            logger.warning("[whatsapp-call] no filler clips selected/loadable — fillers off")
            return None
        logger.info(
            "[whatsapp-call] fillers on (mode={}, delay={}s, clips={})",
            mode,
            delay,
            {k: len(v) for k, v in clips.items()},
        )
        return FillerPlayer(clips, sample_rate=_AUDIO_OUT_RATE, delay_secs=delay)
    except Exception as e:  # noqa: BLE001 — fillers must never block a call
        logger.warning("[whatsapp-call] fillers unavailable: {}", e)
        return None


def _build_transport(
    connection: SmallWebRTCConnection, hotel_scope: str | None
) -> SmallWebRTCTransport:
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
            audio_out_mixer=_ambience_mixer(hotel_scope),
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


# Process-level cache for the handshake media_id so we upload the image once
# and reuse the id for every subsequent call. Meta keeps uploaded media for 30
# days; a server restart re-uploads (acceptable for demo/prod scale).
_HANDSHAKE_MEDIA_ID: str | None = None


async def _ensure_handshake_media_id(settings: WhatsAppSettings) -> str | None:
    """Upload the visual-handshake image if not already cached; return its id.

    Returns None when the feature is disabled (WHATSAPP_HANDSHAKE_IMAGE="") or
    when the upload fails — callers treat None as "skip the image send".
    """
    global _HANDSHAKE_MEDIA_ID  # noqa: PLW0603

    if _HANDSHAKE_MEDIA_ID:
        return _HANDSHAKE_MEDIA_ID

    img_path = handshake_image_path()
    if img_path is None:
        logger.debug("[whatsapp-call] visual handshake disabled or image not found")
        return None

    import aiohttp as _aiohttp
    from voxtera.whatsapp.client import WhatsAppClient as _WAClient

    try:
        async with _aiohttp.ClientSession() as _http:
            _wa = _WAClient(settings=settings, session=_http)
            _HANDSHAKE_MEDIA_ID = await _wa.upload_media(img_path)
        logger.info("[whatsapp-call] handshake image uploaded → media_id={}", _HANDSHAKE_MEDIA_ID)
    except Exception as e:  # noqa: BLE001
        logger.warning("[whatsapp-call] handshake image upload failed (feature disabled): {}", e)
        return None

    return _HANDSHAKE_MEDIA_ID


def make_call_bot(
    caller_wa_id: str | None,
) -> "Callable[[SmallWebRTCConnection], Awaitable[None]]":
    """Return a connection_callback with the caller's wa_id captured.

    Pipecat's ``handle_webhook_request`` only passes the ``SmallWebRTCConnection``
    to the callback — it drops call metadata like ``call.from_``.  We parse
    the caller's number from the raw webhook body in ``_process_call`` and
    close over it here so ``run_call_bot`` can send the visual handshake image
    to the right WhatsApp chat.
    """
    from collections.abc import Awaitable, Callable

    async def _callback(connection: SmallWebRTCConnection) -> None:
        await run_call_bot(connection, caller_wa_id=caller_wa_id)

    return _callback


async def run_call_bot(
    connection: SmallWebRTCConnection,
    *,
    caller_wa_id: str | None = None,
) -> None:
    """Run a Pipecat voice pipeline for one WhatsApp call. Returns on hang-up.

    Invoked via ``make_call_bot`` (the connection_callback factory) so the
    caller's WhatsApp id is available for the visual handshake image send.
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

    # The demo mode decides the soundscape (ambience vs fillers), so the
    # scope is resolved before the transport is built.
    hotel_scope = property_hotel_id()
    transport = _build_transport(connection, hotel_scope)

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
    wa_settings = load_whatsapp_settings()
    wa_region = wa_settings.default_region
    brain = TravelAgentBrain(
        region=wa_region or None,
        session_id=session_id,
        hotel_id=hotel_scope,
        # WhatsApp call: images CAN be delivered to the caller's chat, so the
        # hotel render is allowed to offer photos ([OFFER:<id>] tags).
        images=bool(caller_wa_id),
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
    # Affirmative "yes" → send the pending photo offer. MUST be before the
    # user aggregator (which consumes TranscriptionFrame); swallows the "yes"
    # so it never becomes a concierge turn.
    processors.append(VoiceAffirmativeDetector(caller_wa_id=caller_wa_id, settings=wa_settings))
    processors.append(context_aggregator.user())
    processors.append(brain)
    if tracer is not None:
        processors.append(tracer)
    # Strip [OFFER:<id>] from TTS text and send images/links on voice affirmatives.
    # Always added — handles None caller_wa_id gracefully (tag still stripped).
    processors.append(VoiceOfferProcessor(caller_wa_id=caller_wa_id, settings=wa_settings))
    processors.append(tts)
    # Voice fillers: a short "mm, one moment" in the bot's own voice when the
    # answer hasn't started within FILLER_DELAY_SECS. Placed after TTS so it
    # sees (and yields to) real speech; VAD frames reach it downstream too.
    filler_player = _build_filler_player(hotel_scope)
    if filler_player is not None:
        processors.append(filler_player)
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

    # Visual handshake: upload the hotel image once before the call connects so
    # the media_id is ready to send the instant the WebRTC peer connects. Upload
    # is best-effort — a failure logs a warning but never blocks the call.
    #
    # Why upload here instead of at server startup?  The WhatsAppClient (and its
    # aiohttp session) is created per-call via the Pipecat WhatsAppClient in
    # webhook.py, so we don't have a single long-lived client to pre-warm at boot.
    # Uploading at call-setup time costs ~200 ms on the first call; subsequent
    # calls using the same media_id (cached in _HANDSHAKE_MEDIA_ID) pay nothing.
    _handshake_media_id = await _ensure_handshake_media_id(wa_settings)

    # Greet the caller the moment the WebRTC media connects, so they hear the
    # agent immediately instead of dead air. Queued on connect (not at build)
    # because the audio track isn't live until the peer connection is up.
    greeting_text = _greeting_text(hotel_scope)

    @transport.event_handler("on_client_connected")
    async def _on_client_connected(_transport, _client) -> None:  # noqa: ANN001
        logger.info("[whatsapp-call] client connected (session={})", session_id)

        # --- Visual Handshake -------------------------------------------------
        # Send the hotel image into the guest's WhatsApp chat the instant the
        # voice call connects. The image appears in the text thread while the
        # voice greeting plays — the lobby literally materialises on screen.
        # Runs as a fire-and-forget task so it never delays the voice greeting.
        if _handshake_media_id and caller_wa_id:
            import asyncio
            import aiohttp as _aiohttp
            from voxtera.whatsapp.client import WhatsAppClient as _WAClient

            async def _send_handshake_image() -> None:
                try:
                    async with _aiohttp.ClientSession() as _http:
                        _wa = _WAClient(settings=wa_settings, session=_http)
                        await _wa.send_image(
                            to=caller_wa_id,
                            media_id=_handshake_media_id,
                            caption=handshake_caption(),
                        )
                    logger.info(
                        "[whatsapp-call] visual handshake sent to {} (session={})",
                        caller_wa_id,
                        session_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "[whatsapp-call] visual handshake failed for {}: {}",
                        caller_wa_id,
                        exc,
                    )

            asyncio.create_task(_send_handshake_image())
        # ----------------------------------------------------------------------

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
