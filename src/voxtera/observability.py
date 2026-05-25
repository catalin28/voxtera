"""Pipeline observability: turn-level INFO logs and browser-bound events.

- :class:`PipelineTracer` — emits the small set of INFO log lines that
  describe a turn (you started speaking → heard → bot is thinking → bot
  replied → bot is speaking with latency). Quiet for everything else.
- :class:`UserTranscriptBroadcaster` — emits Daily app-messages for user-side
  events (started/stopped/transcript). Read by the demo page.
- :class:`DemoEventBroadcaster` — emits Daily app-messages for bot-side
  events (thinking/replying/speaking) so the demo page can render a live
  transcript and status badge.
- :func:`_is_daily_disconnected_error_text` — shared error-text matcher used
  by all three to suppress noisy repeated send-message errors after a Daily
  transport drop.
"""

from __future__ import annotations

import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FatalErrorFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

try:
    from pipecat.transports.daily.transport import DailyOutputTransportMessageFrame
except Exception:  # daily-python not available on Windows
    DailyOutputTransportMessageFrame = None  # type: ignore[assignment,misc]

from voxtera.call_record import (
    record_bot_turn,
    record_interruption,
    record_usage,
    record_user_turn,
)
from voxtera.conversation_logger import log_bot_reply, log_user_query
from voxtera.stt import (
    STT_MODEL_DEEPGRAM,
    STT_MODEL_ELEVENLABS,
    STT_MODEL_GOOGLE,
    STT_MODEL_WHISPER,
)
from voxtera.trace import emit as _trace_emit
from voxtera.trace import tracker as _trace_tracker
from voxtera.tts import (
    TTS_MODEL,
    TTS_MODEL_CARTESIA,
    TTS_MODEL_ELEVENLABS,
    TTS_MODEL_GOOGLE,
)

# Per-provider STT/TTS model identifiers, built from the canonical constants
# in voxtera.stt and voxtera.tts so the trace stream's session_providers
# event automatically reflects any model swap (e.g. switching
# STT_MODEL_GOOGLE from "latest_long" to "latest_short" in stt.py) without
# requiring a parallel edit here.
_STT_MODEL_BY_PROVIDER = {
    "whisper": STT_MODEL_WHISPER,
    "deepgram": STT_MODEL_DEEPGRAM,
    "google": STT_MODEL_GOOGLE,
    # google-chirp2 uses a hardcoded model in its builder; surface it here
    # so the dashboard doesn't fall back to a stale model name from a
    # previous provider when the user switches to chirp2.
    "google-chirp2": "chirp_2",
    "elevenlabs": STT_MODEL_ELEVENLABS,
}
_TTS_MODEL_BY_PROVIDER = {
    "openai": TTS_MODEL,
    "google": TTS_MODEL_GOOGLE,
    "cartesia": TTS_MODEL_CARTESIA,
    "elevenlabs": TTS_MODEL_ELEVENLABS,
}


def _current_providers() -> dict:
    """Snapshot of the currently-active models read from the tunables registry.

    Returns a dict suitable for splatting into a ``session_providers``
    lifecycle event. Lazy import keeps observability.py free of an import
    cycle with tunables.py (which imports trace.py which is imported here).
    """
    from voxtera.tunables import TunablesRegistry

    reg = TunablesRegistry.instance()
    out: dict = {}
    for name in ("stt_provider", "tts_provider", "tts_voice", "llm_model"):
        knob = reg.get(name)
        if knob is not None and knob.current is not None:
            out[name] = knob.current
    # Derive the underlying model identifier from the active provider so the
    # dashboard can show "google/latest_long" instead of just "google".
    stt_p = out.get("stt_provider")
    if stt_p in _STT_MODEL_BY_PROVIDER:
        out["stt_model"] = _STT_MODEL_BY_PROVIDER[stt_p]
    tts_p = out.get("tts_provider")
    if tts_p in _TTS_MODEL_BY_PROVIDER:
        out["tts_model"] = _TTS_MODEL_BY_PROVIDER[tts_p]
    return out


def _is_daily_disconnected_error_text(text: str) -> bool:
    return "TrySendError { kind: Disconnected }" in text


