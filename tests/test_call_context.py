"""Acceptance tests for per-call state isolation (CallContext).

The P0 architecture fix: the WhatsApp service hosts MANY calls in ONE
process, so the old process-global ``call_record`` singleton and trace
``TurnTracker`` mixed WAVs, transcripts, and trace session ids between
simultaneous callers. These tests prove the :class:`CallContext` refactor
isolates two truly concurrent fake calls — and that the legacy single-call
paths (no explicit context) still work via the process-default fallback.
"""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

from voxtera import call_context, call_record
from voxtera.trace import TraceEvent, TraceForwarder, tracker

# ---------- helpers ----------


def _start_call(ctx: call_context.CallContext) -> None:
    """Init the call record for a fake call through the module facade."""
    call_record.init_call(
        enabled=True,
        hotel_id=None,
        bot_name="Voxtera",
        transport_mode="whatsapp",
        stt_provider="gladia",
        tts_provider="google",
        llm_model="claude-test",
        context=ctx,
    )


def _read_record(base: Path, session_id: str) -> dict:
    return json.loads((base / session_id / "record.json").read_text(encoding="utf-8"))


# ---------- the P0 acceptance test ----------


async def test_two_concurrent_calls_no_cross_talk(tmp_path: Path) -> None:
    """Two simultaneous calls produce two clean logs/calls/<sid>/ folders.

    Each fake call activates its own CallContext, then records interleaved
    turns through the MODULE-LEVEL facades (exactly what the pipeline
    processors call), yielding between steps so the two calls genuinely
    interleave on the event loop.
    """
    sids = ("wacall_aaa", "wacall_bbb")

    async def fake_call(sid: str, turns: list[tuple[str, str]]) -> None:
        ctx = call_context.new_call_context(session_id=sid, channel="wa", base_dir=tmp_path)
        call_context.activate(ctx)
        _start_call(ctx)
        for user_text, bot_text in turns:
            await asyncio.sleep(0)  # force interleaving with the other call
            call_record.record_user_turn(text=user_text, language="en")
            await asyncio.sleep(0)
            call_record.record_bot_turn(text=bot_text, latency_ms=100.0)
            call_record.record_usage(prompt_tokens=10, completion_tokens=5)
            call_record.record_interruption()
        await asyncio.sleep(0)
        call_record.finalize(ctx)

    turns_a = [("hello from A", "reply A1"), ("more A", "reply A2")]
    turns_b = [("hi from B", "reply B1"), ("more B", "reply B2"), ("again B", "reply B3")]
    await asyncio.gather(fake_call(sids[0], turns_a), fake_call(sids[1], turns_b))

    # Two clean folders, nothing else.
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(sids)

    rec_a = _read_record(tmp_path, sids[0])
    rec_b = _read_record(tmp_path, sids[1])

    # Session metadata is per-call.
    assert rec_a["session_id"] == sids[0]
    assert rec_b["session_id"] == sids[1]
    assert rec_a["ended_at"] is not None
    assert rec_b["ended_at"] is not None

    # No cross-talk: every turn belongs to its own call, counts match.
    texts_a = [t["text"] for t in rec_a["turns"]]
    texts_b = [t["text"] for t in rec_b["turns"]]
    assert texts_a == ["hello from A", "reply A1", "more A", "reply A2"]
    assert texts_b == ["hi from B", "reply B1", "more B", "reply B2", "again B", "reply B3"]
    assert rec_a["metrics"]["user_turns"] == 2
    assert rec_b["metrics"]["user_turns"] == 3
    assert rec_a["metrics"]["interruptions"] == 2
    assert rec_b["metrics"]["interruptions"] == 3
    assert rec_a["metrics"]["llm_prompt_tokens"] == 20
    assert rec_b["metrics"]["llm_prompt_tokens"] == 30


