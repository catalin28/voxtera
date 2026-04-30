"""TelegramSink — posts Tickets to a Telegram channel via the Bot API.

This is the demo-stage implementation of :class:`~voxtera.actions.sink.TicketSink`.
It performs a single HTTP POST to Telegram's ``sendMessage`` endpoint per
ticket. No persistence, no retry queue, no SLA — just notification.

Future sinks (FreshdeskSink, ZendeskSink) implement the same ``send(ticket)``
contract with a different HTTP target and so are drop-in replacements.

Setup requirements (see ``docs/ACTIONS_FEATURE_PLAN.md`` Phase 1):
    1. Telegram bot created via BotFather.
    2. ``TELEGRAM_BOT_TOKEN`` in ``.env``.
    3. Private channel created and bot added as admin with **Post messages**
       permission.
    4. ``TELEGRAM_CHANNEL_ID`` in ``.env`` (negative integer string,
       e.g. ``-1003718824790``).
"""

from __future__ import annotations

import os
from typing import Final

import aiohttp
from loguru import logger

from voxtera.actions.sink import TicketSink
from voxtera.actions.ticket import Ticket

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
# Telegram caps messages at 4096 chars. We keep a generous buffer for the
# prefix and metadata lines so a long summary cannot push us past the limit.
_MAX_TICKET_BODY_CHARS: Final[int] = 3500
# 10s is generous for an HTTP POST; we pick this over Telegram's docs-suggested
# 30s because a sink failure should fall back to the bot's spoken-apology
# fallback within the guest's patience window, not block the call.
_REQUEST_TIMEOUT_SECS: Final[float] = 10.0


class TelegramSink(TicketSink):
    """Posts Tickets to a Telegram channel via the Bot API.

    The bot must already be added as an admin of the target channel with
    the **Post messages** permission. See ``docs/ACTIONS_FEATURE_PLAN.md``
    section 5, Phase 1 for full setup steps.
    """

    def __init__(self, bot_token: str, channel_id: str) -> None:
        if not bot_token:
            raise ValueError("TelegramSink: bot_token is required (got empty string)")
        if not channel_id:
            raise ValueError("TelegramSink: channel_id is required (got empty string)")
        # Token is a secret; storing it on the instance is fine but it must
        # never be logged. Channel IDs are not secret and are safe to log.
        self._bot_token = bot_token
        self._channel_id = channel_id
        self._send_url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"

    @classmethod
    def from_env(cls) -> TelegramSink:
        """Build a TelegramSink from ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHANNEL_ID``.

        Raises :class:`ValueError` if either variable is missing or empty.
        """
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        channel = os.getenv("TELEGRAM_CHANNEL_ID", "")
        return cls(bot_token=token, channel_id=channel)

    async def send(self, ticket: Ticket) -> bool:
        """Post the ticket to Telegram. Never raises — returns bool."""
        body = self._format_message(ticket)
        if len(body) > _MAX_TICKET_BODY_CHARS:
            # Defensive: truncate before sending; a 4xx from Telegram is
            # harder to diagnose than a clean truncation marker in the post.
            body = body[: _MAX_TICKET_BODY_CHARS - 32] + "\n…[truncated]"
            logger.warning(
                "[telegram_sink] Ticket body truncated session={}",
                ticket.session_id,
            )
        payload = {
            "chat_id": self._channel_id,
            "text": body,
            # The original-language quote may include URLs the guest mentioned;
            # we don't want Telegram fetching previews for them.
            "disable_web_page_preview": True,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self._send_url, json=payload) as resp,
            ):
                data = await resp.json()
                if resp.status == 200 and data.get("ok") is True:
                    logger.info(
                        "[telegram_sink] Posted ticket session={} category={} room={}",
                        ticket.session_id,
                        ticket.category.value,
                        ticket.room_number,
                    )
                    return True
                # Telegram returns 4xx with a JSON body explaining why.
                # Log it but do not surface secrets — `data` does not
                # echo the token, only the chat_id (already non-secret).
                logger.error(
                    "[telegram_sink] Telegram rejected ticket session={} status={} response={}",
                    ticket.session_id,
                    resp.status,
                    data,
                )
                return False
        except aiohttp.ClientError as e:
            logger.error(
                "[telegram_sink] Network error session={} err={}",
                ticket.session_id,
                e,
            )
            return False
        except Exception as e:
            # Catch-all so a sink failure can never crash the voice loop.
            logger.exception(
                "[telegram_sink] Unexpected error session={}: {}",
                ticket.session_id,
                e,
            )
            return False

    @staticmethod
    def _format_message(ticket: Ticket) -> str:
        """Render the ticket per ``ACTIONS_FEATURE_PLAN.md`` section 3.5.

        Plain text only — Telegram's HTML/Markdown parsers introduce escaping
        edge cases (especially for guest quotes with stray ``*`` or ``_``)
        that aren't worth the visual upgrade for a demo channel.
        """
        lines = [
            f"[{ticket.category.value}] · Room {ticket.room_number}",
            ticket.summary,
            "",
            f"Guest spoke in: {ticket.language_detected}",
            f'"{ticket.original_quote}"',
            "",
            f"Session: {ticket.session_id}",
        ]
        return "\n".join(lines)
