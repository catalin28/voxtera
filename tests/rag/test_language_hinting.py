"""Focused tests for live RAG language hint wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.frames.frames import LLMContextFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from voxtera.rag import injector as injector_module
from voxtera.rag.injector import CurrentTurnLanguageTracker, RAGContextInjector


def _make_context_frame(messages: list[dict[str, str]]) -> LLMContextFrame:
    frame = MagicMock(spec=LLMContextFrame)
    frame.context = MagicMock()
    frame.context.messages = list(messages)
    frame.__class__ = LLMContextFrame
    return frame


class FakeDailyInputTransportMessageFrame:
    def __init__(self, message: dict[str, object]) -> None:
        self.message = message


@pytest.mark.asyncio
async def test_rag_injector_passes_language_hint_to_retriever() -> None:
    retriever = AsyncMock()
    retriever.retrieve = AsyncMock(return_value=[])
    injector = RAGContextInjector(
        retriever,
        hotel_id="demo",
        retrieval_timeout_ms=500,
        language_getter=lambda: "fr-FR",
    )
    frame = _make_context_frame(
        [
            {"role": "system", "content": "Prompt"},
            {"role": "user", "content": "Quels sont les horaires du spa ?"},
        ]
    )

    with patch.object(injector, "push_frame", new_callable=AsyncMock):
        await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

    retriever.retrieve.assert_awaited_once_with(
        hotel_id="demo",
        query="Quels sont les horaires du spa ?",
        language="fr-FR",
    )


@pytest.mark.asyncio
async def test_language_tracker_tracks_voice_and_clears_on_typed_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        injector_module,
        "DailyInputTransportMessageFrame",
        FakeDailyInputTransportMessageFrame,
    )
    tracker = CurrentTurnLanguageTracker()

    transcript = TranscriptionFrame(
        text="bonjour",
        user_id="guest",
        timestamp="2026-05-30T10:00:00Z",
    )
    transcript.language = "fr"

    with patch.object(tracker, "push_frame", new_callable=AsyncMock):
        await tracker.process_frame(transcript, FrameDirection.DOWNSTREAM)

    assert tracker.current_language == "fr"

    typed_turn = FakeDailyInputTransportMessageFrame(
        {"type": "voxtera-user-text", "text": "hello from keyboard"}
    )
    with patch.object(tracker, "push_frame", new_callable=AsyncMock):
        await tracker.process_frame(typed_turn, FrameDirection.DOWNSTREAM)

    assert tracker.current_language is None