class PipelineTracer(FrameProcessor):
    """Logs the few frames that meaningfully describe a turn, plus timing.

    What you get per turn at INFO:

      [voxtera] you started speaking
      [voxtera] heard: 'Hello, can you recommend a museum in Paris?'
      [voxtera] you stopped speaking
      [voxtera] bot is thinking...
      [voxtera] bot replied (thought 0.92s): 'The Louvre is...'
      [voxtera] bot is speaking (total latency 1.34s)

    Errors are always loud (no silent failures). Uncategorised frames are
    intentionally not logged at any level.
    """

    def __init__(self, label: str, *, hotel_id: str | None = None) -> None:
        super().__init__()
        self._label = label
        self._hotel_id = hotel_id
        # LLM-internal state (only this processor sees the LLM frames going
        # downstream past it). Cross-stage anchors live on TurnTracker so
        # other processors at other positions can write/read them.
        self._llm_started_at: float | None = None
        self._llm_first_chunk_at: float | None = None
        self._llm_chunks: list[str] = []
        self._disconnect_error_logged = False

    def _reset_turn_state(self) -> None:
        # Local LLM-internal state. Cross-stage anchors are cleared by
        # TurnTracker.start_user_turn / end_turn.
        self._llm_started_at = None
        self._llm_first_chunk_at = None
        self._llm_chunks = []

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Errors — always loud.
        if isinstance(frame, FatalErrorFrame):
            logger.error("[{}] FATAL: {}", self._label, getattr(frame, "error", frame))
            _trace_emit(
                "error",
                source=self._label,
                turn_id=_trace_tracker().current(),
                data={"level": "fatal", "message": str(getattr(frame, "error", frame))},
            )
        elif isinstance(frame, ErrorFrame):
            error_text = str(getattr(frame, "error", frame))
            if _is_daily_disconnected_error_text(error_text):
                if not self._disconnect_error_logged:
                    logger.warning(
                        "[{}] transport disconnected; suppressing repeated send-message errors",
                        self._label,
                    )
                    self._disconnect_error_logged = True
            else:
                logger.error("[{}] error: {}", self._label, error_text)
                _trace_emit(
                    "error",
                    source=self._label,
                    turn_id=_trace_tracker().current(),
                    data={"level": "error", "message": error_text},
                )

        # Turn boundaries from VAD. Both VAD frames propagate up AND down,
        # so PipelineTracer reliably sees them at its position.
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._reset_turn_state()
            # ``start_user_turn`` also clears any leftover anchors from a
            # previous turn (defence against typed-text turns reusing stale
            # voice-turn anchors).
            turn_id = _trace_tracker().start_user_turn()
            logger.info("[{}] you started speaking", self._label)
            _trace_emit(
                "lifecycle",
                source=self._label,
                turn_id=turn_id,
                data={"event": "user_started"},
            )
            # Stamp the active provider/model snapshot at the start of every
            # user turn. The dashboard reads this rather than relying on the
            # /knobs HTTP poll, which can be empty (TuneServer offline) or
            # stale (live edits not yet reflected). Keeps the trace stream
            # the single source of truth for what produced any given turn.
            providers = _current_providers()
            if providers:
                _trace_emit(
                    "lifecycle",
                    source=self._label,
                    turn_id=turn_id,
                    data={"event": "session_providers", **providers},
                )
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Shared anchor for stt timing (read by TranscriptStageTimer) and
            # end_to_end (read here on BotStartedSpeakingFrame).
            _trace_tracker().stamp("user_stopped")
            logger.info("[{}] you stopped speaking", self._label)
            _trace_emit(
                "lifecycle",
                source=self._label,
                turn_id=_trace_tracker().current(),
                data={"event": "user_stopped"},
            )

        elif isinstance(frame, InterruptionFrame):
            # A barge-in: the guest cut the bot off mid-reply. Counted on the
            # per-call record so the post-call summary can flag choppy calls.
            logger.debug("[{}] interruption (barge-in)", self._label)
            record_interruption()

        elif isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.debug("[{}] interim: {!r}", self._label, text)

        # Bot turn lifecycle — collect chunks + measure timing.
        # ``TranscriptionFrame`` itself is handled by TranscriptStageTimer
        # which is positioned upstream of context_aggregator (PipelineTracer
        # is downstream of the aggregator and never sees the transcript).
        elif isinstance(frame, LLMFullResponseStartFrame):
            now = time.monotonic()
            # Named gap: transcript → LLM start. Captures the pipeline
            # plumbing between STT output and LLM invocation (context
            # aggregator, RAG retrieval, LLMRunGuard, BrowserTextInputController).
            stt_to_llm_ms = _trace_tracker().measure_ms_from("transcript", end=now)
            if stt_to_llm_ms is not None:
                _trace_emit(
                    "stage",
                    source=self._label,
                    turn_id=_trace_tracker().current(),
                    data={"stage": "stt_to_llm", "duration_ms": stt_to_llm_ms},
                )
            self._llm_started_at = now
            self._llm_first_chunk_at = None
            self._llm_chunks = []
            logger.info("[{}] bot is thinking...", self._label)
            _trace_emit(
                "lifecycle",
                source=self._label,
                turn_id=_trace_tracker().current(),
                data={"event": "llm_start"},
            )
        elif isinstance(frame, LLMTextFrame):
            chunk = frame.text or ""
            if chunk:
                if self._llm_first_chunk_at is None and self._llm_started_at is not None:
                    self._llm_first_chunk_at = time.monotonic()
                    ttft_ms = (self._llm_first_chunk_at - self._llm_started_at) * 1000
                    # Shared anchor for the streaming-aware llm→tts gap.
                    # ``llm_ttft_to_tts`` is measured as
                    # ``tts_started - llm_first_token``; that's always
                    # positive in streaming voice, unlike the misleading
                    # ``llm_end → tts_started`` which can be negative when
                    # TTS picks up the first sentence mid-stream.
                    _trace_tracker().stamp("llm_first_token", value=self._llm_first_chunk_at)
                    _trace_emit(
                        "stage",
                        source=self._label,
                        turn_id=_trace_tracker().current(),
                        data={"stage": "llm_ttft", "duration_ms": round(ttft_ms)},
                    )
                self._llm_chunks.append(chunk)
        elif isinstance(frame, LLMFullResponseEndFrame):
            reply = "".join(self._llm_chunks).strip()
            think_ms = None
            now = time.monotonic()
            if self._llm_started_at is not None:
                think_ms = (now - self._llm_started_at) * 1000
                logger.info(
                    "[{}] bot replied (thought {:.0f}ms): {!r}",
                    self._label,
                    think_ms,
                    reply or "<empty>",
                )
                _trace_emit(
                    "stage",
                    source=self._label,
                    turn_id=_trace_tracker().current(),
                    data={"stage": "llm_full", "duration_ms": round(think_ms)},
                )
                _trace_emit(
                    "lifecycle",
                    source=self._label,
                    turn_id=_trace_tracker().current(),
                    data={
                        "event": "bot_reply",
                        "text": reply,
                        "char_count": len(reply),
                    },
                )
            else:
                logger.info("[{}] bot replied: {!r}", self._label, reply or "<empty>")
            # Shared anchor for the next named gap (LLM end → TTS started),
            # measured by TTSStageTimer when it sees TTSStartedFrame.
            _trace_tracker().stamp("llm_ended", value=now)

            # Structured conversation log for audit / evaluation.
            if reply:
                log_bot_reply(reply=reply, elapsed_ms=think_ms)
                # Per-call record: append this reply as a transcript turn.
                record_bot_turn(text=reply, latency_ms=think_ms)

        # ``TTSStartedFrame`` and ``TTSStoppedFrame`` originate downstream of
        # this processor and don't bubble back. ``TTSStageTimer`` (positioned
        # right after the TTS service in pipeline.py) handles them and stamps
        # the ``tts_started`` anchor on the shared TurnTracker.

        elif isinstance(frame, MetricsFrame):
            # Pipecat emits MetricsFrame after each LLM turn with token usage,
            # including Anthropic's prompt-cache stats. Surface them so we can
            # tell whether enable_prompt_caching=True is actually hitting.
            #
            # Classification per turn (cr = cache_read, cc = cache_creation):
            #  - cr  > 0              →  HIT — the cache served part/all of the
            #                           prefix. A working cache still writes the
            #                           few new tokens of the latest turn, so
            #                           cc > 0 alongside cr > 0 is the NORMAL hit
            #                           case — do NOT require cc == 0.
            #  - cr == 0 and cc > 0   →  MISS — prefix written, nothing reused.
            #  - cr == 0 and cc == 0  →  OFF — caching disabled, or prefix below
            #                           Anthropic's 1024-token minimum.
            for item in frame.data or []:
                if isinstance(item, LLMUsageMetricsData):
                    usage = item.value
                    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
                    cc = getattr(usage, "cache_creation_input_tokens", 0) or 0
                    pt = getattr(usage, "prompt_tokens", 0) or 0
                    ct = getattr(usage, "completion_tokens", 0) or 0
                    state = "HIT" if cr > 0 else "MISS" if cc > 0 else "OFF"
                    logger.info(
                        "[{}] llm-cache {} | prompt={} cache_read={}"
                        " cache_creation={} completion={}",
                        self._label,
                        state,
                        pt,
                        cr,
                        cc,
                        ct,
                    )
                    _trace_emit(
                        "stage",
                        source=self._label,
                        turn_id=_trace_tracker().current(),
                        data={
                            "stage": "llm_cache",
                            "state": state,
                            "prompt_tokens": pt,
                            "cache_read_input_tokens": cr,
                            "cache_creation_input_tokens": cc,
                            "completion_tokens": ct,
                        },
                    )
                    # Per-call record: accumulate this turn's token usage so
                    # the finished record carries the call's total LLM cost.
                    record_usage(
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        cache_read_tokens=cr,
                        cache_creation_tokens=cc,
                    )

        elif isinstance(frame, BotStartedSpeakingFrame):
            now = time.monotonic()
            tracker = _trace_tracker()
            # End-to-end: user mouth-close → bot mouth-open. Read the shared
            # anchor that VADUserStoppedSpeakingFrame stamped earlier.
            total_ms = tracker.measure_ms_from("user_stopped", end=now)
            if total_ms is not None:
                logger.info(
                    "[{}] bot is speaking (total latency {}ms)",
                    self._label,
                    total_ms,
                )
                _trace_emit(
                    "stage",
                    source=self._label,
                    turn_id=tracker.current(),
                    data={"stage": "end_to_end", "duration_ms": total_ms},
                )
                # Clear the anchor so a typed-text turn following this voice
                # turn doesn't reuse a stale ``user_stopped`` and report a
                # bogus end_to_end. End-to-end is only meaningful once per
                # voice turn.
                tracker.clear_anchor("user_stopped")
            else:
                # E.g. the startup greeting — no preceding user turn.
                logger.info("[{}] bot is speaking", self._label)
            # TTS TTFT: TTSStarted → BotStartedSpeaking. Reads the anchor set
            # by TTSStageTimer.
            tts_ttft_ms = tracker.measure_ms_from("tts_started", end=now)
            if tts_ttft_ms is not None:
                _trace_emit(
                    "stage",
                    source=self._label,
                    turn_id=tracker.current(),
                    data={"stage": "tts_ttft", "duration_ms": tts_ttft_ms},
                )
            _trace_emit(
                "lifecycle",
                source=self._label,
                turn_id=tracker.current(),
                data={"event": "bot_speaking"},
            )
        elif isinstance(frame, BotStoppedSpeakingFrame):
            logger.debug("[{}] bot stopped speaking", self._label)
            _trace_emit(
                "lifecycle",
                source=self._label,
                turn_id=_trace_tracker().current(),
                data={"event": "bot_done"},
            )
            # ``end_turn`` clears every shared anchor (belt-and-braces against
            # cross-turn contamination, especially for typed-text turns that
            # have no VAD events of their own).
            _trace_tracker().end_turn()

        # Anything else: silent.

        await self.push_frame(frame, direction)


