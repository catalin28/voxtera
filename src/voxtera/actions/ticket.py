"""The Ticket data structure and Category enum used by the action layer.

A `Ticket` represents a single guest request (complaint, booking, maintenance
issue, etc.) that should be forwarded to hotel staff. Tickets are produced by
the `create_ticket` LLM tool and consumed by a `TicketSink` (Telegram for the
demo, Freshdesk/Zendesk later).

Keeping this module pure — no I/O, no third-party imports — means the LLM
tool layer can construct Tickets without pulling Telegram or HTTP
dependencies, which keeps test surface small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4


class Category(str, Enum):
    """Allowed ticket categories. Mirrors the enum the LLM tool chooses from.

    String values are the human-readable labels shown in the Telegram prefix
    (e.g. ``[Maintenance]``). Update this list, the per-hotel allowed-category
    config, and the system prompt's tool description together — they must
    stay in sync or the LLM will produce categories the sink rejects.
    """

    MAINTENANCE = "Maintenance"
    RESERVATION = "Reservation"
    CONCIERGE = "Concierge"
    RESTAURANT = "Restaurant"
    HOUSEKEEPING = "Housekeeping"
    LOST_AND_FOUND = "Lost & Found"
    COMPLAINT = "Complaint"
    EMERGENCY = "Emergency"
    FEEDBACK = "Feedback"
    OTHER = "Other"


def _new_session_id() -> str:
    """Generate a session id of the form ``vox-YYYY-MM-DD-HHMM-xxxx``.

    The short hex suffix avoids collisions when two tickets are filed in the
    same minute (e.g. a guest reporting two issues back-to-back).
    """
    now = datetime.now(UTC)
    short = uuid4().hex[:4]
    return f"vox-{now:%Y-%m-%d-%H%M}-{short}"


@dataclass(frozen=True)
class Ticket:
    """A structured guest request ready for delivery to staff.

    The ``summary`` field MUST be in the hotel's official language (translated
    by the LLM during the tool call). The ``original_quote`` field is the
    guest's verbatim phrasing in their own language — preserved so staff can
    read what was actually said in case translation lost nuance.
    """

    category: Category
    summary: str  # In hotel's official language.
    room_number: str
    original_quote: str  # In guest's spoken language, verbatim.
    language_detected: str  # BCP-47 code or human label, e.g. "fr-FR" or "French".
    session_id: str = field(default_factory=_new_session_id)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
