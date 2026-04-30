"""Manual smoke test: send a fake ticket to Telegram and verify it lands.

Run from the project root:

    uv run python scripts/test_telegram_sink.py

Requires ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHANNEL_ID`` in ``.env``.

Expected outcome:
    * ``✓ Ticket posted.`` printed to the terminal.
    * A formatted [Maintenance] message appears in your demo channel.
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger

from voxtera.actions import Category, TelegramSink, Ticket


async def _run() -> int:
    load_dotenv()
    sink = TelegramSink.from_env()
    ticket = Ticket(
        category=Category.MAINTENANCE,
        summary="AC not cooling since last night.",
        room_number="412",
        original_quote="La climatisation ne fonctionne pas depuis hier soir.",
        language_detected="French",
    )
    logger.info("Sending fake maintenance ticket to Telegram...")
    ok = await sink.send(ticket)
    if ok:
        logger.info("✓ Ticket posted. Check the Telegram channel.")
        return 0
    logger.error("✗ Ticket post failed. See errors above.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