class TranscriptStageTimer(FrameProcessor):
    """Stamps the transcript anchor and emits the ``stt`` stage + transcript
    lifecycle events.

    Why this isn't part of :class:`PipelineTracer`: the transcript frame is
    consumed by ``context_aggregator.user()`` (it folds the text into the LLM
    context messages and does not push the frame on). PipelineTracer sits
    *downstream* of the aggregator, so it would never see ``TranscriptionFrame``
    and could not measure STT duration. Place this processor anywhere between
    the STT service and the user context aggregator and the timing works.

    The timing anchors live on the shared :class:`voxtera.trace.TurnTracker`
    so PipelineTracer can read ``transcript`` later when computing the
    ``stt_to_llm`` gap.
    """

    def __init__(self, label: str = "voxtera", *, hotel_id: str | None = None) -> None:
        super().__init__()
        self._label = label
        self._hotel_id = hotel_id

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.info("[{}] heard: {!r}", self._label, text)
                log_user_query(user_query=text, hotel_id=self._hotel_id)
                # Per-call record: append this utterance as a transcript turn,
                # carrying the language Whisper/Gladia detected for it.
                record_user_turn(text=text, language=str(getattr(frame, "language", "") or ""))
                tracker = _trace_tracker()
                # ``stt`` duration: VADUserStoppedSpeakingFrame → TranscriptionFrame.
                # ``user_stopped`` was stamped by PipelineTracer.
                stt_ms = tracker.measure_ms_from("user_stopped")
                # Stamp the transcript anchor regardless — PipelineTracer reads
                # it on LLMFullResponseStartFrame to compute ``stt_to_llm``.
                tracker.stamp("transcript")
                if stt_ms is not None:
                    _trace_emit(
                        "stage",
                        source=self._label,
                        turn_id=tracker.current(),
                        data={"stage": "stt", "duration_ms": stt_ms},
                    )
                _trace_emit(
                    "lifecycle",
                    source=self._label,
                    turn_id=tracker.current(),
                    data={
                        "event": "transcript",
                        "text": text,
                        "language": str(getattr(frame, "language", "") or ""),
                    },
                )

        await self.push_frame(frame, direction)


