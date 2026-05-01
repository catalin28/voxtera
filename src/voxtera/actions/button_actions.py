"""Button-action registry and built-in handlers for interactive tickets.

When a staff member taps an inline button under a ticket, Telegram sends a
``callback_query`` containing ``callback_data`` of the form
``<action_id>|<session_id>`` (or ``<action_id>|<session_id>|<arg>`` for
the assign action). The :class:`ActionRegistry` looks up the handler for
``action_id`` and runs it against the matching
:class:`~voxtera.actions.state.TicketRecord`.

A handler returns an :class:`ActionResult`: the new message text, an
optional updated keyboard, and an optional toast for the staff member. The
listener feeds those back to Telegram via ``editMessageText`` and
``answerCallbackQuery``.

The action registry is constructed with a :class:`StaffDirectory` so it
can show per-category staff lists when "Find available" is tapped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from voxtera.actions.staff import Staff, StaffDirectory
from voxtera.actions.staff_notifier import (
    NoopStaffNotifier,
    StaffNotifier,
    dm_action_keyboard,
    format_assignment_dm,
)
from voxtera.actions.state import (
    ActionEntry,
    TicketRecord,
    TicketStateStore,
    TicketStatus,
    UrgencyLevel,
)


@dataclass
class ButtonEvent:
    """Everything the listener gives a handler about the tap that just happened."""

    action_id: str
    session_id: str
    actor_name: str  # Telegram first_name (or "Unknown" if hidden)
    actor_username: str | None  # @handle, may be None
    extra: str | None = None  # third pipe-separated arg (e.g. staff index)


@dataclass
class SecondaryEdit:
    """A second message the listener should also edit after the primary one.

    Used when a DM-side button changes state that should also be reflected
    in the channel post (or vice versa). Independent text + keyboard so
    each surface can show the right view of the same underlying ticket.
    """

    chat_id: str | int
    message_id: int
    new_text: str
    keyboard: list[list[dict]] | None


@dataclass
class ActionResult:
    """What the listener should do to the message after the handler runs."""

    new_text: str  # full new message body — handler does the formatting
    keyboard: list[list[dict]] | None  # new inline_keyboard rows; None = remove
    toast: str | None = None  # short text shown to the staff member only
    show_alert: bool = False  # if True, modal popup instead of toast
    secondary: SecondaryEdit | None = None  # optional cross-message update


# Type alias for a handler. Handlers are async because real ones may hit
# external APIs (PMS, paging systems); the demo ones are sync-fast but
# keep the signature consistent.
ButtonHandler = Callable[[ButtonEvent, TicketRecord, TicketStateStore], Awaitable[ActionResult]]


# ----------------------------------------------------------------------
# Helpers shared by every handler
# ----------------------------------------------------------------------


def _status_label(status: TicketStatus) -> str:
    """Render a status with a small visual cue. Keeps the channel post scannable."""
    if status == TicketStatus.EN_ROUTE:
        return "ON THE WAY 🚶"
    if status == TicketStatus.ON_SITE:
        return "ON-SITE 🛠"
    if status == TicketStatus.RESOLVED:
        return "RESOLVED ✓"
    return status.value.upper()


# Single-character "color bands" placed at the top of the post.
# Telegram doesn't allow real bubble colors, but these emojis are the
# canonical colored circles and render identically on every client, so
# they read as a color cue at a glance.
_STATUS_EMOJI: dict[TicketStatus, str] = {
    TicketStatus.OPEN: "🟢",
    TicketStatus.CLAIMED: "🔵",
    TicketStatus.ASSIGNED: "🔵",
    TicketStatus.EN_ROUTE: "🟣",
    TicketStatus.ON_SITE: "🟡",
    TicketStatus.RESOLVED: "⚫",
}

# Urgency overrides the status color when the ticket has been open too long.
# The escalation tells managers "look at this" without scrolling.
_URGENCY_EMOJI: dict[UrgencyLevel, str] = {
    UrgencyLevel.NORMAL: "",
    UrgencyLevel.ATTENTION: "🟡",
    UrgencyLevel.URGENT: "🟠",
    UrgencyLevel.CRITICAL: "🔴",
}

_URGENCY_BANNER: dict[UrgencyLevel, str] = {
    UrgencyLevel.NORMAL: "",
    UrgencyLevel.ATTENTION: "⏱ ATTENTION — ticket is aging",
    UrgencyLevel.URGENT: "⚠ URGENT — ticket has been open too long",
    UrgencyLevel.CRITICAL: "🚨 CRITICAL — immediate action required",
}


def _color_band_for(record: TicketRecord) -> str:
    """Choose the leading color emoji for the ticket post.

    Resolved tickets use ⚫. Otherwise, urgency overrides status when
    escalation has begun (ATTENTION or higher); else the status emoji.
    """
    if record.status == TicketStatus.RESOLVED:
        return _STATUS_EMOJI[TicketStatus.RESOLVED]
    if record.urgency_level >= UrgencyLevel.ATTENTION:
        return _URGENCY_EMOJI[record.urgency_level]
    return _STATUS_EMOJI.get(record.status, "")


def _format_post(record: TicketRecord) -> str:
    """Render the post body given the ticket record's current state.

    Keeps the same skeleton as the original post (so it stays scannable)
    and appends a status block describing where the ticket is now. The
    leading color band shifts as urgency escalates so an aging ticket is
    visible at a glance, even in a crowded channel.
    """
    t = record.ticket
    color = _color_band_for(record)
    header = f"{color} [{t.category.value}] · Room {t.room_number}"
    lines = [
        header,
        t.summary,
        "",
        f"Guest spoke in: {t.language_detected}",
        f'"{t.original_quote}"',
        "",
        f"Session: {t.session_id}",
        "",
        f"━━━ Status: {_status_label(record.status)} ━━━",
    ]
    # Aging banner: only shown when the ticket is open and has escalated.
    if record.status != TicketStatus.RESOLVED and record.urgency_level >= UrgencyLevel.ATTENTION:
        lines.append(_URGENCY_BANNER[record.urgency_level])
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


def _find_staff_by_name(directory: StaffDirectory, category, name: str) -> Staff | None:
    """Find a Staff object by name within a category's roster."""
    for staff in directory.get(category):
        if staff.name == name:
            return staff
    return None


