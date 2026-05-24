"""Tests for ``voxtera.call_record`` — the per-call conversation record.

These exercise the data layer (:class:`CallRecord`) and the WAV writer
directly against a ``tmp_path`` base directory, so nothing touches the repo's
real ``logs/`` tree. The :class:`CallAudioRecorder` pipeline processor is
covered only at the construction level — its audio buffering is the base
``AudioBufferProcessor``'s responsibility and is exercised end-to-end by the
pipeline itself.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

from voxtera.call_record import (
    RECORDING_CHANNELS,
    RECORDING_SAMPLE_RATE,
    SCHEMA_VERSION,
    CallAudioRecorder,
    CallRecord,
    write_wav,
)

# ---------- helpers ----------


def _record(tmp_path: Path, *, enabled: bool = True) -> CallRecord:
    """A started CallRecord writing under an isolated tmp directory."""
    rec = CallRecord(base_dir=tmp_path)
    rec.start(
        enabled=enabled,
        session_id="sess-123",
        hotel_id="grand-hotel",
        bot_name="Voxtera",
        transport_mode="daily",
        providers={"stt": "whisper", "tts": "google", "llm": "claude-haiku"},
    )
    return rec


def _read_record_json(tmp_path: Path, session_id: str = "sess-123") -> dict:
    """Load the record.json a CallRecord wrote for a session."""
    path = tmp_path / session_id / "record.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- write_wav ----------


class TestWriteWav:
    def test_writes_playable_wav_and_returns_duration(self, tmp_path: Path) -> None:
        """One second of 16 kHz stereo PCM round-trips to a valid WAV."""
        # 1s stereo @ 16 kHz, 16-bit: 16000 frames * 2 ch * 2 bytes.
        pcm = b"\x00\x00" * (16000 * 2)
        wav_path = tmp_path / "out.wav"

        duration = write_wav(wav_path, pcm, sample_rate=16000, num_channels=2)

        assert duration == 1.0
        with wave.open(str(wav_path), "rb") as wav_file:
            assert wav_file.getnchannels() == 2
            assert wav_file.getframerate() == 16000
            assert wav_file.getsampwidth() == 2
            assert wav_file.getnframes() == 16000

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        """write_wav makes the call directory if it does not exist yet."""
        wav_path = tmp_path / "calls" / "sess-9" / "recording.wav"
        write_wav(wav_path, b"\x00\x00" * 100, sample_rate=16000, num_channels=1)
        assert wav_path.exists()

    def test_empty_pcm_yields_zero_duration(self, tmp_path: Path) -> None:
        """An empty buffer still produces a (zero-length) WAV, not a crash."""
        duration = write_wav(tmp_path / "e.wav", b"", sample_rate=16000, num_channels=2)
        assert duration == 0.0


# ---------- CallRecord lifecycle ----------


class TestCallRecordLifecycle:
    def test_call_dir_is_none_before_start(self, tmp_path: Path) -> None:
        """No session id => no call directory."""
        assert CallRecord(base_dir=tmp_path).call_dir() is None

    def test_start_writes_record_json_immediately(self, tmp_path: Path) -> None:
        """record.json exists right after start(), before any turn."""
        _record(tmp_path)
        data = _read_record_json(tmp_path)
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["session_id"] == "sess-123"
        assert data["hotel_id"] == "grand-hotel"
        assert data["transport_mode"] == "daily"
        assert data["providers"] == {"stt": "whisper", "tts": "google", "llm": "claude-haiku"}
        assert data["started_at"] is not None
        assert data["ended_at"] is None
        assert data["turns"] == []

    def test_start_is_idempotent(self, tmp_path: Path) -> None:
        """A second start() does not reset an in-progress call."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="hello", language="en")
        rec.start(
            enabled=True,
            session_id="different",
            hotel_id="other",
            bot_name="Other",
            transport_mode="local",
            providers={},
        )
        assert rec.call_dir() == tmp_path / "sess-123"
        assert len(rec.snapshot()["turns"]) == 1

    def test_finalize_stamps_end_time(self, tmp_path: Path) -> None:
        """finalize() sets ended_at and a non-negative duration."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="hi", language="en")
        rec.finalize()
        data = _read_record_json(tmp_path)
        assert data["ended_at"] is not None
        assert data["duration_secs"] is not None
        assert data["duration_secs"] >= 0.0


# ---------- transcript turns ----------


class TestTranscriptTurns:
    def test_turns_recorded_in_order_with_shared_counter(self, tmp_path: Path) -> None:
        """User and bot turns share one sequential turn_id counter."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="Where is the spa?", language="en")
        rec.add_bot_turn(text="On the third floor.", latency_ms=812.4)
        rec.add_user_turn(text="Merci", language="fr")

        turns = rec.snapshot()["turns"]
        assert [t["turn_id"] for t in turns] == [1, 2, 3]
        assert [t["role"] for t in turns] == ["user", "bot", "user"]
        assert turns[0]["text"] == "Where is the spa?"
        assert turns[0]["language"] == "en"
        assert turns[1]["latency_ms"] == 812.4
        assert turns[1]["language"] is None

    def test_blank_turns_are_ignored(self, tmp_path: Path) -> None:
        """Empty / whitespace-only text never creates a turn."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="   ", language="en")
        rec.add_bot_turn(text="", latency_ms=10.0)
        assert rec.snapshot()["turns"] == []

    def test_record_json_rewritten_after_each_turn(self, tmp_path: Path) -> None:
        """Each turn flushes to disk, so a killed process keeps its record."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="first", language="en")
        assert len(_read_record_json(tmp_path)["turns"]) == 1
        rec.add_bot_turn(text="reply", latency_ms=100.0)
        assert len(_read_record_json(tmp_path)["turns"]) == 2


