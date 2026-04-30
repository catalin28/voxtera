"""InteractiveTelegramSink — a TelegramSink that adds inline buttons.

Extends :class:`~voxtera.actions.telegram_sink.TelegramSink` in two ways:

1. Attaches an ``inline_keyboard`` to each posted ticket so staff can act
   without leaving Telegram.
2. Captures the resulting Telegram ``message_id`` and persists a
   :class:`~voxtera.actions.state.TicketRecord` to a
   :class:`~voxtera.actions.state.TicketStateStore` so the listener can
   later edit the same post.

The button layout per category comes from ``DEFAULT_BUTTON_LAYOUT`` in
``state.py``; categories not in that dict get a generic Resolved button
so staff at least have a way to close them.
"""

from __future__ import annotations

from typing import Final

import aiohttp
from loguru import logger

from voxtera.actions.state import (
    DEFAULT_BUTTON_LAYOUT,
    TicketRecord,
    TicketStateStore,
)
from voxtera.actions.telegram_sink import TelegramSink
from voxtera.actions.ticket import Category, Ticket

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
_REQUEST_TIMEOUT_SECS: Final[float] = 10.0


def _build_keyboard(category: Category, session_id: str) -> list[list[dict]]:
    """Build the initial inline_keyboard rows for a ticket of ``category``.

    Each row is a list of button dicts. We put one button per row for
    readability on phone screens — Telegram packs them tightly otherwise.
    """
    layout = DEFAULT_BUTTON_LAYOUT.get(category)
    if layout is None:
        # Fallback: just a Resolved button so the ticket is still actionable.
        return [
            [{"text": "✓ Resolved", "callback_data": f"resolved|{session_id}"}],
        ]
    return [
        [{"text": label, "callback_data": f"{action_id}|{session_id}"}]
        for action_id, label in layout
    ]


class InteractiveTelegramSink(TelegramSink):
    """TelegramSink that attaches inline buttons and tracks message state."""

    def __init__(self, bot_token: str, channel_id: str, store: TicketStateStore) -> None:
        super().__init__(bot_token=bot_token, channel_id=channel_id)
        self._store = store

    @classmethod
    def from_env(  # type: ignore[override]
        cls, store: TicketStateStore | None = None
    ) -> InteractiveTelegramSink:
        """Build from env vars; create an empty store if one is not supplied."""
        import os

        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        channel = os.getenv("TELEGRAM_CHANNEL_ID", "")
        return cls(bot_token=token, channel_id=channel, store=store or TicketStateStore())

    @property
    def store(self) -> TicketStateStore:
        """Expose the state store so the listener can share it."""
        return self._store

    async def send(self, ticket: Ticket) -> bool:  # type: ignore[override]
        """Post the ticket with buttons, then record the message_id for editing."""
        body = self._format_message(ticket)
        keyboard = _build_keyboard(ticket.category, ticket.session_id)
        payload = {
            "chat_id": self._channel_id,
            "text": body,
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }
        try:
            timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self._send_url, json=payload) as resp,
            ):
                data = await resp.json()
                if resp.status == 200 and data.get("ok") is True:
                    message_id = data["result"]["message_id"]
                    record = TicketRecord(
                        session_id=ticket.session_id,
                        ticket=ticket,
                        chat_id=self._channel_id,
                        message_id=message_id,
                    )
                    await self._store.put(record)
                    logger.info(
                        "[interactive_sink] Posted ticket session={} msg_id={} category={}",
                        ticket.session_id,
                        message_id,
                        ticket.category.value,
                    )
                    return True
                logger.error(
                    "[interactive_sink] Telegram rejected ticket session={} status={} resp={}",
                    ticket.session_id,
                    resp.status,
                    data,
                )
                return False
        except aiohttp.ClientError as e:
            logger.error(
                "[interactive_sink] Network error session={} err={}",
                ticket.session_id,
                e,
            )
            return False
        except Exception as e:
            logger.exception(
                "[interactive_sink] Unexpected error session={}: {}",
                ticket.session_id,
                e,
            )
            return False
