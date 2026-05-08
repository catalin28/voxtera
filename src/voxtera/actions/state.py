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
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from voxtera.actions.ticket import Category, Ticket


class UrgencyLevel(IntEnum):
    """How urgent an open ticket is, computed from how long it has been open.

    The aging monitor in :class:`~voxtera.actions.listener.TelegramListener`
    re-edits the channel post when this level changes, so managers see the
    visual escalation without polling the channel themselves.
    """

    NORMAL = 0
    ATTENTION = 1  # yellow — early warning, action expected soon
    URGENT = 2  # orange — getting concerning
    CRITICAL = 3  # red — needs immediate attention


@dataclass(frozen=True)
class UrgencyThresholds:
    """Time thresholds (in seconds) that drive urgency escalation.

    Demo defaults are aggressive (1m/3m/6m) so colors visibly shift during
    a short demo. Production hotels would use longer windows like 5m/15m/30m.
    Override via env vars at startup.
    """

    attention_secs: int = 60
    urgent_secs: int = 180
    critical_secs: int = 360

    def level_for_age(self, age_secs: float) -> UrgencyLevel:
        """Pick the urgency level for a given age."""
        if age_secs >= self.critical_secs:
            return UrgencyLevel.CRITICAL
        if age_secs >= self.urgent_secs:
            return UrgencyLevel.URGENT
        if age_secs >= self.attention_secs:
            return UrgencyLevel.ATTENTION
        return UrgencyLevel.NORMAL

    @classmethod
    def from_env(cls) -> UrgencyThresholds:
        """Load thresholds from env vars, falling back to demo defaults."""
        return cls(
            attention_secs=int(os.getenv("VOXTERA_URGENCY_ATTENTION_SECS", "60")),
            urgent_secs=int(os.getenv("VOXTERA_URGENCY_URGENT_SECS", "180")),
            critical_secs=int(os.getenv("VOXTERA_URGENCY_CRITICAL_SECS", "360")),
        )


class TicketStatus(StrEnum):
    """Lifecycle of a posted ticket. Linear, no rewinding for v1.

    Status progression for the DM-driven flow:
    ``OPEN → ASSIGNED → EN_ROUTE → ON_SITE → RESOLVED``
    Each transition is triggered by a tap (manager from channel,
    or assigned staff from DM) and mirrors to the other surface so
    everyone sees the same state in real time.
    """

    OPEN = "open"
    CLAIMED = "claimed"
    ASSIGNED = "assigned"
    EN_ROUTE = "en_route"
    ON_SITE = "on_site"
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
    # When the assigned staff has a Telegram chat_id and we successfully
    # DM them, we record both ids here so DM-button taps can edit BOTH the
    # DM and the channel post (cross-message UI consistency).
    dm_chat_id: int | None = None
    dm_message_id: int | None = None
    # Last urgency level the aging monitor applied. Used to skip no-op edits
    # when a ticket's urgency hasn't actually changed since the last scan.
    urgency_level: UrgencyLevel = UrgencyLevel.NORMAL

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


# Per-category default button layout for newly-posted tickets.
# Tuple format: (action_id, label). The interactive sink filters out
# `find_avail` for categories that have no staff in StaffDirectory.
# Categories not listed here fall back to a single Resolved button.
DEFAULT_BUTTON_LAYOUT: dict[Category, list[tuple[str, str]]] = {
    Category.MAINTENANCE: [
        ("find_avail", "🔍 Find available technician"),
        ("ack", "✅ I'm on it"),
        ("resolved", "✓ Resolved"),
    ],
    Category.CONCIERGE: [
        ("find_avail", "🔍 Find available concierge"),
        ("ack", "✅ I'm on it"),
        ("resolved", "✓ Resolved"),
    ],
    Category.RESTAURANT: [
        ("find_avail", "🔍 Find available staff"),
        ("ack", "✅ I'm on it"),
        ("resolved", "✓ Resolved"),
    ],
    Category.HOUSEKEEPING: [
        ("find_avail", "🔍 Find available housekeeping"),
        ("ack", "✅ I'm on it"),
        ("resolved", "✓ Resolved"),
    ],
    Category.LOST_AND_FOUND: [
        ("find_avail", "🔍 Find lost & found desk"),
        ("ack", "✅ I'm on it"),
        ("resolved", "✓ Resolved"),
    ],
    Category.EMERGENCY: [
        ("find_avail", "🚨 Find on-call staff"),
        ("ack", "✅ I'm responding"),
        ("resolved", "✓ Resolved"),
    ],
    # Reservation, Complaint, Feedback, Other: minimal layout, no picker.
    # Add staff to config/staff/<hotel>.yaml + extend here if needed.
}
