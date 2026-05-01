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
import contextlib
from datetime import UTC, datetime
from typing import Any, Final

import aiohttp
from loguru import logger

from voxtera.actions.button_actions import (
    ActionRegistry,
    ButtonEvent,
    _format_post,
)
from voxtera.actions.state import (
    TicketRecord,
    TicketStateStore,
    TicketStatus,
    UrgencyThresholds,
)


def _source_message_coords(callback: dict[str, Any], record: TicketRecord) -> tuple[str | int, int]:
    """Determine which message the staff actually tapped on.

    Channel taps come from ``record.chat_id`` / ``record.message_id``.
    DM taps come from a different chat (the staff's private chat with the
    bot). The callback's ``message`` block carries that. Falling back to
    the channel coordinates is the safe default if the field is missing.
    """
    msg = callback.get("message") or {}
    chat = msg.get("chat") or {}
    src_chat_id = chat.get("id")
    src_message_id = msg.get("message_id")
    if src_chat_id is None or src_message_id is None:
        return record.chat_id, record.message_id
    return src_chat_id, src_message_id


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
# How often the aging monitor scans open tickets. Short enough that
# escalations feel real-time, long enough to keep API volume modest.
_AGING_SCAN_INTERVAL_SECS: Final[float] = 15.0


class TelegramListener:
    """Long-polls Telegram for button taps and dispatches them to handlers."""

    def __init__(
        self,
        *,
        bot_token: str,
        store: TicketStateStore,
        registry: ActionRegistry | None = None,
        urgency_thresholds: UrgencyThresholds | None = None,
    ) -> None:
        if not bot_token:
            raise ValueError("TelegramListener: bot_token is required")
        self._bot_token = bot_token
        self._store = store
        self._registry = registry or ActionRegistry()
        self._thresholds = urgency_thresholds or UrgencyThresholds.from_env()
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
        """Main loop. Poll → dispatch → repeat. Returns on stop or fatal error.

        Also runs the aging-monitor task in the background — it scans the
        store every ``_AGING_SCAN_INTERVAL_SECS`` and re-edits any ticket
        whose urgency level has crossed a threshold since the last scan.
        """
        backoff = _BACKOFF_INITIAL_SECS
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            logger.info(
                "[listener] starting; urgency thresholds (s): "
                "attention={} urgent={} critical={}",
                self._thresholds.attention_secs,
                self._thresholds.urgent_secs,
                self._thresholds.critical_secs,
            )
            aging_task = asyncio.create_task(self._aging_monitor_loop(http))
            try:
                while not self._stop_requested:
                    try:
                        updates = await self._fetch_updates(http)
                        backoff = _BACKOFF_INITIAL_SECS  # reset on success
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
                            asyncio.create_task(self._dispatch_callback(http, callback))
            finally:
                aging_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await aging_task
                logger.info("[listener] stop requested, exiting")

    async def _aging_monitor_loop(self, http: aiohttp.ClientSession) -> None:
        """Periodic scan of open tickets — escalate urgency as they age.

        Runs alongside the long-poll loop. Cheap: only edits messages
        when the urgency level for a ticket has actually changed.
        """
        while not self._stop_requested:
            try:
                await self._scan_and_escalate(http)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning("[listener] aging scan errored")
            await asyncio.sleep(_AGING_SCAN_INTERVAL_SECS)

    async def _scan_and_escalate(self, http: aiohttp.ClientSession) -> None:
        """One pass over open tickets; re-edit any that crossed a threshold."""
        now = datetime.now(UTC)
        open_records = await self._store.all_open()
        for record in open_records:
            # Skip terminal tickets (defensive — all_open should already filter).
            if record.status == TicketStatus.RESOLVED:
                continue
            age_secs = (now - record.ticket.timestamp).total_seconds()
            new_level = self._thresholds.level_for_age(age_secs)
            if new_level == record.urgency_level:
                continue

            def _mutate(r: TicketRecord, lvl=new_level) -> None:
                r.urgency_level = lvl

            updated = await self._store.update(record.session_id, _mutate)
            if updated is None:
                continue

            logger.info(
                "[listener] urgency changed session={} age={:.0f}s level={}→{}",
                record.session_id,
                age_secs,
                record.urgency_level.name,
                new_level.name,
            )
            # Edit the channel post to reflect the new urgency. We don't
            # change the keyboard during escalation — only the body.
            from voxtera.actions.button_actions import _default_keyboard  # noqa: PLC0415

            await self._edit_message(
                http,
                updated.chat_id,
                updated.message_id,
                _format_post(updated),
                _default_keyboard(updated, self._registry.directory),
            )

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

        # Parse <action_id>|<session_id>[|<extra>] from the button's callback_data.
        # `extra` is used by the staff picker (`assign|<sid>|<staff_idx>`).
        if "|" not in data:
            logger.warning("[listener] malformed callback_data: {!r}", data)
            await self._answer_callback(http, callback_id, "Unknown button.", show_alert=False)
            return
        parts = data.split("|", 2)
        action_id = parts[0]
        session_id = parts[1] if len(parts) >= 2 else ""
        extra = parts[2] if len(parts) >= 3 else None

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
            extra=extra,
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

        # The "primary" message to edit is whichever surface the tap came
        # from — channel post for channel buttons, DM for DM buttons.
        # We detect it via the chat_id of the message embedded in the
        # callback_query. If unavailable, fall back to the channel post.
        source_chat_id, source_message_id = _source_message_coords(callback, record)

        # Apply the result back to Telegram. Edit first, then ack — that
        # way the staff member's spinner stops only once the post is fresh.
        await self._edit_message(
            http,
            source_chat_id,
            source_message_id,
            result.new_text,
            result.keyboard,
        )
        # Some handlers ask us to also refresh a second message (e.g. a DM
        # action that should mirror state to the channel post, or vice versa).
        if result.secondary is not None:
            await self._edit_message(
                http,
                result.secondary.chat_id,
                result.secondary.message_id,
                result.secondary.new_text,
                result.secondary.keyboard,
            )
        await self._answer_callback(
            http,
            callback_id,
            result.toast or "",
            show_alert=result.show_alert,
        )

    async def _edit_message(
        self,
        http: aiohttp.ClientSession,
        chat_id: str | int,
        message_id: int,
        new_text: str,
        keyboard: list[list[dict]] | None,
    ) -> None:
        """Update a message via editMessageText. Tolerates 'not modified' errors."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
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
