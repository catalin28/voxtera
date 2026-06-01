"""Focused tests for the STT stage tracing helpers."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

import voxtera.trace as trace_module
from voxtera.observability import TranscriptStageTimer
from voxtera.stt import _emit_gladia_final_metrics


def _reset_trace_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace_module, "_TRACKER", None)
    monkeypatch.setattr(trace_module.TraceBus, "_instance", None)


def _stage_names() -> list[str]:
    return [
        str(event.to_dict().get("data", {}).get("stage"))
        for event in trace_module.TraceBus.instance().recent()
        if event.kind == "stage"
    ]


def _transcript(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="guest", timestamp="2026-05-30T10:00:00Z")


@pytest.mark.asyncio
async def test_gladia_final_metrics_emit_provider_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    now = time.monotonic()
    first_audio_ms, provider_tail_ms = _emit_gladia_final_metrics(
        now=now,
        first_audio_at=now - 1.0,
        user_stopped_at=now - 0.5,
    )

    assert first_audio_ms == 1000
    assert provider_tail_ms == 500
    stage_names = _stage_names()
    assert "stt_first_audio_to_final" in stage_names
    assert "stt_provider_tail" in stage_names


@pytest.mark.asyncio
async def test_transcript_stage_emits_post_provider_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    now = time.monotonic()
    _emit_gladia_final_metrics(
        now=now,
        first_audio_at=now - 1.0,
        user_stopped_at=now - 0.4,
    )
    timer = TranscriptStageTimer("voxtera")

    with (
        patch("voxtera.observability.log_user_query"),
        patch("voxtera.observability.record_user_turn"),
        patch.object(timer, "push_frame", new_callable=AsyncMock),
    ):
        await timer.process_frame(_transcript("hello"), FrameDirection.DOWNSTREAM)

    assert "stt_post_provider" in _stage_names()