async def test_concurrent_raw_input_wavs_stay_separate(tmp_path: Path) -> None:
    """Each call's RawInputRecorder writes its own input_raw.wav."""
    from pipecat.frames.frames import InputAudioRawFrame

    from voxtera.call_record import RawInputRecorder

    payloads = {"wacall_one": b"\x01\x01" * 800, "wacall_two": b"\x02\x02" * 1600}

    async def fake_call(sid: str, pcm: bytes) -> None:
        ctx = call_context.new_call_context(session_id=sid, channel="wa", base_dir=tmp_path)
        call_context.activate(ctx)
        _start_call(ctx)
        recorder = RawInputRecorder(sample_rate=16000, context=ctx)
        await asyncio.sleep(0)
        # Feed audio without running a full pipeline: buffer + flush directly.
        recorder._chunks.append(
            InputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1).audio
        )
        recorder._started = True
        await asyncio.sleep(0)
        await call_record.flush_raw_input(ctx)
        call_record.finalize(ctx)

    await asyncio.gather(*(fake_call(sid, pcm) for sid, pcm in payloads.items()))

    for sid, pcm in payloads.items():
        wav_path = tmp_path / sid / "input_raw.wav"
        assert wav_path.exists(), f"missing WAV for {sid}"
        with wave.open(str(wav_path), "rb") as wav:
            frames = wav.readframes(wav.getnframes())
        assert frames == pcm, f"WAV content mixed between calls for {sid}"


async def test_trace_tracker_is_per_context() -> None:
    """Concurrent calls get independent turn ids and timing anchors."""
    results: dict[str, list[str]] = {}

    async def fake_call(sid: str, n_turns: int) -> None:
        ctx = call_context.new_call_context(session_id=sid, channel="wa")
        call_context.activate(ctx)
        ids = []
        for _ in range(n_turns):
            await asyncio.sleep(0)
            ids.append(tracker().start_user_turn())
            tracker().stamp("user_stopped")
            await asyncio.sleep(0)
            assert tracker() is ctx.tracker  # facade resolves to OUR tracker
        results[sid] = ids

    await asyncio.gather(fake_call("s1", 2), fake_call("s2", 3))

    # Sequence counters are per-call (both start at 001), proving no sharing.
    # Turn ids only need uniqueness WITHIN a session: every trace event now
    # carries the call's session id, and the dashboard groups by session.
    assert [i.rsplit("-", 1)[1] for i in results["s1"]] == ["001", "002"]
    assert [i.rsplit("-", 1)[1] for i in results["s2"]] == ["001", "002", "003"]


def test_trace_forwarder_filters_foreign_sessions() -> None:
    """A forwarder ships only its own call's events (plus unstamped ones)."""
    fwd = TraceForwarder(launcher_url="http://x", session_id="mine")
    assert fwd._accepts(TraceEvent(kind="stage", source="t", session_id="mine"))
    assert fwd._accepts(TraceEvent(kind="stage", source="t", session_id=None))
    assert not fwd._accepts(TraceEvent(kind="stage", source="t", session_id="other"))


async def test_emit_stamps_active_session() -> None:
    """trace.emit tags events with the active call's session id."""
    from voxtera.trace import TraceBus, emit

    ctx = call_context.new_call_context(session_id="stamp-test", channel="wa")
    call_context.activate(ctx)
    try:
        emit("lifecycle", source="test", data={"event": "x"})
        last = TraceBus.instance().recent(limit=1)[0]
        assert last.session_id == "stamp-test"
    finally:
        call_context.deactivate()

    emit("lifecycle", source="test", data={"event": "y"})
    last = TraceBus.instance().recent(limit=1)[0]
    assert last.session_id is None


def test_default_context_fallback_for_single_call_paths(tmp_path: Path, monkeypatch) -> None:
    """Daily/local paths (no explicit context) keep the legacy behaviour."""
    fallback = call_context.CallContext(session_id="")
    fallback.record = call_record.CallRecord(base_dir=tmp_path)
    from voxtera.trace import TurnTracker

    fallback.tracker = TurnTracker()
    monkeypatch.setattr(call_context, "_default", fallback)
    monkeypatch.setattr("voxtera.launcher_client.SESSION_ID", "daily-sess")

    call_record.init_call(
        enabled=True,
        hotel_id="grand-hotel",
        bot_name="Voxtera",
        transport_mode="daily",
        stt_provider="whisper",
        tts_provider="google",
        llm_model="claude-test",
    )
    call_record.record_user_turn(text="hello")
    call_record.finalize()

    rec = _read_record(tmp_path, "daily-sess")
    assert rec["session_id"] == "daily-sess"  # launcher id resolved, as before
    assert [t["text"] for t in rec["turns"]] == ["hello"]
    assert call_record.get_call_dir() == tmp_path / "daily-sess"
