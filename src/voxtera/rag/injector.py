"""RAGContextInjector — Pipecat FrameProcessor that enriches LLM context with RAG results.

Intercepts every ``LLMContextFrame`` flowing downstream, extracts the latest
user message, queries the retriever, and (if results are found) appends the
relevant hotel excerpts to that user message.  The LLM therefore "sees" the
excerpts as contextual information attached to the question they answer.

Why append to the user message rather than inject a separate system message:
a separate system message that is rebuilt every turn changes the cached
prompt prefix on every turn, which makes Anthropic prompt caching miss 100%
of the time (0 cache reads, full re-prefill each turn). By baking each
turn's excerpts into that turn's user message and never rewriting earlier
turns, the conversation history stays byte-stable — so the cached prefix
holds and caching actually hits.

Safety guarantees:
* On retrieval timeout: log WARNING, push context unmodified.
* On any exception: log ERROR, push context unmodified.
* Idempotent: never injects twice into the same user message.
* Never raises — the voice pipeline must keep flowing.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voxtera.conversation_logger import log_rag_context
from voxtera.rag.retriever import Retriever

_RAG_PREAMBLE = (
    "Here are relevant excerpts from the hotel's information. Use them when "
    "answering, but only if they're relevant to the user's most recent "
    "question. If they don't answer that question, ignore them. Do not "
    "answer earlier questions unless the user asks again."
)


class RAGContextInjector(FrameProcessor):
    """Enriches ``LLMContextFrame`` with chunks retrieved from the hotel knowledge base."""

    def __init__(
        self,
        retriever: Retriever,
        *,
        hotel_id: str,
        retrieval_timeout_ms: int = 5000,
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
        """Retrieve chunks and append them to the current user message.

        The excerpts are appended to the *latest* user message, not inserted
        as a separate system message. Earlier turns are left byte-for-byte
        untouched, so the cached prompt prefix stays stable and Anthropic
        prompt caching can hit.
        """
        context = frame.context
        messages = context.messages

        # Find the latest user message (index + content).
        user_idx: int | None = None
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if isinstance(msg, dict) and msg.get("role") == "user":
                user_idx = i
                break

        if user_idx is None:
            return

        user_msg = messages[user_idx]
        content = user_msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return

        # Idempotency guard: if this message already carries excerpts (e.g.
        # the context frame is reprocessed), do not inject again. The
        # preamble string is the marker.
        if _RAG_PREAMBLE in content:
            return

        user_text = content

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

        # Structured log for audit / evaluation.
        log_rag_context(
            hotel_id=self._hotel_id,
            user_query=user_text,
            chunks=[
                {
                    "doc_id": r.doc_id,
                    "score": round(r.score, 4),
                    "text": r.text[:300],
                }
                for r in results
            ],
            elapsed_ms=elapsed_ms,
        )

        if not results:
            return

        # Build the excerpt block and append it to the current user message.
        # ONLY this message is rewritten — every earlier turn stays immutable
        # so the cached prefix holds turn-to-turn (see module docstring).
        parts = [_RAG_PREAMBLE, ""]
        for chunk in results:
            parts.append(chunk.text)
            parts.append("")
        rag_block = "\n".join(parts).strip()

        messages[user_idx] = {
            **user_msg,
            "content": f"{content}\n\n{rag_block}",
        }
