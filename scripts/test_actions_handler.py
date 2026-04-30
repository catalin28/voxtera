"""Manual smoke test: build the create_ticket handler and exercise it end-to-end.

Run from the project root:

    uv run python scripts/test_actions_handler.py

What it does (no live LLM, no live voice loop):

1. Loads hotel config for "demo" via ``load_hotel_config``.
2. Builds the create_ticket FunctionSchema and prints it as a sanity check.
3. Builds the actions prompt fragment and prints its first few lines.
4. Builds a TelegramSink from .env (the same one Phase 1 verified).
5. Constructs a fake FunctionCallParams as if Claude had just called the tool
   with reasonable arguments.
6. Invokes the handler.
7. Reports whether a ticket landed in the Telegram channel and what result
   payload Claude would have received.

Expected outcome:
    * The schema dump shows the hotel's allowed categories in the enum.
    * The result payload reads ``status: filed`` with the session_id.
    * A formatted [Maintenance] message appears in the Telegram channel.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from voxtera.actions import (
    TelegramSink,
    build_actions_prompt_fragment,
    build_create_ticket_tool,
    load_hotel_config,
)
from voxtera.actions.handler import make_create_ticket_handler


class _FakeCallback:
    """Captures whatever the handler would feed back to Claude."""

    def __init__(self) -> None:
        self.captured: list[Any] = []

    async def __call__(self, result: Any, *, properties: Any = None) -> None:
        self.captured.append(result)


class _FakeParams:
    """Stand-in for pipecat.services.llm_service.FunctionCallParams."""

    def __init__(self, args: dict[str, Any], cb: _FakeCallback) -> None:
        self.arguments = args
        self.result_callback = cb


async def _run() -> int:
    load_dotenv()
    cfg = load_hotel_config("demo")
    logger.info("Hotel: {!r}, official language: {}", cfg.hotel_name, cfg.official_language)

    schema = build_create_ticket_tool(cfg)
    logger.info("Tool schema (first 600 chars):")
    print(json.dumps(schema.to_default_dict(), indent=2)[:600] + "\n...")

    fragment = build_actions_prompt_fragment(cfg)
    logger.info("Prompt fragment (first 300 chars):")
    print(fragment[:300] + "...\n")

    sink = TelegramSink.from_env()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)

    args = {
        "category": "Maintenance",
        "summary": "AC not cooling in room 412 since last night.",
        "room_number": "412",
        "original_quote": "La climatisation ne fonctionne pas depuis hier soir.",
        "language_detected": "French",
    }
    cb = _FakeCallback()
    params = _FakeParams(args, cb)

    logger.info("Invoking handler as if Claude just called create_ticket...")
    await handler(params)  # type: ignore[arg-type]

    if not cb.captured:
        logger.error("✗ Handler returned no result. Something is broken.")
        return 1

    result = cb.captured[0]
    logger.info("Result payload Claude would receive: {}", result)
    if result.get("status") == "filed":
        logger.info("✓ Ticket filed. Check the Telegram channel for a [Maintenance] post.")
        return 0
    logger.error("✗ Ticket NOT filed. status={}", result.get("status"))
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