def _dm_secondary(record: TicketRecord, staff: Staff | None) -> SecondaryEdit | None:
    """Build a SecondaryEdit for the staff DM, if the DM exists.

    Used when a channel-side action should also refresh the staff's DM
    (e.g. manager hits Resolved → DM updates to "Status: ✓ RESOLVED").
    """
    if record.dm_chat_id is None or record.dm_message_id is None or staff is None:
        return None
    return SecondaryEdit(
        chat_id=record.dm_chat_id,
        message_id=record.dm_message_id,
        new_text=format_assignment_dm(staff, record),
        keyboard=dm_action_keyboard(record),
    )


def _channel_secondary(record: TicketRecord, directory: StaffDirectory) -> SecondaryEdit:
    """Build a SecondaryEdit for the channel post.

    Used when a DM-side action should also refresh the channel post.
    """
    return SecondaryEdit(
        chat_id=record.chat_id,
        message_id=record.message_id,
        new_text=_format_post(record),
        keyboard=_default_keyboard(record, directory),
    )


def _format_post_with_picker(record: TicketRecord, staff_list: tuple[Staff, ...]) -> str:
    """Render the post body during staff-picker view.

    Shows the original ticket plus a header announcing the picker so the
    operator knows why the keyboard suddenly changed.
    """
    base = _format_post(record)
    return base + (
        f"\n\n━━━ Pick a {record.ticket.category.value} staff ━━━\n"
        f"({len(staff_list)} available)"
    )


def _default_keyboard(record: TicketRecord, directory: StaffDirectory) -> list[list[dict]] | None:
    """The "main" keyboard for a ticket given its current status.

    Returns ``None`` for resolved tickets (the listener strips the keyboard).
    """
    if record.is_terminal():
        return None

    sid = record.session_id
    rows: list[list[dict]] = []

    # Find-available is only meaningful when staff exist for this category
    # AND no one is yet assigned. After assignment, the picker disappears.
    has_staff = directory.has(record.ticket.category)
    if record.assigned_to is None and has_staff:
        rows.append(
            [{"text": "🔍 Find available technician", "callback_data": f"find_avail|{sid}"}]
        )
    if record.claimed_by is None and record.assigned_to is None:
        rows.append([{"text": "✅ I'm on it", "callback_data": f"ack|{sid}"}])
    rows.append([{"text": "✓ Resolved", "callback_data": f"resolved|{sid}"}])
    return rows


