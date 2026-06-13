"""Custom user-turn strategies.

- :class:`TailAwareTurnStopStrategy` — fixes the split-utterance race where
  the LLM run starts before Gladia's trailing final lands.

Background (trace ``wacall_8317dae263a6``, +37..+45 s): the caller's sentence
arrived as two Gladia finals 1.6 s apart. Pipecat's
:class:`TurnAnalyzerUserTurnStopStrategy` waits ``ttfs_p99 − vad_stop_secs``
(1.49 − 0.3 = **1.19 s**) after VAD stop for trailing transcripts, then runs
the LLM with whatever it has. Gladia's measured tail in production is up to
~1.6 s, so the second final missed the turn by ~200 ms and the bot answered
half a question ("lütfen sorunuzu tamamlayın").

Two compounding problems in the stock strategy when used with Gladia:

1. Gladia frames never set ``TranscriptionFrame.finalized``, so the
   early-trigger path never fires — EVERY turn silently pays the full
   1.19 s timeout even when the final arrived hundreds of ms earlier.
2. The timeout is a hard cliff: a final arriving just after it is lost to
   the next turn (or dropped by the suppressor).

Fix — two complementary behaviours:

- **Trigger on tail final**: a (non-``finalized``) final arriving in the wait
  window (after VAD stop, before timeout) covers the utterance's end — Gladia
  segments utterances with its own VAD — so treat it like a finalized
  transcript and stop the turn immediately. This makes most turns FASTER than
  stock (no more fixed 1.19 s wait).
- **Raised cap**: because the common case now triggers on final arrival, the
  p99-based timeout can be raised (via ``ttfs_p99_latency`` on the STT
  service) to cover Gladia's real tail without slowing normal turns. The
  timeout is paid only when Gladia is genuinely slow.

Optionally, when Gladia partials are enabled, an unconsumed interim (newer
than the last final) holds the trigger up to ``max_tail_wait_secs`` — the
strongest possible "more text is coming" signal. With partials disabled this
is a harmless no-op.
"""

from __future__ import annotations

import time
from typing import Optional

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy

__all__ = ["TailAwareTurnStopStrategy"]


class TailAwareTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """Turn-stop strategy that respects late STT finals (Gladia tail).

    See module docstring for the full rationale.
    """

    def __init__(self, *, max_tail_wait_secs: float = 2.5, **kwargs) -> None:
        """Initialize the strategy.

        Args:
            max_tail_wait_secs: Hard cap (measured from VAD stop) on how long
                an unconsumed interim transcript may hold the turn open while
                waiting for its final. Only relevant when the STT service
                emits partial transcripts.
            **kwargs: Forwarded to :class:`TurnAnalyzerUserTurnStopStrategy`
                (notably ``turn_analyzer``).
        """
        super().__init__(**kwargs)
        self._max_tail_wait_secs = max_tail_wait_secs
        # True when an InterimTranscriptionFrame arrived and no final has
        # consumed it yet — i.e. an utterance is still being decoded.
        self._interim_pending = False
        # Monotonic time of the most recent VAD stop; None while the user is
        # speaking. Anchors the max_tail_wait cap.
        self._wait_window_opened_at: Optional[float] = None
        self._tail_cap_task = None

    async def reset(self) -> None:
        """Reset the strategy to its initial state."""
        await super().reset()
        self._interim_pending = False
        self._wait_window_opened_at = None
        await self._cancel_tail_cap()

    async def cleanup(self) -> None:
        """Cleanup the strategy."""
        await super().cleanup()
        await self._cancel_tail_cap()

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        """Track interim transcripts in addition to the base frame handling."""
        if isinstance(frame, InterimTranscriptionFrame) and frame.text.strip():
            self._interim_pending = True
        return await super().process_frame(frame)

    async def _handle_vad_user_started_speaking(self, frame: VADUserStartedSpeakingFrame) -> None:
        self._wait_window_opened_at = None
        await self._cancel_tail_cap()
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_vad_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame) -> None:
        # Entering the wait window: turn analysis done, waiting for the STT
        # tail. Finals arriving from here on cover the utterance's end.
        self._wait_window_opened_at = time.monotonic()
        await super()._handle_vad_user_stopped_speaking(frame)

    async def _handle_transcription(self, frame: TranscriptionFrame) -> None:
        # A final consumes any in-flight utterance.
        self._interim_pending = False
        in_window = self._wait_window_opened_at is not None and not self._vad_user_speaking
        await super()._handle_transcription(frame)
        # Tail final fast-path: a final landing in the wait window means the
        # STT has caught up with everything spoken before VAD stop — no reason
        # to keep waiting for the p99 timeout. Skipped when the base class
        # already triggered (frame.finalized path, or the late-transcript
        # path after timeout expiry) to avoid a double turn-stop.
        if in_window and not frame.finalized and not self._timeout_expired:
            self._transcript_finalized = True
            logger.debug(
                "{}: tail final in wait window — stopping turn without "
                "waiting for STT p99 timeout",
                self,
            )
            await self._maybe_trigger_user_turn_stopped()

    async def _maybe_trigger_user_turn_stopped(self) -> None:
        # Interim hold: an unconsumed interim means the STT is mid-utterance —
        # its final is imminent and belongs to THIS turn. Hold the trigger
        # (up to max_tail_wait_secs from VAD stop); the final's arrival
        # triggers via _handle_transcription, or the cap task fires as a
        # safety net (e.g. a Gladia stall — see the 9.4 s "bir de" loop).
        if self._interim_pending and self._turn_complete and self._text:
            anchor = self._wait_window_opened_at
            waited = time.monotonic() - anchor if anchor is not None else 0.0
            remaining = self._max_tail_wait_secs - waited
            if remaining > 0:
                if self._tail_cap_task is None:
                    logger.debug(
                        "{}: interim pending at turn stop — holding up to "
                        "{:.2f}s for its final",
                        self,
                        remaining,
                    )
                    self._tail_cap_task = self.task_manager.create_task(
                        self._tail_cap_handler(remaining), f"{self}::_tail_cap_handler"
                    )
                return
        await super()._maybe_trigger_user_turn_stopped()

    async def _tail_cap_handler(self, timeout: float) -> None:
        """Force the turn stop if the held-for final never arrives."""
        import asyncio

        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        finally:
            self._tail_cap_task = None
        if self._interim_pending:
            logger.warning(
                "{}: held {:.2f}s for an interim's final that never arrived — "
                "stopping turn with current text",
                self,
                timeout,
            )
            self._interim_pending = False
            await self._maybe_trigger_user_turn_stopped()

    async def _cancel_tail_cap(self) -> None:
        if self._tail_cap_task is not None:
            task, self._tail_cap_task = self._tail_cap_task, None
            await self.task_manager.cancel_task(task)
