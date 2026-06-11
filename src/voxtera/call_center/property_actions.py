"""Property actions — Telegram ticket delivery for the concierge's hotel mode.

Holds the long-lived Telegram plumbing for the property (hotel-concierge)
brain: the per-hotel actions runtime (InteractiveTelegramSink + ticket state
store + staff directory) and the button listener.

WHO decides to file is ``call_center.property_render``: the answering LLM
holds the ``create_ticket`` tool with the legacy confirmation prompt, so
gathering details and asking "shall I send this?" happen in the same model
call that speaks — promises and actions cannot diverge. This module only
builds runtimes and delivers validated tickets.

Everything is best-effort: any failure here degrades to an honest reply,
never breaks a turn.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger


def _listener_enabled() -> bool:
    return os.environ.get("CONCIERGE_TELEGRAM_LISTENER", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


class PropertyTicketer:
    """Builds per-hotel action runtimes and delivers tickets to Telegram.

    One instance lives on the shared concierge deps; runtimes are built
    lazily per hotel and cached (the Telegram sink and state store are
    long-lived by design).
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, Any] = {}  # hotel_id -> ActionRuntime | None
        self._listener_task: asyncio.Task | None = None

    def runtime(self, hotel_id: str):
        """Build (once) and return the hotel's ActionRuntime, or None.

        None when the feature can't run: no TELEGRAM_BOT_TOKEN, missing or
        invalid hotel config. Cached either way so we don't re-attempt on
        every turn.
        """
        if hotel_id in self._runtimes:
            return self._runtimes[hotel_id]
        runtime = None
        try:
            from voxtera.actions import build_action_runtime

            runtime = build_action_runtime(hotel_id)
        except Exception as e:  # noqa: BLE001 — feature off, never break a turn
            logger.warning(
                "[property-actions] ticket runtime unavailable for hotel={!r}: {}", hotel_id, e
            )
        self._runtimes[hotel_id] = runtime
        if runtime is not None and self._listener_task is None and _listener_enabled():
            # Staff button taps (acknowledge / assign / resolve) need the
            # long-poll listener. One per process is both sufficient and
            # required — two pollers on one bot token make Telegram 409.
            self._listener_task = asyncio.create_task(
                runtime.listener.run(), name="telegram-button-listener"
            )
            logger.info("[property-actions] Telegram button listener started")
        return runtime

    async def file(
        self,
        *,
        hotel_id: str,
        fields: dict[str, Any],
        original_quote: str,
    ) -> dict[str, Any] | None:
        """Deliver a validated ticket. None on any failure."""
        runtime = self.runtime(hotel_id)
        if runtime is None:
            return None

        from voxtera.actions.ticket import Category, Ticket

        try:
            category = Category(fields.get("category") or Category.OTHER.value)
        except ValueError:
            category = Category.OTHER
        ticket = Ticket(
            category=category,
            summary=(fields.get("summary") or original_quote)[:500],
            room_number=(fields.get("room_number") or "unknown")[:64],
            original_quote=original_quote[:1000],
            language_detected=(fields.get("language_detected") or "unknown")[:64],
        )
        try:
            ok = await runtime.sink.send(ticket)
        except Exception as e:  # noqa: BLE001 — sink contract says no-raise; belt+braces
            logger.exception("[property-actions] sink raised session={}: {}", ticket.session_id, e)
            ok = False
        if not ok:
            logger.error("[property-actions] ticket delivery failed session={}", ticket.session_id)
            return None
        logger.info(
            "[property-actions] ticket filed session={} category={} room={}",
            ticket.session_id,
            ticket.category.value,
            ticket.room_number,
        )
        return {"session_id": ticket.session_id, "category": ticket.category.value}
