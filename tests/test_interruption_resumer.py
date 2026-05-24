"""Tests for InterruptionResumer — returns to a reply the guest cut off.

These drive the processor with pipecat's own ``run_test`` harness, which sets
up the full processor lifecycle (StartFrame, task manager, interruption
handling) — necessary because :class:`InterruptionResumer` reacts to
:class:`InterruptionFrame`, a system frame the base ``FrameProcessor`` handles
specially. ``SleepFrame`` is used to space frames so the barge-in timing
relative to ``resume_window_secs`` is deterministic.
"""

from __future__ import annotations

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    TranscriptionFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from voxtera.controllers import InterruptionResumer

# ---------- helpers ----------


def _transcript(text: str) -> TranscriptionFrame:
    """A finalized guest utterance, as STT would emit it."""
    return TranscriptionFrame(text=text, user_id="guest", timestamp="2026-05-22T10:00:00Z")


def _notes(down_frames: list) -> list[LLMMessagesAppendFrame]:
    """The resume notes the processor injected into the context."""
    return [f for f in down_frames if isinstance(f, LLMMessagesAppendFrame)]


# ---------- tests ----------


@pytest.mark.asyncio
async def test_resume_note_injected_on_early_barge_in() -> None:
    """A barge-in early in the reply arms a resume note on the next transcript."""
    resumer = InterruptionResumer(enabled=True, resume_window_secs=5.0)
    frames = [
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        InterruptionFrame(),
        SleepFrame(sleep=0.05),
        _transcript("and tell me about the spa"),
    ]
    down, _ = await run_test(resumer, frames_to_send=frames)

    notes = _notes(down)
    assert len(notes) == 1, "an early barge-in should inject exactly one resume note"
    # The note is a user-role message carrying the out-of-band directive.
    assert notes[0].messages[0]["role"] == "user"
    assert "cut off" in notes[0].messages[0]["content"].lower()
    # The interrupting transcript must still flow through — it is the new turn.
    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert len(transcripts) == 1, "the guest's interrupting utterance must not be dropped"


@pytest.mark.asyncio
async def test_no_note_on_clean_turn() -> None:
    """A reply that finishes cleanly (no barge-in) must not inject a note."""
    resumer = InterruptionResumer(enabled=True, resume_window_secs=5.0)
    frames = [
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        BotStoppedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        _transcript("what time is breakfast"),
    ]
    down, _ = await run_test(resumer, frames_to_send=frames)

    assert _notes(down) == [], "no barge-in → no resume note"


@pytest.mark.asyncio
async def test_no_note_when_barge_in_past_window() -> None:
    """A barge-in after the resume window is treated as a near-complete reply."""
    resumer = InterruptionResumer(enabled=True, resume_window_secs=0.05)
    frames = [
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.25),  # reply runs well past the 0.05 s window
        InterruptionFrame(),
        SleepFrame(sleep=0.05),
        _transcript("and the spa"),
    ]
    down, _ = await run_test(resumer, frames_to_send=frames)

    assert _notes(down) == [], "a barge-in past the window should not resume"


@pytest.mark.asyncio
async def test_no_note_when_disabled() -> None:
    """With the feature disabled, even an early barge-in injects no note."""
    resumer = InterruptionResumer(enabled=False, resume_window_secs=5.0)
    frames = [
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        InterruptionFrame(),
        SleepFrame(sleep=0.05),
        _transcript("and the spa"),
    ]
    down, _ = await run_test(resumer, frames_to_send=frames)

    assert _notes(down) == [], "a disabled resumer must not inject a note"


@pytest.mark.asyncio
async def test_note_injected_once_per_barge_in() -> None:
    """The note is one-shot: a multi-segment utterance injects it only once."""
    resumer = InterruptionResumer(enabled=True, resume_window_secs=5.0)
    frames = [
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        InterruptionFrame(),
        SleepFrame(sleep=0.05),
        _transcript("and tell me"),
        SleepFrame(sleep=0.05),
        _transcript("about the spa"),
    ]
    down, _ = await run_test(resumer, frames_to_send=frames)

    assert len(_notes(down)) == 1, "only the first transcript after a barge-in gets the note"


@pytest.mark.asyncio
async def test_second_clean_turn_after_resume_does_not_refire() -> None:
    """Once consumed, the flag stays clear: a later clean turn injects no note."""
    resumer = InterruptionResumer(enabled=True, resume_window_secs=5.0)
    frames = [
        # First turn: barge-in → one note.
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        InterruptionFrame(),
        SleepFrame(sleep=0.05),
        _transcript("and the spa"),
        # Second turn: clean reply, then a normal question.
        SleepFrame(sleep=0.05),
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        BotStoppedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        _transcript("what about parking"),
    ]
    down, _ = await run_test(resumer, frames_to_send=frames)

    assert len(_notes(down)) == 1, "the resume note must fire once, not leak into the next turn"
