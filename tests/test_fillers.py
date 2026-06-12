"""Tests for FillerPlayer — voice fillers that mask LLM thinking time.

Driven with pipecat's ``run_test`` harness (full processor lifecycle) and
``SleepFrame`` for deterministic timing: the filler delay is shrunk to 80 ms
so "slow turn" is a 200 ms sleep and "fast turn" is a 30 ms one.
"""

from __future__ import annotations

import wave

import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test

from voxtera.fillers import FillerPlayer, load_filler_clips


def _final(text: str, language: str | None = None) -> TranscriptionFrame:
    """A finalized guest utterance — the arming signal (an answer is coming)."""
    return TranscriptionFrame(text=text, user_id="g", timestamp="t", language=language)


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
        frames_to_send=[_final("what time is breakfast?"), SleepFrame(sleep=0.3)],
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
            _final("hello"),
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
            _final("hello"),
            SleepFrame(sleep=0.03),
            VADUserStartedSpeakingFrame(),  # guest kept talking
            SleepFrame(sleep=0.3),
        ],
    )
    assert _filler_audio(down) == b""


@pytest.mark.asyncio
async def test_started_filler_finishes_when_answer_arrives_mid_clip() -> None:
    """Real speech arriving DURING the clip must not truncate it — a cut
    half-syllable ('Mm—') is an unidentifiable blip to the caller."""
    player = FillerPlayer(_clips(n_chunks_en=10), sample_rate=SR, delay_secs=0.05)
    real = TTSAudioRawFrame(audio=b"\x09\x00" * (CHUNK // 2), sample_rate=SR, num_channels=1)
    down, _ = await run_test(
        player,
        frames_to_send=[
            _final("hello"),
            SleepFrame(sleep=0.08),  # filler fired, clip mid-play (10 paced chunks)
            real,  # the answer's audio arrives now
            SleepFrame(sleep=0.4),
        ],
    )
    audio = _filler_audio(down)
    # Whole 10-chunk clip + the real frame, nothing truncated.
    assert len(audio) == 10 * CHUNK + len(real.audio)


@pytest.mark.asyncio
async def test_filler_fires_at_most_once_per_turn() -> None:
    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.05)
    down, _ = await run_test(
        player,
        frames_to_send=[_final("what time is breakfast?"), SleepFrame(sleep=0.4)],
    )
    assert len(_filler_audio(down)) == 3 * CHUNK  # one clip, not a loop


@pytest.mark.asyncio
async def test_filler_follows_detected_language() -> None:
    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.05)
    down, _ = await run_test(
        player,
        frames_to_send=[
            _final("merhaba", language="tr-TR"),
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


@pytest.mark.asyncio
async def test_vad_stop_without_transcript_never_arms() -> None:
    """A trailing-off 'umm' (VAD stop, no transcript) means NO answer is
    coming — the filler must stay silent instead of promising one."""
    from pipecat.frames.frames import VADUserStoppedSpeakingFrame

    player = FillerPlayer(_clips(), sample_rate=SR, delay_secs=0.05)
    down, _ = await run_test(
        player,
        frames_to_send=[VADUserStoppedSpeakingFrame(), SleepFrame(sleep=0.3)],
    )
    assert _filler_audio(down) == b""


def test_filler_settings_read_from_json(tmp_path) -> None:
    from voxtera.fillers import load_filler_settings

    (tmp_path / "fillers.json").write_text(
        '{"modes": {"hotel": {"enabled": true, "delay_secs": 3.5, "clips": ["en/x.wav"]},'
        ' "travel": {"enabled": false, "delay_secs": 0.9, "clips": []}}}'
    )
    hotel = load_filler_settings("hotel", tmp_path)
    assert hotel == {"enabled": True, "delay_secs": 3.5, "clips": ["en/x.wav"]}
    travel = load_filler_settings("travel", tmp_path)
    assert travel["enabled"] is False and travel["clips"] == []


def test_filler_settings_defaults_when_missing_or_broken(tmp_path) -> None:
    from voxtera.fillers import load_filler_settings

    # No file at all → historical defaults.
    assert load_filler_settings("hotel", tmp_path)["enabled"] is False
    assert load_filler_settings("travel", tmp_path) == {
        "enabled": True,
        "delay_secs": 1.2,
        "clips": None,
    }
    # Broken JSON → same defaults, no exception.
    (tmp_path / "fillers.json").write_text("{not json")
    assert load_filler_settings("travel", tmp_path)["enabled"] is True


def test_clip_selection_filters_loaded_files(tmp_path) -> None:
    for lang, names in {"en": ["01", "02"], "tr": ["01"]}.items():
        d = tmp_path / lang
        d.mkdir()
        for n in names:
            with wave.open(str(d / f"{n}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(b"\x00\x01" * 100)

    everything = load_filler_clips(tmp_path, sample_rate=SR)
    assert {k: len(v) for k, v in everything.items()} == {"en": 2, "tr": 1}

    picked = load_filler_clips(tmp_path, sample_rate=SR, only=["en/02.wav"])
    assert {k: len(v) for k, v in picked.items()} == {"en": 1}

    nothing = load_filler_clips(tmp_path, sample_rate=SR, only=[])
    assert nothing == {}
