"""TelegramListener — long-poll loop that turns button taps into action calls.

Telegram's bot API uses ``getUpdates`` with a long-poll timeout to deliver
events. This listener:

1. Polls ``getUpdates`` continuously with a 30-second timeout.
2. For each ``callback_query`` (a button tap), parses
   ``callback_data`` into ``<action_id>|<session_id>``.
3. Looks up the matching :class:`TicketRecord` in the
   :class:`~voxtera.actions.state.TicketStateStore`.
4. Dispatches to the registry, gets an
   :class:`~voxtera.actions.button_actions.ActionResult`.
5. Calls ``editMessageText`` to update the original post and
   ``answerCallbackQuery`` to dismiss the loading spinner (and optionally
   show a toast to the staff member).

The listener is fully self-contained — start it with ``await listener.run()``
in any asyncio context. It exits cleanly when ``stop()`` is called or the
task is cancelled.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import aiohttp
from loguru import logger

from voxtera.actions.button_actions import (
    ActionRegistry,
    ActionResult,
    ButtonEvent,
)
from voxtera.actions.state import TicketStateStore

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
# Long poll: ask Telegram to hold the request open up to N seconds waiting
# for new events. Reduces request volume to ~1/poll-interval per channel.
_LONG_POLL_TIMEOUT_SECS: Final[int] = 30
# Network-level timeout — must be longer than the long-poll value or we
# self-cancel before Telegram replies.
_REQUEST_TIMEOUT_SECS: Final[float] = float(_LONG_POLL_TIMEOUT_SECS + 10)
# Backoff bounds when Telegram is unavailable. Capped to keep retries
# visible in logs without spamming.
_BACKOFF_INITIAL_SECS: Final[float] = 1.0
_BACKOFF_MAX_SECS: Final[float] = 30.0


class TelegramListener:
    """Long-polls Telegram for button taps and dispatches them to handlers."""

    def __init__(
        self,
        *,
        bot_token: str,
        store: TicketStateStore,
        registry: ActionRegistry | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("TelegramListener: bot_token is required")
        self._bot_token = bot_token
        self._store = store
        self._registry = registry or ActionRegistry()
        self._base_url = f"{_TELEGRAM_API_BASE}/bot{bot_token}"
        self._stop_requested = False
        # ``offset`` is the highest update_id we've seen + 1. Telegram uses
        # this to mark earlier updates as acknowledged so we don't re-receive
        # them. Persisting this to disk would survive restarts; in-memory is
        # fine for the demo since the listener and bot start together.
        self._offset = 0

    def stop(self) -> None:
        """Request the run loop to exit at the next iteration."""
        self._stop_requested = True

    async def run(self) -> None:
        """Main loop. Poll → dispatch → repeat. Returns on stop or fatal error."""
        backoff = _BACKOFF_INITIAL_SECS
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            logger.info("[listener] starting long-poll loop")
            while not self._stop_requested:
                try:
                    updates = await self._fetch_updates(http)
                    backoff = _BACKOFF_INITIAL_SECS  # reset on a successful fetch
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        "[listener] getUpdates failed, backing off {:.1f}s: {}",
                        backoff,
                        e,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_SECS)
                    continue

                for update in updates:
                    self._offset = max(self._offset, update.get("update_id", 0) + 1)
                    callback = update.get("callback_query")
                    if callback is not None:
                        # Run dispatch concurrently so a slow handler doesn't
                        # block the next poll. Errors get logged inside.
                        asyncio.create_task(self._dispatch_callback(http, callback))

            logger.info("[listener] stop requested, exiting")

    async def _fetch_updates(self, http: aiohttp.ClientSession) -> list[dict[str, Any]]:
        """Issue one long-poll getUpdates and return the parsed updates list."""
        params = {
            "timeout": _LONG_POLL_TIMEOUT_SECS,
            "offset": self._offset,
            "allowed_updates": ["callback_query"],  # ignore everything else
        }
        async with http.get(f"{self._base_url}/getUpdates", params=params) as resp:
            data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates not ok: {data}")
        return data.get("result", []) or []

    async def _dispatch_callback(
        self, http: aiohttp.ClientSession, callback: dict[str, Any]
    ) -> None:
        """Parse one callback_query and run the matching handler."""
        callback_id = callback.get("id", "")
        data = callback.get("data", "") or ""
        from_user = callback.get("from", {}) or {}
        actor_name = from_user.get("first_name") or from_user.get("username") or "Unknown"
        actor_username = from_user.get("username")

        # Parse <action_id>|<session_id> from the button's callback_data.
        if "|" not in data:
            logger.warning("[listener] malformed callback_data: {!r}", data)
            await self._answer_callback(http, callback_id, "Unknown button.", show_alert=False)
            return
        action_id, session_id = data.split("|", 1)

        handler = self._registry.get(action_id)
        if handler is None:
            logger.warning("[listener] no handler for action_id={!r}", action_id)
            await self._answer_callback(
                http, callback_id, "Action not implemented.", show_alert=False
            )
            return

        record = await self._store.get(session_id)
        if record is None:
            logger.warning(
                "[listener] no ticket for session_id={!r} (bot may have restarted)",
                session_id,
            )
            await self._answer_callback(
                http,
                callback_id,
                "Ticket not found — it may have expired.",
                show_alert=False,
            )
            return

        event = ButtonEvent(
            action_id=action_id,
            session_id=session_id,
            actor_name=actor_name,
            actor_username=actor_username,
        )

        try:
            result = await handler(event, record, self._store)
        except Exception:
            logger.opt(exception=True).error(
                "[listener] handler {!r} raised for session={!r}",
                action_id,
                session_id,
            )
            await self._answer_callback(
                http,
                callback_id,
                "Action failed — please retry.",
                show_alert=True,
            )
            return

        # Apply the result back to Telegram. Edit first, then ack — that
        # way the staff member's spinner stops only once the post is fresh.
        await self._edit_message(http, record.chat_id, record.message_id, result)
        await self._answer_callback(
            http,
            callback_id,
            result.toast or "",
            show_alert=result.show_alert,
        )

    async def _edit_message(
        self, http: aiohttp.ClientSession, chat_id: str, message_id: int, result: ActionResult
    ) -> None:
        """Update the original post with the new text (and keyboard, if any)."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": result.new_text,
            "disable_web_page_preview": True,
        }
        if result.keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": result.keyboard}
        # If keyboard is None we want to remove buttons; Telegram does this
        # by sending an empty reply_markup object.
        else:
            payload["reply_markup"] = {"inline_keyboard": []}

        try:
            async with http.post(f"{self._base_url}/editMessageText", json=payload) as resp:
                data = await resp.json()
            if not data.get("ok"):
                # 'message is not modified' is a benign error — Telegram raises
                # it when the new text equals the old text. Log at debug only.
                desc = data.get("description", "")
                if "message is not modified" in desc:
                    logger.debug("[listener] editMessageText: no change ({})", desc)
                else:
                    logger.error("[listener] editMessageText failed: {}", data)
        except aiohttp.ClientError as e:
            logger.error("[listener] editMessageText network error: {}", e)

    async def _answer_callback(
        self,
        http: aiohttp.ClientSession,
        callback_id: str,
        text: str,
        *,
        show_alert: bool,
    ) -> None:
        """Acknowledge the callback so the spinner stops; optionally show a toast."""
        if not callback_id:
            return
        payload = {
            "callback_query_id": callback_id,
            "text": text or "",
            "show_alert": show_alert,
        }
        try:
            async with http.post(f"{self._base_url}/answerCallbackQuery", json=payload) as resp:
                if resp.status != 200:
                    logger.debug("[listener] answerCallbackQuery non-200: {}", resp.status)
        except aiohttp.ClientError as e:
            logger.debug("[listener] answerCallbackQuery network error: {}", e)