# ---------- metadata counters ----------


class TestMetadataCounters:
    def test_languages_collect_unique_codes(self, tmp_path: Path) -> None:
        """Every distinct detected language is collected, once."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="hello", language="en")
        rec.add_user_turn(text="bonjour", language="fr")
        rec.add_user_turn(text="hi again", language="en")
        assert rec.snapshot()["languages"] == ["en", "fr"]

    def test_turn_and_interruption_counts(self, tmp_path: Path) -> None:
        """user_turns / bot_turns / interruptions are tallied on the record."""
        rec = _record(tmp_path)
        rec.add_user_turn(text="q1", language="en")
        rec.add_user_turn(text="q2", language="en")
        rec.add_bot_turn(text="a1", latency_ms=50.0)
        rec.add_interruption()
        rec.add_interruption()

        metrics = rec.snapshot()["metrics"]
        assert metrics["user_turns"] == 2
        assert metrics["bot_turns"] == 1
        assert metrics["interruptions"] == 2

    def test_bot_character_total_sums_reply_lengths(self, tmp_path: Path) -> None:
        """bot_character_total is the Chirp 3 HD billing proxy."""
        rec = _record(tmp_path)
        rec.add_bot_turn(text="12345", latency_ms=1.0)  # 5 chars
        rec.add_bot_turn(text="abc", latency_ms=1.0)  # 3 chars
        assert rec.snapshot()["metrics"]["bot_character_total"] == 8

    def test_usage_accumulates_across_turns(self, tmp_path: Path) -> None:
        """LLM token usage sums into the call totals."""
        rec = _record(tmp_path)
        rec.add_usage(prompt_tokens=100, completion_tokens=20, cache_read_tokens=80)
        rec.add_usage(prompt_tokens=150, completion_tokens=30, cache_creation_tokens=40)

        metrics = rec.snapshot()["metrics"]
        assert metrics["llm_prompt_tokens"] == 250
        assert metrics["llm_completion_tokens"] == 50
        assert metrics["llm_cache_read_tokens"] == 80
        assert metrics["llm_cache_creation_tokens"] == 40

    def test_set_audio_populates_audio_block(self, tmp_path: Path) -> None:
        """set_audio records the WAV pointer and channel layout."""
        rec = _record(tmp_path)
        rec.set_audio(file_name="recording.wav", duration_secs=42.567)
        audio = rec.snapshot()["audio"]
        assert audio["file"] == "recording.wav"
        assert audio["duration_secs"] == 42.57
        assert audio["channels"] == RECORDING_CHANNELS
        assert audio["sample_rate"] == RECORDING_SAMPLE_RATE
        assert "guest" in audio["layout"]

    def test_audio_block_absent_until_set(self, tmp_path: Path) -> None:
        """A call with no audio has a null audio block, not a crash."""
        assert _record(tmp_path).snapshot()["audio"] is None


# ---------- disabled mode ----------


class TestDisabledMode:
    def test_disabled_record_writes_nothing(self, tmp_path: Path) -> None:
        """enabled=False => no directory, no file, mutators are no-ops."""
        rec = _record(tmp_path, enabled=False)
        rec.add_user_turn(text="hello", language="en")
        rec.add_bot_turn(text="hi", latency_ms=10.0)
        rec.add_interruption()
        rec.finalize()

        assert rec.enabled is False
        assert not (tmp_path / "sess-123").exists()
        assert rec.snapshot()["turns"] == []


# ---------- CallAudioRecorder ----------


class TestCallAudioRecorder:
    def test_constructs_as_stereo_processor(self) -> None:
        """The recorder is a 2-channel AudioBufferProcessor."""
        recorder = CallAudioRecorder()
        assert recorder.num_channels == RECORDING_CHANNELS
