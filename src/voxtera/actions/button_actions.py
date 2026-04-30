"""Button-action registry and built-in handlers for interactive tickets.

When a staff member taps an inline button under a ticket, Telegram sends a
``callback_query`` containing ``callback_data`` of the form
``<action_id>|<session_id>``. The :class:`ActionRegistry` looks up the
handler for that ``action_id`` and runs it against the matching
:class:`~voxtera.actions.state.TicketRecord`.

A handler returns an :class:`ActionResult`: the new message text, an
optional updated keyboard, and an optional toast for the staff member. The
listener feeds those back to Telegram via ``editMessageText`` and
``answerCallbackQuery``.

Mock data (the staff list) lives here for v1. When real PMS / scheduling
integrations land, only ``find_available`` changes — the registry shape
and the handler signature stay identical.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from voxtera.actions.state import (
    ActionEntry,
    TicketRecord,
    TicketStateStore,
    TicketStatus,
)

# Mock staff list for the demo. A real implementation would query the
# hotel's scheduling system. Round-robin assignment is good enough to
# show the loop closing on stage.
_MOCK_STAFF = [
    {"name": "John Smith", "role": "maintenance", "eta_min": 5},
    {"name": "Maria Garcia", "role": "maintenance", "eta_min": 7},
    {"name": "Akio Tanaka", "role": "maintenance", "eta_min": 4},
]
_round_robin = itertools.cycle(_MOCK_STAFF)


@dataclass
class ButtonEvent:
    """Everything the listener gives a handler about the tap that just happened."""

    action_id: str
    session_id: str
    actor_name: str  # Telegram first_name (or "Unknown" if hidden)
    actor_username: str | None  # @handle, may be None


@dataclass
class ActionResult:
    """What the listener should do to the message after the handler runs."""

    new_text: str  # full new message body — handler does the formatting
    keyboard: list[list[dict]] | None  # new inline_keyboard rows; None = remove
    toast: str | None = None  # short text shown to the staff member only
    show_alert: bool = False  # if True, modal popup instead of toast


# Type alias for a handler. Handlers are async because real ones may hit
# external APIs (PMS, paging systems); the demo ones are sync-fast but
# keep the signature consistent.
ButtonHandler = Callable[[ButtonEvent, TicketRecord, TicketStateStore], Awaitable[ActionResult]]


# ----------------------------------------------------------------------
# Helpers shared by every handler
# ----------------------------------------------------------------------


def _format_post(record: TicketRecord) -> str:
    """Render the updated Telegram post given the ticket record's current state.

    Keeps the same skeleton as the original post (so it stays scannable)
    and appends a status block describing where the ticket is now.
    """
    t = record.ticket
    lines = [
        f"[{t.category.value}] · Room {t.room_number}",
        t.summary,
        "",
        f"Guest spoke in: {t.language_detected}",
        f'"{t.original_quote}"',
        "",
        f"Session: {t.session_id}",
        "",
        f"━━━ Status: {record.status.value.upper()} ━━━",
    ]
    if record.assigned_to:
        lines.append(f"Assigned to: {record.assigned_to}")
    if record.claimed_by and record.status != TicketStatus.RESOLVED:
        lines.append(f"Claimed by: {record.claimed_by}")
    if record.history:
        lines.append("")
        lines.append("History:")
        for entry in record.history[-4:]:  # cap to keep the post readable
            ts = entry.timestamp.strftime("%H:%M:%S UTC")
            lines.append(f"• {ts} — {entry.actor}: {entry.note}")
    return "\n".join(lines)


def _build_keyboard_for(record: TicketRecord) -> list[list[dict]] | None:
    """Pick the right keyboard for the current state.

    - Open / Claimed / Assigned: full button set, minus any already-completed actions.
    - Resolved: no keyboard (return None so the listener strips it).
    """
    if record.is_terminal():
        return None
    sid = record.session_id
    rows: list[list[dict]] = []
    # Always show all three for v1. Future: hide find_avail once assigned, etc.
    if not record.assigned_to:
        rows.append(
            [{"text": "🔍 Find available technician", "callback_data": f"find_avail|{sid}"}]
        )
    if record.claimed_by is None and record.assigned_to is None:
        rows.append([{"text": "✅ I'm on it", "callback_data": f"ack|{sid}"}])
    rows.append([{"text": "✓ Resolved", "callback_data": f"resolved|{sid}"}])
    return rows


# ----------------------------------------------------------------------
# Built-in handlers
# ----------------------------------------------------------------------


async def handle_find_available(
    event: ButtonEvent, record: TicketRecord, store: TicketStateStore
) -> ActionResult:
    """Auto-assign a maintenance staff member from the mock pool."""
    if record.is_terminal():
        return ActionResult(
            new_text=_format_post(record),
            keyboard=None,
            toast="Already resolved.",
        )
    if record.assigned_to:
        return ActionResult(
            new_text=_format_post(record),
            keyboard=_build_keyboard_for(record),
            toast=f"Already assigned to {record.assigned_to}.",
        )

    staff = next(_round_robin)
    note = f"Auto-assigned to {staff['name']} (ETA {staff['eta_min']} min)"

    def _mutate(r: TicketRecord) -> None:
        r.assigned_to = staff["name"]
        r.status = TicketStatus.ASSIGNED
        r.history.append(
            ActionEntry(
                timestamp=TicketStateStore.now(),
                actor=event.actor_name,
                action_id=event.action_id,
                note=note,
            )
        )

    updated = await store.update(record.session_id, _mutate)
    if updated is None:
        return ActionResult(new_text=_format_post(record), keyboard=None, toast="Ticket vanished.")

    logger.info(
        "[buttons] find_available session={} actor={!r} → {}",
        event.session_id,
        event.actor_name,
        staff["name"],
    )
    return ActionResult(
        new_text=_format_post(updated),
        keyboard=_build_keyboard_for(updated),
        toast=f"Assigned to {staff['name']}",
    )


async def handle_acknowledge(
    event: ButtonEvent, record: TicketRecord, store: TicketStateStore
) -> ActionResult:
    """Staff member claims the ticket (first-click-wins)."""
    if record.is_terminal():
        return ActionResult(
            new_text=_format_post(record),
            keyboard=None,
            toast="Already resolved.",
        )
    if record.claimed_by is not None:
        return ActionResult(
            new_text=_format_post(record),
            keyboard=_build_keyboard_for(record),
            toast=f"Already claimed by {record.claimed_by}.",
        )

    claimer = event.actor_name

    def _mutate(r: TicketRecord) -> None:
        r.claimed_by = claimer
        r.status = TicketStatus.CLAIMED
        r.history.append(
            ActionEntry(
                timestamp=TicketStateStore.now(),
                actor=claimer,
                action_id=event.action_id,
                note=f"Claimed by {claimer}",
            )
        )

    updated = await store.update(record.session_id, _mutate)
    if updated is None:
        return ActionResult(new_text=_format_post(record), keyboard=None, toast="Ticket vanished.")

    logger.info(
        "[buttons] ack session={} claimer={!r}",
        event.session_id,
        claimer,
    )
    return ActionResult(
        new_text=_format_post(updated),
        keyboard=_build_keyboard_for(updated),
        toast="You've claimed this ticket.",
    )


async def handle_resolved(
    event: ButtonEvent, record: TicketRecord, store: TicketStateStore
) -> ActionResult:
    """Mark the ticket resolved and strip the keyboard."""
    if record.is_terminal():
        return ActionResult(
            new_text=_format_post(record),
            keyboard=None,
            toast="Already resolved.",
        )

    actor = event.actor_name

    def _mutate(r: TicketRecord) -> None:
        r.status = TicketStatus.RESOLVED
        r.history.append(
            ActionEntry(
                timestamp=TicketStateStore.now(),
                actor=actor,
                action_id=event.action_id,
                note=f"Resolved by {actor}",
            )
        )

    updated = await store.update(record.session_id, _mutate)
    if updated is None:
        return ActionResult(new_text=_format_post(record), keyboard=None, toast="Ticket vanished.")

    logger.info("[buttons] resolved session={} actor={!r}", event.session_id, actor)
    return ActionResult(
        new_text=_format_post(updated),
        keyboard=None,  # remove buttons — terminal state
        toast="Ticket marked resolved. Thank you!",
    )


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


class ActionRegistry:
    """Maps ``action_id`` strings to handler callables.

    Built-in actions are pre-registered. Custom actions can be added at
    startup via ``register(...)`` — this lets future hotels wire
    integration-specific handlers without forking the codebase.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ButtonHandler] = {}
        self.register("find_avail", handle_find_available)
        self.register("ack", handle_acknowledge)
        self.register("resolved", handle_resolved)

    def register(self, action_id: str, handler: ButtonHandler) -> None:
        """Add or replace a handler for ``action_id``."""
        if "|" in action_id:
            raise ValueError(f"action_id may not contain '|': {action_id!r}")
        if len(action_id) > 16:
            # Keep IDs short — Telegram caps callback_data at 64 bytes total.
            raise ValueError(f"action_id too long (max 16 chars): {action_id!r}")
        self._handlers[action_id] = handler

    def get(self, action_id: str) -> ButtonHandler | None:
        return self._handlers.get(action_id)