def _picker_keyboard(session_id: str, staff_list: tuple[Staff, ...]) -> list[list[dict]]:
    """Build the staff picker keyboard.

    One button per staff member, plus a Back button to cancel out.
    Indexed by position so callback_data stays compact (within the 64-byte
    Telegram limit).
    """
    rows: list[list[dict]] = []
    for idx, staff in enumerate(staff_list):
        rows.append(
            [
                {
                    "text": staff.button_label(),
                    "callback_data": f"assign|{session_id}|{idx}",
                }
            ]
        )
    rows.append([{"text": "⬅ Back", "callback_data": f"back|{session_id}"}])
    return rows


# ----------------------------------------------------------------------
# Registry — handlers are closures over the staff directory so they can
# do per-category lookups without a global.
# ----------------------------------------------------------------------


class ActionRegistry:
    """Maps ``action_id`` strings to handler callables.

    Built-in actions are pre-registered. Custom actions can be added at
    startup via :meth:`register` — useful when a hotel needs an
    integration-specific button.
    """

    def __init__(
        self,
        directory: StaffDirectory | None = None,
        notifier: StaffNotifier | None = None,
    ) -> None:
        self._directory = directory or StaffDirectory.empty()
        self._notifier = notifier or NoopStaffNotifier()
        self._handlers: dict[str, ButtonHandler] = {}
        self.register("find_avail", self._make_find_avail())
        self.register("assign", self._make_assign())
        self.register("back", self._make_back())
        self.register("ack", self._make_ack())
        self.register("resolved", self._make_resolved())
        self.register("enroute", self._make_enroute())
        self.register("onsite", self._make_onsite())
        self.register("dmdone", self._make_dmdone())

    @property
    def directory(self) -> StaffDirectory:
        """Expose the directory so callers (e.g. interactive_sink) can share it."""
        return self._directory

    def register(self, action_id: str, handler: ButtonHandler) -> None:
        if "|" in action_id:
            raise ValueError(f"action_id may not contain '|': {action_id!r}")
        if len(action_id) > 16:
            # Keep IDs short — Telegram caps callback_data at 64 bytes total.
            raise ValueError(f"action_id too long (max 16 chars): {action_id!r}")
        self._handlers[action_id] = handler

    def get(self, action_id: str) -> ButtonHandler | None:
        return self._handlers.get(action_id)

    # ------------------------------------------------------------------
    # Handler factories
    # ------------------------------------------------------------------

    def _make_find_avail(self) -> ButtonHandler:
        directory = self._directory

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=None,
                    toast="Already resolved.",
                )
            if record.assigned_to:
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=_default_keyboard(record, directory),
                    toast=f"Already assigned to {record.assigned_to}.",
                )

            staff_list = directory.get(record.ticket.category)
            if not staff_list:
                logger.warning(
                    "[buttons] find_avail called for {} but no staff configured",
                    record.ticket.category.value,
                )
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=_default_keyboard(record, directory),
                    toast=(
                        f"No {record.ticket.category.value} staff configured. "
                        "Add them in config/staff/<hotel>.yaml."
                    ),
                    show_alert=True,
                )

            return ActionResult(
                new_text=_format_post_with_picker(record, staff_list),
                keyboard=_picker_keyboard(record.session_id, staff_list),
                toast=None,
            )

        return handler

    def _make_assign(self) -> ButtonHandler:
        directory = self._directory
        notifier = self._notifier

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=None,
                    toast="Already resolved.",
                )
            if record.assigned_to:
                # Race: someone else assigned a different person between the
                # picker showing and this tap landing. Honour first-write-wins.
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=_default_keyboard(record, directory),
                    toast=f"Already assigned to {record.assigned_to}.",
                )

            staff_list = directory.get(record.ticket.category)
            try:
                idx = int(event.extra) if event.extra is not None else -1
            except ValueError:
                idx = -1
            if not (0 <= idx < len(staff_list)):
                logger.warning(
                    "[buttons] assign with bad index extra={!r} for {}",
                    event.extra,
                    record.ticket.category.value,
                )
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=_default_keyboard(record, directory),
                    toast="Selection no longer valid.",
                )

            staff = staff_list[idx]
            note = f"Assigned to {staff.name} (ETA {staff.eta_min} min) by {event.actor_name}"

            def _mutate(r: TicketRecord) -> None:
                r.assigned_to = staff.name
                r.status = TicketStatus.ASSIGNED
                r.history.append(
                    ActionEntry(
                        timestamp=TicketStateStore.now(),
                        actor=event.actor_name,
                        action_id="assign",
                        note=note,
                    )
                )

            updated = await store.update(record.session_id, _mutate)
            if updated is None:
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Ticket vanished."
                )

            # DM the staff. The notifier returns the message_id of the DM
            # on success so we can record it for later editing (when the
            # staff or manager mutates the ticket later, both posts update).
            try:
                dm_message_id = await notifier.notify(staff, updated)
            except Exception:
                logger.opt(exception=True).warning(
                    "[buttons] notifier raised unexpectedly for staff={!r}", staff.name
                )
                dm_message_id = None

            if dm_message_id is not None and staff.telegram_chat_id is not None:
                # Record the DM coordinates so DM-button taps can edit both
                # surfaces and channel-side actions can mirror to the DM.
                def _record_dm(r: TicketRecord) -> None:
                    r.dm_chat_id = staff.telegram_chat_id
                    r.dm_message_id = dm_message_id

                updated = await store.update(record.session_id, _record_dm) or updated

            logger.info(
                "[buttons] assigned session={} → {} by {!r} dm_msg_id={}",
                event.session_id,
                staff.name,
                event.actor_name,
                dm_message_id,
            )
            toast_suffix = (
                "" if dm_message_id is not None else " (no DM — staff hasn't messaged the bot yet)"
            )
            return ActionResult(
                new_text=_format_post(updated),
                keyboard=_default_keyboard(updated, directory),
                toast=f"Assigned to {staff.name}{toast_suffix}",
            )

        return handler

    def _make_back(self) -> ButtonHandler:
        directory = self._directory

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            # Pure UX action — no state mutation, just restore the keyboard.
            return ActionResult(
                new_text=_format_post(record),
                keyboard=_default_keyboard(record, directory),
                toast="Cancelled.",
            )

        return handler

    def _make_ack(self) -> ButtonHandler:
        directory = self._directory

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Already resolved."
                )
            if record.claimed_by is not None:
                return ActionResult(
                    new_text=_format_post(record),
                    keyboard=_default_keyboard(record, directory),
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
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Ticket vanished."
                )

            logger.info("[buttons] ack session={} claimer={!r}", event.session_id, claimer)
            return ActionResult(
                new_text=_format_post(updated),
                keyboard=_default_keyboard(updated, directory),
                toast="You've claimed this ticket.",
            )

        return handler

    def _make_resolved(self) -> ButtonHandler:
        directory = self._directory
        notifier = self._notifier

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Already resolved."
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
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Ticket vanished."
                )

            # If an assigned staff exists and we have a DM open with them,
            # mirror the resolved status to their DM (so they see "✓ RESOLVED")
            # and send a thank-you note.
            staff = (
                _find_staff_by_name(directory, updated.ticket.category, updated.assigned_to)
                if updated.assigned_to
                else None
            )
            secondary = _dm_secondary(updated, staff)
            if staff is not None and staff.name != actor:
                # Don't double-DM the staff if they themselves resolved via
                # their DM — they already saw the inline confirmation.
                try:
                    await notifier.notify_resolved(staff, updated)
                except Exception:
                    logger.opt(exception=True).warning(
                        "[buttons] notify_resolved raised for staff={!r}", staff.name
                    )

            logger.info("[buttons] resolved session={} actor={!r}", event.session_id, actor)
            return ActionResult(
                new_text=_format_post(updated),
                keyboard=None,  # remove buttons — terminal state
                toast="Ticket marked resolved. Thank you!",
                secondary=secondary,
            )

        return handler

    def _make_enroute(self) -> ButtonHandler:
        """Handler for the DM-side "I'm on my way" button.

        Marks the ticket EN_ROUTE and refreshes both the DM and the
        channel post — the manager sees "ON THE WAY 🚶" appear without
        leaving the channel.
        """
        directory = self._directory

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Already resolved."
                )
            # Don't backtrack: if already on-site, en-route is a no-op.
            if record.status == TicketStatus.ON_SITE:
                staff = (
                    _find_staff_by_name(directory, record.ticket.category, record.assigned_to)
                    if record.assigned_to
                    else None
                )
                primary_text = (
                    format_assignment_dm(staff, record)
                    if staff is not None
                    else _format_post(record)
                )
                return ActionResult(
                    new_text=primary_text,
                    keyboard=dm_action_keyboard(record) if staff else None,
                    toast="You're already marked on-site.",
                )

            actor = event.actor_name

            def _mutate(r: TicketRecord) -> None:
                r.status = TicketStatus.EN_ROUTE
                r.history.append(
                    ActionEntry(
                        timestamp=TicketStateStore.now(),
                        actor=actor,
                        action_id="enroute",
                        note=f"{actor} is on the way",
                    )
                )

            updated = await store.update(record.session_id, _mutate)
            if updated is None:
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Ticket vanished."
                )

            staff = (
                _find_staff_by_name(directory, updated.ticket.category, updated.assigned_to)
                if updated.assigned_to
                else None
            )
            primary_text = (
                format_assignment_dm(staff, updated) if staff is not None else _format_post(updated)
            )
            primary_keyboard = (
                dm_action_keyboard(updated)
                if staff is not None
                else _default_keyboard(updated, directory)
            )
            logger.info("[buttons] enroute session={} actor={!r}", event.session_id, actor)
            return ActionResult(
                new_text=primary_text,
                keyboard=primary_keyboard,
                toast="🚶 Marked on the way. Manager has been notified.",
                secondary=_channel_secondary(updated, directory),
            )

        return handler

    def _make_onsite(self) -> ButtonHandler:
        """Handler for the DM-side "I'm on-site" button.

        Updates the ticket to ON_SITE and refreshes BOTH the DM (where the
        tap came from) and the channel post (so the manager sees real-time
        progress without leaving the channel).
        """
        directory = self._directory

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Already resolved."
                )

            actor = event.actor_name

            def _mutate(r: TicketRecord) -> None:
                r.status = TicketStatus.ON_SITE
                r.history.append(
                    ActionEntry(
                        timestamp=TicketStateStore.now(),
                        actor=actor,
                        action_id="onsite",
                        note=f"{actor} arrived on-site",
                    )
                )

            updated = await store.update(record.session_id, _mutate)
            if updated is None:
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Ticket vanished."
                )

            staff = (
                _find_staff_by_name(directory, updated.ticket.category, updated.assigned_to)
                if updated.assigned_to
                else None
            )
            # Primary edit: the DM (where the tap came from). Secondary:
            # the channel post (so the manager sees the on-site update).
            primary_text = (
                format_assignment_dm(staff, updated) if staff is not None else _format_post(updated)
            )
            primary_keyboard = (
                dm_action_keyboard(updated)
                if staff is not None
                else _default_keyboard(updated, directory)
            )
            logger.info("[buttons] onsite session={} actor={!r}", event.session_id, actor)
            return ActionResult(
                new_text=primary_text,
                keyboard=primary_keyboard,
                toast="✅ Marked on-site. Manager has been notified.",
                secondary=_channel_secondary(updated, directory),
            )

        return handler

    def _make_dmdone(self) -> ButtonHandler:
        """Handler for the DM-side "Done" button.

        Marks the ticket resolved and refreshes both the DM and the channel
        post. No follow-up notify_resolved DM since the staff is the one
        completing the action.
        """
        directory = self._directory

        async def handler(
            event: ButtonEvent, record: TicketRecord, store: TicketStateStore
        ) -> ActionResult:
            if record.is_terminal():
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Already resolved."
                )

            actor = event.actor_name

            def _mutate(r: TicketRecord) -> None:
                r.status = TicketStatus.RESOLVED
                r.history.append(
                    ActionEntry(
                        timestamp=TicketStateStore.now(),
                        actor=actor,
                        action_id="dmdone",
                        note=f"Resolved by {actor} via DM",
                    )
                )

            updated = await store.update(record.session_id, _mutate)
            if updated is None:
                return ActionResult(
                    new_text=_format_post(record), keyboard=None, toast="Ticket vanished."
                )

            staff = (
                _find_staff_by_name(directory, updated.ticket.category, updated.assigned_to)
                if updated.assigned_to
                else None
            )
            primary_text = (
                format_assignment_dm(staff, updated) if staff is not None else _format_post(updated)
            )
            logger.info("[buttons] dmdone session={} actor={!r}", event.session_id, actor)
            return ActionResult(
                new_text=primary_text,
                keyboard=None,  # terminal — strip buttons from DM
                toast="Thanks! Ticket closed.",
                secondary=_channel_secondary(updated, directory),
            )

        return handler
