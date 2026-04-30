"""Demo: post a Maintenance ticket with buttons, then listen for taps.

Run from the project root::

    uv run python scripts/demo_interactive_buttons.py

What happens:

1. A sample Maintenance ticket is posted to your Telegram channel,
   complete with three inline buttons:
   - 🔍 Find available technician
   - ✅ I'm on it
   - ✓ Resolved
2. The listener starts and runs for ~3 minutes (or until Ctrl-C).
3. Tap any button on your phone or in the Telegram desktop app and watch:
   - The terminal logs the event.
   - The post in the channel updates in place to reflect new state.
4. Tap "✓ Resolved" to mark it done — the buttons disappear and the
   post shows the final history.

This is the demo to walk through with your team. It uses real Telegram,
real handler code, real state — only the staff list and the "ETA 5 min"
are mocked, and those are easy to swap out later.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from voxtera.actions import load_hotel_config
from voxtera.actions.button_actions import ActionRegistry
from voxtera.actions.interactive_sink import InteractiveTelegramSink
from voxtera.actions.listener import TelegramListener
from voxtera.actions.state import TicketStateStore
from voxtera.actions.ticket import Category, Ticket

# Default duration of the demo. After this many seconds the listener stops
# and the script exits cleanly. Override with VOXTERA_DEMO_SECS=NN for
# longer demos.
_DEFAULT_DEMO_SECS = 180


async def _run() -> int:
    load_dotenv()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        return 1

    cfg = load_hotel_config(os.getenv("HOTEL_ID", "demo"))

    # Shared state store between sink (writes records) and listener (reads + mutates them).
    store = TicketStateStore()
    sink = InteractiveTelegramSink(
        bot_token=bot_token,
        channel_id=cfg.telegram_channel_id,
        store=store,
    )

    # Sample ticket: a French guest reporting a broken AC. Same shape as
    # what Claude would produce in the live bot.
    ticket = Ticket(
        category=Category.MAINTENANCE,
        summary="AC not cooling in room 412 since last night.",
        room_number="412",
        original_quote="La climatisation ne fonctionne pas depuis hier soir.",
        language_detected="French",
    )

    logger.info("[demo] Posting interactive ticket to channel {}", cfg.telegram_channel_id)
    ok = await sink.send(ticket)
    if not ok:
        logger.error("[demo] Failed to post ticket — aborting.")
        return 1

    logger.info(
        "[demo] ✓ Ticket posted with buttons. Open Telegram and try them.\n"
        "       Listening for {} seconds. Ctrl-C to stop early.",
        os.getenv("VOXTERA_DEMO_SECS", str(_DEFAULT_DEMO_SECS)),
    )

    listener = TelegramListener(
        bot_token=bot_token,
        store=store,
        registry=ActionRegistry(),
    )

    duration = float(os.getenv("VOXTERA_DEMO_SECS", str(_DEFAULT_DEMO_SECS)))
    listen_task = asyncio.create_task(listener.run())
    try:
        await asyncio.wait_for(listen_task, timeout=duration)
    except TimeoutError:
        logger.info("[demo] Time's up — stopping listener.")
        listener.stop()
        # Give the loop a moment to exit cleanly before cancelling.
        try:
            await asyncio.wait_for(listen_task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            listen_task.cancel()
    except KeyboardInterrupt:
        logger.info("[demo] Interrupted, stopping listener.")
        listener.stop()
        listen_task.cancel()

    # Show what happened during the demo.
    open_records = await store.all_open()
    all_records_count = len([r for r in store._records.values()])  # noqa: SLF001 demo introspection
    logger.info(
        "[demo] Done. Tickets seen: {} (open at exit: {}).",
        all_records_count,
        len(open_records),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        sys.exit(0)
