"""In-memory ticket-state store for the interactive actions feature.

Tracks the lifecycle of a ticket once it has been posted to Telegram:
which Telegram ``message_id`` it lives at, who claimed it, who got
assigned, and whether it is still open. The store is process-local —
restarting the bot drops all state. That is fine for a demo. SQLite
persistence is the obvious upgrade and is documented in
``ACTIONS_FEATURE_PLAN.md`` Phase 8.

Thread-safety: a single asyncio Lock protects the dict. We expect
contention only when two staff members tap buttons within the same
event-loop tick, which is rare but not impossible.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from voxtera.actions.ticket import Category, Ticket


class TicketStatus(str, Enum):
    """Lifecycle of a posted ticket. Linear, no rewinding for v1."""

    OPEN = "open"
    CLAIMED = "claimed"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"


@dataclass
class ActionEntry:
    """One entry in a ticket's action history (audit trail)."""

    timestamp: datetime
    actor: str  # Telegram username/first_name of the staff member
    action_id: str
    note: str  # human-readable summary, shown in the updated post


@dataclass
class TicketRecord:
    """Per-ticket runtime state, mutated as buttons are tapped."""

    session_id: str
    ticket: Ticket
    chat_id: str
    message_id: int
    status: TicketStatus = TicketStatus.OPEN
    claimed_by: str | None = None  # staff display name once "I'm on it" tapped
    assigned_to: str | None = None  # mock staff name picked by Find-available
    history: list[ActionEntry] = field(default_factory=list)

    def is_terminal(self) -> bool:
        """True once the ticket is resolved — buttons should no longer mutate it."""
        return self.status == TicketStatus.RESOLVED


class TicketStateStore:
    """Process-local store of TicketRecords, keyed by session_id."""

    def __init__(self) -> None:
        self._records: dict[str, TicketRecord] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: TicketRecord) -> None:
        """Insert a freshly-posted ticket. Overwrites any record with the same id."""
        async with self._lock:
            self._records[record.session_id] = record

    async def get(self, session_id: str) -> TicketRecord | None:
        """Read a record by session_id, or None if unknown (e.g. bot restarted)."""
        async with self._lock:
            return self._records.get(session_id)

    async def update(self, session_id: str, mutator) -> TicketRecord | None:
        """Atomically apply ``mutator(record)`` if the record exists.

        ``mutator`` is a callable that takes a TicketRecord and either mutates
        it in place or returns a new value (None means "no change"). The
        store-wide lock is held for the duration, so this is the right place
        to enforce first-click-wins semantics.
        """
        async with self._lock:
            record = self._records.get(session_id)
            if record is None:
                return None
            mutator(record)
            return record

    async def all_open(self) -> list[TicketRecord]:
        """Snapshot of every non-resolved ticket, useful for diagnostics."""
        async with self._lock:
            return [r for r in self._records.values() if not r.is_terminal()]

    @staticmethod
    def now() -> datetime:
        """UTC timestamp helper kept here so callers don't import datetime twice."""
        return datetime.now(UTC)


# Default registry mapping. Only Maintenance has full button wiring for v1;
# other categories fall back to a generic Acknowledge button. Easy to expand
# without touching listener.py — just edit this dict.
DEFAULT_BUTTON_LAYOUT: dict[Category, list[tuple[str, str]]] = {
    Category.MAINTENANCE: [
        ("find_avail", "🔍 Find available technician"),
        ("ack", "✅ I'm on it"),
        ("resolved", "✓ Resolved"),
    ],
    # Catch-all default for categories without explicit buttons. Used by
    # `interactive_sink.build_keyboard` if the category is missing here.
}
