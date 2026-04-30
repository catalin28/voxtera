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
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.daily.transport import DailyOutputTransportMessageFrame

from voxtera.conversation_logger import log_bot_reply, log_user_query


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
        # Per-turn state (cleared at the start of each user turn).
        self._user_stopped_at: float | None = None
        self._llm_started_at: float | None = None
        self._llm_chunks: list[str] = []
        self._disconnect_error_logged = False

    def _reset_turn_state(self) -> None:
        self._user_stopped_at = None
        self._llm_started_at = None
        self._llm_chunks = []

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Errors — always loud.
        if isinstance(frame, FatalErrorFrame):
            logger.error("[{}] FATAL: {}", self._label, getattr(frame, "error", frame))
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

        # Turn boundaries from VAD
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._reset_turn_state()
            logger.info("[{}] you started speaking", self._label)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_stopped_at = time.monotonic()
            logger.info("[{}] you stopped speaking", self._label)

        # What was heard
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.info("[{}] heard: {!r}", self._label, text)
                log_user_query(user_query=text, hotel_id=self._hotel_id)
        elif isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.debug("[{}] interim: {!r}", self._label, text)

        # Bot turn lifecycle — collect chunks + measure timing
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_started_at = time.monotonic()
            self._llm_chunks = []
            logger.info("[{}] bot is thinking...", self._label)
        elif isinstance(frame, LLMTextFrame):
            chunk = frame.text or ""
            if chunk:
                self._llm_chunks.append(chunk)
        elif isinstance(frame, LLMFullResponseEndFrame):
            reply = "".join(self._llm_chunks).strip()
            think_ms = None
            if self._llm_started_at is not None:
                think_ms = (time.monotonic() - self._llm_started_at) * 1000
                logger.info(
                    "[{}] bot replied (thought {:.0f}ms): {!r}",
                    self._label,
                    think_ms,
                    reply or "<empty>",
                )
            else:
                logger.info("[{}] bot replied: {!r}", self._label, reply or "<empty>")

            # Structured conversation log for audit / evaluation.
            if reply:
                log_bot_reply(reply=reply, elapsed_ms=think_ms)

        # TTS lifecycle stays at DEBUG; latency is what matters at INFO.
        elif isinstance(frame, TTSStartedFrame):
            logger.debug("[{}] TTS started", self._label)
        elif isinstance(frame, TTSStoppedFrame):
            logger.debug("[{}] TTS stopped", self._label)

        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._user_stopped_at is not None:
                total_ms = (time.monotonic() - self._user_stopped_at) * 1000
                logger.info(
                    "[{}] bot is speaking (total latency {:.0f}ms)",
                    self._label,
                    total_ms,
                )
            else:
                # E.g. the startup greeting — no preceding user turn.
                logger.info("[{}] bot is speaking", self._label)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            logger.debug("[{}] bot stopped speaking", self._label)

        # Anything else: silent.

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
