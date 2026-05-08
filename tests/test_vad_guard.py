"""Tests for VADSanityGuard — suppresses false VAD triggers on silence."""

from __future__ import annotations

import numpy as np
import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from voxtera.audio import VADSanityGuard

# ---------- helpers ----------


def _silence_frame(n_samples: int = 320) -> InputAudioRawFrame:
    """20 ms of silence at 16 kHz mono."""
    audio = np.zeros(n_samples, dtype=np.int16).tobytes()
    return InputAudioRawFrame(audio=audio, sample_rate=16000, num_channels=1)


def _speech_frame(amplitude: int = 3000, n_samples: int = 320) -> InputAudioRawFrame:
    """20 ms of simulated speech (sine-ish) at 16 kHz mono."""
    t = np.linspace(0, 2 * np.pi, n_samples, dtype=np.float32)
    audio = (np.sin(t) * amplitude).astype(np.int16).tobytes()
    return InputAudioRawFrame(audio=audio, sample_rate=16000, num_channels=1)


class FrameCollector:
    """Minimal downstream sink that collects pushed frames."""

    def __init__(self) -> None:
        self.frames: list = []

    async def process_frame(self, frame, direction):
        self.frames.append(frame)


async def _run_frames(guard: VADSanityGuard, frames: list) -> list:
    """Push frames through the guard and return what it emits."""
    collected: list = []
    original_push = guard.push_frame

    async def _capture(frame, direction=FrameDirection.DOWNSTREAM):
        collected.append(frame)

    guard.push_frame = _capture  # type: ignore[assignment]
    for f in frames:
        await guard.process_frame(f, FrameDirection.DOWNSTREAM)
    guard.push_frame = original_push  # type: ignore[assignment]
    return collected


# ---------- tests ----------


@pytest.mark.asyncio
async def test_suppresses_vad_on_silence() -> None:
    """VAD start on zero-energy audio must be dropped."""
    guard = VADSanityGuard()
    frames = [
        _silence_frame(),
        _silence_frame(),
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
    ]
    output = await _run_frames(guard, frames)
    # Only audio frames should pass; both VAD frames should be suppressed
    vad_starts = [f for f in output if isinstance(f, VADUserStartedSpeakingFrame)]
    vad_stops = [f for f in output if isinstance(f, VADUserStoppedSpeakingFrame)]
    assert len(vad_starts) == 0, "VAD start on silence should be suppressed"
    assert len(vad_stops) == 0, "matching VAD stop should also be suppressed"
    # Audio frames should still pass through
    audio_frames = [f for f in output if isinstance(f, InputAudioRawFrame)]
    assert len(audio_frames) == 2


@pytest.mark.asyncio
async def test_allows_vad_on_real_speech() -> None:
    """VAD start on real speech energy must pass through."""
    guard = VADSanityGuard()
    frames = [
        _speech_frame(),
        _speech_frame(),
        _speech_frame(),
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
    ]
    output = await _run_frames(guard, frames)
    vad_starts = [f for f in output if isinstance(f, VADUserStartedSpeakingFrame)]
    vad_stops = [f for f in output if isinstance(f, VADUserStoppedSpeakingFrame)]
    assert len(vad_starts) == 1, "VAD start on speech should pass"
    assert len(vad_stops) == 1, "VAD stop should pass when start was not suppressed"


@pytest.mark.asyncio
async def test_suppresses_vad_with_no_audio_history() -> None:
    """VAD start with zero audio frames in history must be dropped."""
    guard = VADSanityGuard()
    frames = [
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
    ]
    output = await _run_frames(guard, frames)
    vad_starts = [f for f in output if isinstance(f, VADUserStartedSpeakingFrame)]
    assert len(vad_starts) == 0, "VAD start with no audio history should be suppressed"


@pytest.mark.asyncio
async def test_passes_unrelated_frames() -> None:
    """Non-audio, non-VAD frames must pass through unchanged."""
    guard = VADSanityGuard()
    text_frame = TextFrame(text="hello")
    frames = [text_frame]
    output = await _run_frames(guard, frames)
    assert len(output) == 1
    assert output[0] is text_frame


@pytest.mark.asyncio
async def test_second_vad_after_speech_passes() -> None:
    """After suppressing a false trigger, real speech VAD still works."""
    guard = VADSanityGuard()
    frames = [
        # First: false trigger on silence
        _silence_frame(),
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
        # Then: real speech
        _speech_frame(),
        _speech_frame(),
        _speech_frame(),
        _speech_frame(),
        VADUserStartedSpeakingFrame(),
        VADUserStoppedSpeakingFrame(),
    ]
    output = await _run_frames(guard, frames)
    vad_starts = [f for f in output if isinstance(f, VADUserStartedSpeakingFrame)]
    assert len(vad_starts) == 1, "Second VAD on real speech should pass"
