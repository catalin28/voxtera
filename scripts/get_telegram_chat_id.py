"""One-time helper: discover a staff member's Telegram chat_id.

Run once per staff member you want to enable DM notifications for::

    uv run python scripts/get_telegram_chat_id.py

Then, on Telegram, the staff member opens a chat with your bot
(``@voxtera_demo_bot`` for the demo) and sends ANY message — for
example ``/start`` or just ``hi``. The script prints their ``chat_id``
(and a ready-to-paste YAML snippet), then exits.

Edit ``config/staff/<hotel_id>.yaml`` and add ``telegram_chat_id`` to the
staff entry::

    Maintenance:
      - name: John Smith
        role: maintenance technician
        eta_min: 5
        skills: [hvac, electrical]
        telegram_chat_id: 123456789  # ← paste here

Restart the demo. From the next assignment onward, John will receive a
DM with the ticket details + a "View in channel" link.

Why this script exists
----------------------
Telegram bots cannot DM a user who has not messaged them first. We need
the user to send a message so the Bot API hands us their numeric id.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Final

import aiohttp
from dotenv import load_dotenv
from loguru import logger

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
_LONG_POLL_SECS: Final[int] = 30
_REQUEST_TIMEOUT_SECS: Final[float] = float(_LONG_POLL_SECS + 10)


async def _run() -> int:
    load_dotenv()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        return 1

    base = f"{_TELEGRAM_API_BASE}/bot{bot_token}"
    print(
        "\n"
        "  ▸ Open Telegram, find your bot, and send it ANY message.\n"
        "  ▸ For example: /start, or just type 'hi'.\n"
        "  ▸ This script will print the sender's chat_id and exit.\n"
        "  ▸ Press Ctrl-C to abort.\n"
    )

    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
    offset = 0
    seen: set[int] = set()
    async with aiohttp.ClientSession(timeout=timeout) as http:
        while True:
            try:
                params: dict[str, Any] = {
                    "timeout": _LONG_POLL_SECS,
                    "offset": offset,
                    "allowed_updates": ["message"],
                }
                async with http.get(f"{base}/getUpdates", params=params) as resp:
                    data = await resp.json()
            except aiohttp.ClientError as e:
                logger.warning("network error: {} — retrying", e)
                await asyncio.sleep(2)
                continue

            if not data.get("ok"):
                logger.error("Telegram getUpdates not ok: {}", data)
                return 1

            for update in data.get("result", []) or []:
                offset = max(offset, update.get("update_id", 0) + 1)
                msg = update.get("message")
                if not msg:
                    continue
                from_user = msg.get("from", {}) or {}
                chat = msg.get("chat", {}) or {}
                chat_id = chat.get("id")
                user_id = from_user.get("id")
                # Private DMs: chat_id == user_id. We use chat_id since
                # that's what sendMessage expects.
                if chat_id is None or chat_id in seen:
                    continue
                seen.add(chat_id)

                first_name = from_user.get("first_name", "Unknown")
                last_name = from_user.get("last_name", "")
                username = from_user.get("username")
                full_name = (first_name + " " + last_name).strip()
                handle = f"@{username}" if username else "(no username)"
                text = msg.get("text", "(non-text message)")

                print()
                print(f"  ✓ Got a message from: {full_name} {handle}")
                print(f"    User ID:  {user_id}")
                print(f"    Chat ID:  {chat_id}")
                print(f"    Message:  {text!r}")
                print()
                print("  Paste this into the relevant entry of config/staff/<hotel>.yaml:")
                print()
                print(f"        telegram_chat_id: {chat_id}")
                print()
                return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_run()))
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(0)
