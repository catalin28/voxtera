"""Tests for RAGContextInjector FrameProcessor."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pipecat.processors.frame_processor import FrameDirection

from voxtera.rag.injector import _RAG_PREAMBLE, RAGContextInjector
from voxtera.rag.retriever import RetrievedChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(messages: list[dict[str, str]]) -> MagicMock:
    """Build a mock LLMContext with a real mutable messages list."""
    ctx = MagicMock()
    ctx.messages = list(messages)  # real list so insert() works
    return ctx


def _make_frame(messages: list[dict[str, str]]) -> MagicMock:
    """Build a mock LLMContextFrame wrapping the given messages."""
    frame = MagicMock()
    frame.context = _make_context(messages)
    # Make isinstance(frame, LLMContextFrame) work via __class__.
    from pipecat.frames.frames import LLMContextFrame

    frame.__class__ = LLMContextFrame
    return frame


def _chunks(*texts: str) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(text=t, score=0.9, doc_id="info.md", category=None)
        for t in texts
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRAGContextInjector:
    """Context-modification logic with a mock retriever."""

    @pytest.fixture()
    def retriever(self) -> AsyncMock:
        r = AsyncMock()
        r.retrieve = AsyncMock(return_value=[])
        return r

    @pytest.fixture()
    def injector(self, retriever: AsyncMock) -> RAGContextInjector:
        return RAGContextInjector(retriever, hotel_id="demo", retrieval_timeout_ms=500)

    # -- Happy path: chunks found ----------------------------------------

    async def test_injects_rag_system_message(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        retriever.retrieve.return_value = _chunks("Breakfast at 7am.", "Pool open 8-20.")

        frame = _make_frame(
            [
                {"role": "system", "content": "You are a hotel concierge."},
                {"role": "user", "content": "When is breakfast?"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock) as mock_push:

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        msgs = frame.context.messages
        # Excerpts are appended to the current user message (kept stable for
        # Anthropic prompt caching — see injector.py module docstring), not
        # inserted as a separate system message. So the message count is
        # unchanged; only the user message content grows.
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert _RAG_PREAMBLE in msgs[1]["content"]
        assert "When is breakfast?" in msgs[1]["content"]
        assert "Breakfast at 7am." in msgs[1]["content"]
        assert "Pool open 8-20." in msgs[1]["content"]
        mock_push.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)

    # -- No chunks returned â†’ context unchanged -------------------------

    async def test_no_chunks_leaves_context_unchanged(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        retriever.retrieve.return_value = []

        frame = _make_frame(
            [
                {"role": "system", "content": "You are a hotel concierge."},
                {"role": "user", "content": "Hello"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock) as mock_push:

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        assert len(frame.context.messages) == 2
        mock_push.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)

    # -- No user message â†’ nothing happens ------------------------------

    async def test_no_user_message_skips_retrieval(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        frame = _make_frame([{"role": "system", "content": "Prompt"}])

        with patch.object(injector, "push_frame", new_callable=AsyncMock):

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        retriever.retrieve.assert_not_called()
        assert len(frame.context.messages) == 1

    # -- Retrieval timeout â†’ context unmodified, WARNING logged ---------

    async def test_retrieval_timeout_passes_context_unmodified(
        self, retriever: AsyncMock
    ) -> None:
        async def _slow(**kwargs: object) -> list[RetrievedChunk]:
            await asyncio.sleep(5)
            return []

        retriever.retrieve.side_effect = _slow

        injector = RAGContextInjector(
            retriever, hotel_id="demo", retrieval_timeout_ms=50
        )
        frame = _make_frame(
            [
                {"role": "system", "content": "Prompt"},
                {"role": "user", "content": "Hi"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock):

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        assert len(frame.context.messages) == 2

    # -- Retrieval exception â†’ context unmodified, ERROR logged ---------

    async def test_retrieval_exception_passes_context_unmodified(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        retriever.retrieve.side_effect = RuntimeError("embed api down")

        frame = _make_frame(
            [
                {"role": "system", "content": "Prompt"},
                {"role": "user", "content": "Hi"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock):

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        assert len(frame.context.messages) == 2

    # -- Upstream frames pass through untouched -------------------------

    async def test_upstream_frame_passes_through(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        frame = _make_frame(
            [
                {"role": "system", "content": "Prompt"},
                {"role": "user", "content": "Hi"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock):

            await injector.process_frame(frame, FrameDirection.UPSTREAM)

        # Retrieval should NOT be called for upstream frames.
        retriever.retrieve.assert_not_called()
        assert len(frame.context.messages) == 2

    # -- Retriever called with correct args -----------------------------

    async def test_retriever_receives_hotel_id_and_query(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        retriever.retrieve.return_value = []

        frame = _make_frame(
            [
                {"role": "system", "content": "Prompt"},
                {"role": "user", "content": "pool hours?"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock):

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        # ``language`` is None when no language_getter is wired.
        retriever.retrieve.assert_awaited_once_with(
            hotel_id="demo", query="pool hours?", language=None
        )

    # -- RAG excerpts attach to the LATEST user message -----------------

    async def test_rag_inserted_after_system_prompt(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        retriever.retrieve.return_value = _chunks("Spa 9am-9pm.")

        frame = _make_frame(
            [
                {"role": "system", "content": "You are a concierge."},
                {"role": "user", "content": "Massage?"},
                {"role": "assistant", "content": "Let me check."},
                {"role": "user", "content": "Spa hours?"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock):

            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        msgs = frame.context.messages
        # Count unchanged: excerpts attach to the LATEST user message so the
        # cached prefix (everything before the new turn) stays byte-stable.
        assert len(msgs) == 4
        assert msgs[0]["role"] == "system"  # original system prompt
        assert msgs[1]["role"] == "user"  # earlier turn left untouched
        assert msgs[1]["content"] == "Massage?"
        assert msgs[2]["role"] == "assistant"
        assert msgs[3]["role"] == "user"  # latest user message carries excerpts
        assert "Spa hours?" in msgs[3]["content"]
        assert _RAG_PREAMBLE in msgs[3]["content"]
        assert "Spa 9am-9pm." in msgs[3]["content"]

    # -- Multi-turn: per-turn excerpts attach to that turn's user message --

    async def test_second_turn_replaces_previous_rag_message(
        self, injector: RAGContextInjector, retriever: AsyncMock
    ) -> None:
        """Two turns: turn 1 excerpts stay on turn 1's user message; turn 2
        excerpts attach to turn 2's user message. Earlier turns are NOT
        rewritten — that's what keeps the Anthropic prompt-cache prefix
        stable across turns (see injector.py module docstring)."""
        # Turn 1
        retriever.retrieve.return_value = _chunks("Breakfast at 7am.")
        frame = _make_frame(
            [
                {"role": "system", "content": "You are a concierge."},
                {"role": "user", "content": "breakfast?"},
            ]
        )

        with patch.object(injector, "push_frame", new_callable=AsyncMock):
            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        assert len(frame.context.messages) == 2  # sys + user (excerpts in user)
        assert "Breakfast at 7am." in frame.context.messages[1]["content"]

        # Simulate turn 2: assistant replied, user asks again.
        frame.context.messages.append({"role": "assistant", "content": "7am."})
        frame.context.messages.append({"role": "user", "content": "pool?"})
        retriever.retrieve.return_value = _chunks("Pool open 8-20.")

        with patch.object(injector, "push_frame", new_callable=AsyncMock):
            await injector.process_frame(frame, FrameDirection.DOWNSTREAM)

        msgs = frame.context.messages
        # Turn 1 user message is byte-stable: still carries turn-1 excerpts.
        assert "Breakfast at 7am." in msgs[1]["content"]
        assert "Pool open 8-20." not in msgs[1]["content"]
        # Turn 2 user message carries turn-2 excerpts.
        assert msgs[-1]["role"] == "user"
        assert "pool?" in msgs[-1]["content"]
        assert "Pool open 8-20." in msgs[-1]["content"]
        assert "Breakfast at 7am." not in msgs[-1]["content"]
