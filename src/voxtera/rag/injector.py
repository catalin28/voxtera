"""RAGContextInjector — Pipecat FrameProcessor that enriches LLM context with RAG results.

Intercepts every ``LLMContextFrame`` flowing downstream, extracts the latest
user message, queries the retriever, and (if results are found) prepends a
system message with the relevant hotel excerpts.  The LLM therefore "sees"
the excerpts as contextual information it can draw on.

Safety guarantees:
* On retrieval timeout: log WARNING, push context unmodified.
* On any exception: log ERROR, push context unmodified.
* Never raises — the voice pipeline must keep flowing.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voxtera.rag.retriever import Retriever

_RAG_PREAMBLE = (
    "Here are relevant excerpts from the hotel's information. Use them when "
    "answering, but only if they're relevant. If they don't answer the "
    "question, ignore them."
)


class RAGContextInjector(FrameProcessor):
    """Enriches ``LLMContextFrame`` with chunks retrieved from the hotel knowledge base."""

    def __init__(
        self,
        retriever: Retriever,
        *,
        hotel_id: str,
        retrieval_timeout_ms: int = 500,
    ) -> None:
        super().__init__()
        self._retriever = retriever
        self._hotel_id = hotel_id
        self._timeout = retrieval_timeout_ms / 1000.0  # convert to seconds

    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            await self._inject_context(frame)

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------

    async def _inject_context(self, frame: LLMContextFrame) -> None:
        """Retrieve chunks and prepend them as a system message."""
        context = frame.context
        messages = context.messages

        # Find the latest user message.
        user_text = ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_text = content
                break

        if not user_text:
            return

        t0 = time.monotonic()
        try:
            results = await asyncio.wait_for(
                self._retriever.retrieve(hotel_id=self._hotel_id, query=user_text),
                timeout=self._timeout,
            )
        except TimeoutError:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.warning("[rag] retrieval timed out after {:.0f}ms", elapsed_ms)
            return
        except Exception:
            logger.opt(exception=True).error("[rag] retrieval failed")
            return

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info("[rag] retrieved {} chunks in {:.0f}ms", len(results), elapsed_ms)

        if not results:
            return

        # Build a single system message with all retrieved excerpts.
        parts = [_RAG_PREAMBLE, ""]
        for chunk in results:
            parts.append(chunk.text)
            parts.append("")

        rag_message: dict[str, str] = {"role": "system", "content": "\n".join(parts).strip()}

        # Remove any previously injected RAG message (from a prior turn) so we
        # don't accumulate stale context across the conversation.
        messages[:] = [
            m for m in messages
            if not (isinstance(m, dict) and _RAG_PREAMBLE in str(m.get("content", "")))
        ]

        # Insert right after the first system message (the main system prompt)
        # so the LLM sees: system prompt → RAG excerpts → conversation history.
        insert_idx = 1
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                insert_idx = i + 1
                break

        messages.insert(insert_idx, rag_message)  # type: ignore[arg-type]
