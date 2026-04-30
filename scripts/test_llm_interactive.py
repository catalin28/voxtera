"""Interactive LLM test for the actions feature — text mode, real Claude.

Run from the project root::

    uv run python scripts/test_llm_interactive.py [hotel_id]

This is a text-mode chat that uses **real Claude** via the Anthropic API,
loaded with the same system prompt and ``create_ticket`` tool that the live
bot would use. The voice layer (STT, TTS, Pipecat pipeline) is bypassed
entirely. The Telegram sink IS real — confirmed tickets land in your demo
channel exactly as they would during a voice call.

What this validates that the smoke test does NOT:

- Whether the prompt makes Claude ask for missing info (e.g. room number).
- Whether Claude follows the confirmation rule before filing.
- Whether Claude picks the right category for a given complaint.
- Whether Claude translates the summary into the hotel's staff language.
- Multi-turn behaviour across a real conversation.

What this still does NOT validate:

- Voice loop, STT accuracy, TTS naturalness, latency, interruptions.
- Pipecat's frame plumbing (LLMContext, aggregators, transports).

Hooks for the future "chat mode" the user mentioned: this script's
``_chat_turn`` function is the conversational core. It can be lifted into a
Pipecat input source or a web endpoint with minimal change — input/output
plumbing is the only thing that varies.

Type ``quit``, ``exit``, or ``bye`` to end the session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from loguru import logger

from voxtera.actions import (
    TelegramSink,
    build_actions_prompt_fragment,
    build_create_ticket_tool,
    load_hotel_config,
)
from voxtera.actions.handler import make_create_ticket_handler
from voxtera.controllers import LLM_MODEL
from voxtera.prompts import SYSTEM_PROMPT

# RAG block prefix — kept identical to the live `RAGContextInjector` so any
# logs / debugging crossover between this script and a real call stays
# unambiguous.
_RAG_PREAMBLE = (
    "Here are relevant excerpts from the hotel's information. Use them when "
    "answering, but only if they're relevant to the user's most recent "
    "question. If they don't answer that question, ignore them. Do not "
    "answer earlier questions unless the user asks again."
)

# ANSI escape codes — make the terminal output easier to follow at a glance.
_RESET = "\x1b[0m"
_GREEN = "\x1b[1;32m"
_BLUE = "\x1b[1;34m"
_YELLOW = "\x1b[1;33m"
_DIM = "\x1b[2m"

# Per-call response budget. Voice replies are short anyway; bigger doesn't help.
_MAX_TOKENS = 1024


def _schema_to_anthropic_tool(schema: Any) -> dict[str, Any]:
    """Convert a Pipecat FunctionSchema into Anthropic's ``tools`` payload format.

    Pipecat's ``to_default_dict()`` produces the OpenAI-style shape with
    ``parameters``. Anthropic's API expects the same JSON Schema body but
    nested under ``input_schema`` rather than ``parameters``. The contents
    inside (type/properties/required) are identical.
    """
    return {
        "name": schema.name,
        "description": schema.description,
        "input_schema": {
            "type": "object",
            "properties": schema.properties,
            "required": schema.required,
        },
    }


class _FakeCallback:
    """Captures the dict our handler sends back to (would-be) Pipecat."""

    def __init__(self) -> None:
        self.captured: list[Any] = []

    async def __call__(self, result: Any, *, properties: Any = None) -> None:
        self.captured.append(result)


class _FakeParams:
    """Stand-in for pipecat.services.llm_service.FunctionCallParams."""

    def __init__(self, args: dict[str, Any]) -> None:
        self.arguments = args
        self.result_callback = _FakeCallback()


async def _run_tool(handler: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke the handler and return whatever it would have told the LLM."""
    params = _FakeParams(args)
    await handler(params)
    if params.result_callback.captured:
        result = params.result_callback.captured[0]
        return result if isinstance(result, dict) else {"status": "unknown", "raw": str(result)}
    return {"status": "failed", "reason": "handler returned no result"}


def _build_retriever(hotel_id: str) -> Any | None:
    """Build the same Retriever the live bot uses, or return None on failure.

    Returning None instead of raising lets the script keep working even when
    RAG is misconfigured — answers will just lack hotel knowledge, but the
    actions feature itself can still be exercised.
    """
    try:
        from voxtera.rag.embeddings import embed_sync
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore
    except Exception:
        logger.opt(exception=True).warning(
            "[rag] could not import RAG modules — continuing without RAG"
        )
        return None

    default_db = str(Path.home() / ".voxtera" / "voxtera.db")
    db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
    if not db_path.exists():
        logger.warning(
            "[rag] db not found at {} — continuing without RAG. "
            "Run the ingest pipeline (see docs/rag-implementation-plan.md) to populate it.",
            db_path,
        )
        return None

    try:
        # Warm up the embedding model so the first query doesn't pay the
        # cold-start cost mid-conversation.
        logger.info("[rag] loading embedding model (one-time, ~5s)...")
        embed_sync(["warmup"])
        store = ChunksStore(db_path)
        store.init_schema()
        retriever = Retriever(store)
        logger.info("[rag] ready — hotel_id={}", hotel_id)
        return retriever
    except Exception:
        logger.opt(exception=True).warning("[rag] init failed — continuing without RAG")
        return None


