"""Focused tests for the pre-LLM stage timing emits."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import (
    LLMContextFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

import voxtera.trace as trace_module
from voxtera.controllers import LLMRunGuard
from voxtera.observability import (
    ContextUserStageEndTimer,
    ContextUserStageStartTimer,
    _ContextUserStageState,
)
from voxtera.rag.injector import RAGContextInjector
from voxtera.time_context import TimeContextInjector


def _reset_trace_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trace_module, "_TRACKER", None)
    monkeypatch.setattr(trace_module.TraceBus, "_instance", None)


def _stage_events() -> list[dict]:
    return [
        event.to_dict()
        for event in trace_module.TraceBus.instance().recent()
        if event.kind == "stage"
    ]


def _make_context_frame(messages: list[dict[str, str]]) -> LLMContextFrame:
    frame = MagicMock(spec=LLMContextFrame)
    frame.context = MagicMock()
    frame.context.messages = list(messages)
    frame.__class__ = LLMContextFrame
    return frame


@pytest.mark.asyncio
async def test_ctx_user_stage_emits_on_matching_append(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    state = _ContextUserStageState()
    start = ContextUserStageStartTimer("voxtera", state=state)
    end = ContextUserStageEndTimer("voxtera", state=state)
    transcript = TranscriptionFrame(
        text="where is the spa",
        user_id="guest",
        timestamp="2026-05-30T10:00:00Z",
    )
    append = LLMMessagesAppendFrame([{"role": "user", "content": "where is the spa"}])

    with patch.object(start, "push_frame", new_callable=AsyncMock):
        await start.process_frame(transcript, FrameDirection.DOWNSTREAM)
    with patch.object(end, "push_frame", new_callable=AsyncMock):
        await end.process_frame(append, FrameDirection.DOWNSTREAM)

    assert any(event["data"].get("stage") == "ctx_user" for event in _stage_events())


@pytest.mark.asyncio
async def test_ctx_user_stage_ignores_non_matching_append(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    state = _ContextUserStageState()
    start = ContextUserStageStartTimer("voxtera", state=state)
    end = ContextUserStageEndTimer("voxtera", state=state)
    transcript = TranscriptionFrame(
        text="where is the spa",
        user_id="guest",
        timestamp="2026-05-30T10:00:00Z",
    )
    resume_note = LLMMessagesAppendFrame(
        [{"role": "user", "content": "Please continue your previous answer."}]
    )

    with patch.object(start, "push_frame", new_callable=AsyncMock):
        await start.process_frame(transcript, FrameDirection.DOWNSTREAM)
    with patch.object(end, "push_frame", new_callable=AsyncMock):
        await end.process_frame(resume_note, FrameDirection.DOWNSTREAM)

    assert not any(event["data"].get("stage") == "ctx_user" for event in _stage_events())


@pytest.mark.asyncio
async def test_llm_run_guard_emits_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    guard = LLMRunGuard(max_age_secs=10.0, min_run_interval_secs=0.0)
    append = LLMMessagesAppendFrame([{"role": "user", "content": "hello"}])

    with patch.object(guard, "push_frame", new_callable=AsyncMock):
        await guard.process_frame(append, FrameDirection.DOWNSTREAM)
    with patch.object(guard, "push_frame", new_callable=AsyncMock):
        await guard.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

    assert any(event["data"].get("stage") == "llm_run_guard" for event in _stage_events())


@pytest.mark.asyncio
async def test_rag_injector_emits_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    retriever = AsyncMock()
    retriever.retrieve = AsyncMock(return_value=[])
    injector = RAGContextInjector(retriever, hotel_id="demo", retrieval_timeout_ms=500)
    frame = _make_context_frame(
        [
            {"role": "system", "content": "Prompt"},
            {"role": "user", "content": "spa hours?"},
        ]
    )

    with patch.object(injector, "push_frame", new_callable=AsyncMock):
        await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert any(event["data"].get("stage") == "rag_retrieve" for event in _stage_events())


@pytest.mark.asyncio
async def test_time_context_emits_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_trace_state(monkeypatch)
    trace_module.tracker().start_user_turn()

    injector = TimeContextInjector()
    frame = _make_context_frame(
        [
            {"role": "system", "content": "Prompt"},
            {"role": "user", "content": "is the spa still open?"},
        ]
    )

    with patch.object(injector, "push_frame", new_callable=AsyncMock):
        await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert any(event["data"].get("stage") == "time_context" for event in _stage_events())
