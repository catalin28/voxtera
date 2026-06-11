"""Tests for FillerPlayer — voice fillers that mask LLM thinking time.

Driven with pipecat's ``run_test`` harness (full processor lifecycle) and
``SleepFrame`` for deterministic timing: the filler delay is shrunk to 80 ms
so "slow turn" is a 200 ms sleep and "fast turn" is a 30 ms one.
"""

from __future__ import annotations

import wave

import pytest
from pipecat.frames.frames import (
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from voxtera.fillers import FillerPlayer, load_filler_clips

SR = 16000
CHUNK = int(SR * 0.02) * 2  # one 20 ms chunk, 16-bit mono


def _clips(n_chunks_en: int = 3, n_chunks_tr: int = 5) -> dict[str, list[bytes]]:
    return {
        "en": [b"\x01\x00" * (CHUNK // 2) * n_chunks_en],
        "tr": [b"\x02\x00" * (CHUNK // 2) * n_chunks_tr],
    }


def _filler_audio(down: list) -> bytes:
    return b"".join(f.audio for f in down if isinstance(f, TTSAudioRawFrame))


@pytest.mark.asyncio
async def test_filler_fires_when_answer_is_slow() -> None:
    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.08)
    down, _ = await run_test(
        player,
        frames_to_send=[VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.3)],
    )
    audio = _filler_audio(down)
    assert len(audio) == 3 * CHUNK  # the whole en clip was played
    assert audio[:2] == b"\x01\x00"


@pytest.mark.asyncio
async def test_no_filler_when_answer_is_fast() -> None:
    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.08)
    real = TTSAudioRawFrame(audio=b"\x09\x00" * (CHUNK // 2), sample_rate=SR, num_channels=1)
    down, _ = await run_test(
        player,
        frames_to_send=[
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.03),
            real,  # real speech arrives before the 80 ms deadline
            SleepFrame(sleep=0.3),
        ],
    )
    audio = _filler_audio(down)
    assert audio == real.audio  # only the real frame — no filler bytes


@pytest.mark.asyncio
async def test_no_filler_when_guest_resumes_speaking() -> None:
    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.08)
    down, _ = await run_test(
        player,
        frames_to_send=[
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.03),
            VADUserStartedSpeakingFrame(),  # guest kept talking
            SleepFrame(sleep=0.3),
        ],
    )
    assert _filler_audio(down) == b""


@pytest.mark.asyncio
async def test_filler_fires_at_most_once_per_turn() -> None:
    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.05)
    down, _ = await run_test(
        player,
        frames_to_send=[VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.4)],
    )
    assert len(_filler_audio(down)) == 3 * CHUNK  # one clip, not a loop


@pytest.mark.asyncio
async def test_filler_follows_detected_language() -> None:
    from pipecat.frames.frames import TranscriptionFrame

    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.05)
    down, _ = await run_test(
        player,
        frames_to_send=[
            TranscriptionFrame(text="merhaba", user_id="g", timestamp="t", language="tr-TR"),
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=0.4),
        ],
    )
    audio = _filler_audio(down)
    assert len(audio) == 5 * CHUNK  # the tr clip
    assert audio[:2] == b"\x02\x00"


def test_load_filler_clips_validates_format(tmp_path) -> None:
    en = tmp_path / "en"
    en.mkdir()

    def write(name: str, rate: int, channels: int = 1) -> None:
        with wave.open(str(en / name), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x01" * 100 * channels)

    write("good.wav", SR)
    write("wrong_rate.wav", 44100)
    write("stereo.wav", SR, channels=2)

    clips = load_filler_clips(tmp_path, sample_rate=SR)
    assert list(clips) == ["en"]
    assert len(clips["en"]) == 1  # only the valid one survived