class TTSStageTimer(FrameProcessor):
    """Stamps the ``tts_started`` anchor and emits two LLM→TTS gap metrics.

    Why this isn't part of :class:`PipelineTracer`: ``TTSStartedFrame`` is
    emitted by the TTS service going downstream and never bubbles upstream
    through PipelineTracer (which sits before the TTS in the pipeline). Place
    this processor anywhere downstream of the TTS service to capture the
    frame.

    Two gap metrics are emitted:

    - **``llm_ttft_to_tts``** (always meaningful) — first LLM token →
      TTSStarted. This is the time TTS spent buffering tokens / waiting for
      a sentence boundary before its first synthesis call. It is the
      *latency-relevant* number: every millisecond of it delays bot speech.
    - **``llm_end_to_tts``** (only when positive) — LLMFullResponseEnd →
      TTSStarted. In streaming voice the TTS typically starts mid-stream
      (before the LLM has finished generating), so this delta is normally
      negative — meaning TTS started **before** the LLM finished. We only
      emit it when it's ≥ 0 (LLM finished early), as a "background work"
      indicator. A consistently positive value means the response was short
      enough that LLM finished before TTS picked it up.

    PipelineTracer reads the ``tts_started`` anchor on
    ``BotStartedSpeakingFrame`` to compute ``tts_ttft``.
    """

    def __init__(self, label: str = "voxtera") -> None:
        super().__init__()
        self._label = label

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            tracker = _trace_tracker()
            now = time.monotonic()
            # Latency-relevant gap. Read the anchor stamped by PipelineTracer
            # on the first LLMTextFrame.
            ttft_to_tts_ms = tracker.measure_ms_from("llm_first_token", end=now)
            # Background-work gap. Often negative in streaming — only useful
            # when the LLM happened to finish before TTS started.
            end_to_tts_ms = tracker.measure_ms_from("llm_ended", end=now)
            tracker.stamp("tts_started", value=now)
            if ttft_to_tts_ms is not None:
                _trace_emit(
                    "stage",
                    source=self._label,
                    turn_id=tracker.current(),
                    data={"stage": "llm_ttft_to_tts", "duration_ms": ttft_to_tts_ms},
                )
            if end_to_tts_ms is not None and end_to_tts_ms >= 0:
                _trace_emit(
                    "stage",
                    source=self._label,
                    turn_id=tracker.current(),
                    data={"stage": "llm_end_to_tts", "duration_ms": end_to_tts_ms},
                )

        await self.push_frame(frame, direction)