async def _retrieve_rag_block(retriever: Any | None, hotel_id: str, query: str) -> str | None:
    """Run retrieval for ``query`` and format the chunks into a system block.

    Returns ``None`` if RAG is disabled, the query is empty, retrieval times
    out, or no chunks score above the retriever's threshold. Mirrors the
    behaviour of :class:`voxtera.rag.injector.RAGContextInjector`.
    """
    if retriever is None or not query.strip():
        return None

    try:
        results = await asyncio.wait_for(
            retriever.retrieve(hotel_id=hotel_id, query=query),
            timeout=5.0,
        )
    except TimeoutError:
        logger.warning("[rag] retrieval timed out")
        return None
    except Exception:
        logger.opt(exception=True).error("[rag] retrieval failed")
        return None

    if not results:
        return None

    parts = [_RAG_PREAMBLE, ""]
    for r in results:
        parts.append(r.text)
        parts.append("")
    logger.info("[rag] injected {} chunks (top score {:.3f})", len(results), results[0].score)
    return "\n".join(parts).strip()


async def _chat_turn(
    *,
    client: AsyncAnthropic,
    model: str,
    base_system: str,
    rag_block: str | None,
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    handler: Any,
) -> None:
    """Run one user-turn end-to-end.

    Loops while Claude keeps requesting tool calls. Mutates ``messages`` in
    place so the conversation history stays in sync with what the model sees.

    The ``rag_block`` is appended to ``base_system`` for this turn only; it
    is not retained across turns. This matches the live bot, where RAG fires
    fresh for every user turn rather than accumulating chunks.
    """
    system = base_system if not rag_block else base_system + "\n\n" + rag_block

    while True:
        response = await client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
        )

        # Add the assistant turn to history. We pass content blocks back to
        # the API verbatim on the next turn, which keeps tool_use/tool_result
        # IDs aligned without us having to re-serialize them.
        messages.append({"role": "assistant", "content": response.content})

        any_tool_use = False
        tool_results_for_next_turn: list[dict[str, Any]] = []

        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", "")
                print(f"{_BLUE}BOT:{_RESET} {text}")
            elif block_type == "tool_use":
                any_tool_use = True
                tool_name = getattr(block, "name", "?")
                tool_input = getattr(block, "input", {}) or {}
                print(
                    f"{_YELLOW}[ACTION] Claude calling {tool_name} with args:{_RESET}\n"
                    f"  {json.dumps(tool_input, ensure_ascii=False, indent=2)}"
                )
                result = await _run_tool(handler, dict(tool_input))
                print(f"{_YELLOW}[ACTION] Result fed back to Claude:{_RESET} {result}")
                tool_results_for_next_turn.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": getattr(block, "id", ""),
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        # If Claude wants tools run, hand back the results and let it speak again.
        if any_tool_use and response.stop_reason == "tool_use":
            messages.append({"role": "user", "content": tool_results_for_next_turn})
            continue
        # Otherwise the assistant turn is done.
        return


async def _main() -> int:
    load_dotenv()

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY not set in .env — aborting.")
        return 1

    hotel_id = sys.argv[1] if len(sys.argv) > 1 else "demo"
    try:
        cfg = load_hotel_config(hotel_id)
    except (FileNotFoundError, ValueError) as e:
        logger.error("Hotel config load failed: {}", e)
        return 1

    # Compose the bits the live bot would build.
    base_system = SYSTEM_PROMPT.rstrip() + "\n" + build_actions_prompt_fragment(cfg)
    schema = build_create_ticket_tool(cfg)
    tools = [_schema_to_anthropic_tool(schema)]
    sink = TelegramSink.from_env()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)

    # RAG: build the same retriever the live pipeline does, if a populated
    # DB exists. Failure here is non-fatal — the script just runs RAG-less.
    rag_disabled = os.getenv("RAG_DISABLED", "false").lower() == "true"
    retriever = None if rag_disabled else _build_retriever(cfg.hotel_id)

    # Banner so it's obvious which hotel, channel, and RAG state you're hitting.
    cats = ", ".join(c.value for c in cfg.allowed_categories)
    print(f"\n{_GREEN}=== Voxtera LLM-only chat ==={_RESET}")
    print(f"  Hotel:           {cfg.hotel_name}")
    print(f"  Staff language:  {cfg.official_language}")
    print(f"  Categories:      {cats}")
    print(f"  Telegram channel: {cfg.telegram_channel_id}")
    print(f"  Model:           {LLM_MODEL}")
    print(f"  RAG:             {'enabled' if retriever else 'disabled'}")
    print(f"  {_DIM}Type 'quit', 'exit', or 'bye' to end. Speak any language.{_RESET}\n")

    client = AsyncAnthropic()
    messages: list[dict[str, Any]] = []

    while True:
        try:
            # Run blocking input() on a worker thread so the event loop stays
            # responsive (matters once you wire this into a richer UI).
            line = await asyncio.to_thread(input, f"{_GREEN}YOU:{_RESET} ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        line = line.strip()
        if not line:
            continue
        if line.lower() in {"quit", "exit", "bye"}:
            print(f"{_DIM}Bye.{_RESET}")
            break

        messages.append({"role": "user", "content": line})

        # Compute the RAG block for THIS user turn only. We pass the latest
        # user message into the retriever; if there's no DB or no match, the
        # block is None and the system prompt is the base prompt only.
        rag_block = await _retrieve_rag_block(retriever, cfg.hotel_id, line)

        try:
            await _chat_turn(
                client=client,
                model=LLM_MODEL,
                base_system=base_system,
                rag_block=rag_block,
                tools=tools,
                messages=messages,
                handler=handler,
            )
        except Exception as e:
            logger.exception("Anthropic call or tool execution failed: {}", e)
            # Roll back the user message so the next attempt starts clean.
            if messages and messages[-1].get("role") == "user":
                messages.pop()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)