class UserTranscriptBroadcaster(FrameProcessor):
    """Captures user speech events early in the pipeline (before the context
    aggregator consumes them) and emits DailyOutputTransportMessageFrame
    events that flow downstream to the transport output."""

    def _evt(self, event: str, data: dict | None = None) -> DailyOutputTransportMessageFrame:
        return DailyOutputTransportMessageFrame(
            message={
                "type": "voxtera-event",
                "event": event,
                "ts": time.time(),
                "data": data or {},
            }
        )

    def __init__(self) -> None:
        super().__init__()
        self._downstream_connected = True
        self._disconnect_logged = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, ErrorFrame):
            error_text = str(getattr(frame, "error", frame))
            if _is_daily_disconnected_error_text(error_text):
                self._downstream_connected = False
                if not self._disconnect_logged:
                    logger.warning(
                        "[broadcast:user] daily disconnected; stopping user event app-messages"
                    )
                    self._disconnect_logged = True

        if not self._downstream_connected:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self.push_frame(self._evt("user-started"), FrameDirection.DOWNSTREAM)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self.push_frame(self._evt("user-stopped"), FrameDirection.DOWNSTREAM)
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                evt = self._evt("user-transcript", {"text": text})
                await self.push_frame(evt, FrameDirection.DOWNSTREAM)

        await self.push_frame(frame, direction)


class DemoEventBroadcaster(FrameProcessor):
    """Sends pipeline events to the browser via Daily's data channel.

    Pushes DailyOutputTransportMessageFrame with a JSON envelope so the
    demo page can render a live transcript and status badge.
    Only useful when transport_mode == "daily".
    """

    def __init__(self) -> None:
        super().__init__()
        self._user_stopped_at: float | None = None
        self._llm_started_at: float | None = None
        self._llm_chunks: list[str] = []
        self._downstream_connected = True
        self._disconnect_logged = False

    def _evt(self, event: str, data: dict | None = None) -> DailyOutputTransportMessageFrame:
        return DailyOutputTransportMessageFrame(
            message={"type": "voxtera-event", "event": event, "ts": time.time(), "data": data or {}}
        )

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, ErrorFrame):
            error_text = str(getattr(frame, "error", frame))
            if _is_daily_disconnected_error_text(error_text):
                self._downstream_connected = False
                if not self._disconnect_logged:
                    logger.warning(
                        "[broadcast:demo] daily disconnected; stopping demo event app-messages"
                    )
                    self._disconnect_logged = True

        if not self._downstream_connected:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_stopped_at = time.monotonic()

        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_started_at = time.monotonic()
            self._llm_chunks = []
            await self.push_frame(self._evt("bot-thinking"), FrameDirection.DOWNSTREAM)

        elif isinstance(frame, LLMTextFrame):
            chunk = frame.text or ""
            if chunk:
                self._llm_chunks.append(chunk)

        elif isinstance(frame, LLMFullResponseEndFrame):
            reply = "".join(self._llm_chunks).strip()
            think_ms = None
            if self._llm_started_at is not None:
                think_ms = round((time.monotonic() - self._llm_started_at) * 1000)
            if reply:
                await self.push_frame(
                    self._evt("bot-reply", {"text": reply, "think_ms": think_ms}),
                    FrameDirection.DOWNSTREAM,
                )

        elif isinstance(frame, BotStartedSpeakingFrame):
            latency_ms = None
            if self._user_stopped_at is not None:
                latency_ms = round((time.monotonic() - self._user_stopped_at) * 1000)
            await self.push_frame(
                self._evt("bot-speaking", {"latency_ms": latency_ms}),
                FrameDirection.DOWNSTREAM,
            )

        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self.push_frame(self._evt("bot-done-speaking"), FrameDirection.DOWNSTREAM)

        # Log TTS frames for debugging
        elif isinstance(frame, TTSStartedFrame):
            logger.info("[audio-debug] TTS started")

        elif isinstance(frame, TTSStoppedFrame):
            logger.info("[audio-debug] TTS stopped")

        await self.push_frame(frame, direction)
