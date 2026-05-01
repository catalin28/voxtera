"""Direct-message notifier for staff after assignment.

When a manager picks a staff member from the picker, the action layer fires
:meth:`StaffNotifier.notify` so the assigned staff gets a Telegram DM with
the ticket details. This complements the channel-side update — the DM
gives staff an immediate notification on their lock screen even when they
aren't actively watching the channel.

Key Telegram constraints to know:

- Bots cannot initiate DMs. The user must send the bot at least one
  message first. We capture their ``chat_id`` (= ``user_id`` in private
  chats) once and store it in ``config/staff/<hotel>.yaml``.
- A bot can only DM users for whom it has a ``chat_id``. There is no
  "DM by username" path — usernames need to be resolved to numeric ids.

The notifier never raises — failures are logged and the action loop
continues. A missed DM is not a reason to fail the assignment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Final

import aiohttp
from loguru import logger

from voxtera.actions.staff import Staff
from voxtera.actions.state import TicketRecord, TicketStatus

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
_REQUEST_TIMEOUT_SECS: Final[float] = 10.0


class StaffNotifier(ABC):
    """Contract for notifying a staff member about a ticket lifecycle event."""

    @abstractmethod
    async def notify(self, staff: Staff, record: TicketRecord) -> int | None:
        """Send the ASSIGNMENT notification to ``staff``.

        Returns the Telegram ``message_id`` of the DM on success (so the
        caller can record it for later editing), or ``None`` on any failure.
        Implementations MUST NOT raise.
        """
        ...

    @abstractmethod
    async def notify_resolved(self, staff: Staff, record: TicketRecord) -> bool:
        """Send a follow-up "ticket resolved" thank-you DM to ``staff``.

        Returns True on success, False on any failure. Never raises.
        """
        ...


class NoopStaffNotifier(StaffNotifier):
    """Default notifier that just logs. Useful when DMs aren't configured."""

    async def notify(self, staff: Staff, record: TicketRecord) -> int | None:
        logger.debug(
            "[staff_notifier] (noop) would notify staff={!r} session={}",
            staff.name,
            record.session_id,
        )
        return None

    async def notify_resolved(self, staff: Staff, record: TicketRecord) -> bool:
        logger.debug(
            "[staff_notifier] (noop) would notify-resolved staff={!r} session={}",
            staff.name,
            record.session_id,
        )
        return True


class TelegramStaffNotifier(StaffNotifier):
    """Sends a Telegram DM to the assigned staff using the same bot token.

    The staff must have DM'd the bot at least once — see
    ``scripts/get_telegram_chat_id.py`` for capturing the chat_id.
    """

    def __init__(self, bot_token: str) -> None:
        if not bot_token:
            raise ValueError("TelegramStaffNotifier: bot_token is required")
        self._bot_token = bot_token
        self._send_url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"

    async def notify(self, staff: Staff, record: TicketRecord) -> int | None:
        if staff.telegram_chat_id is None:
            logger.info(
                "[staff_notifier] no telegram_chat_id for {!r} — skipping DM",
                staff.name,
            )
            return None

        text = format_assignment_dm(staff, record)
        payload = {
            "chat_id": staff.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": dm_action_keyboard(record),
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECS)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self._send_url, json=payload) as resp,
            ):
                data = await resp.json()
                if resp.status == 200 and data.get("ok") is True:
                    msg_id = data["result"]["message_id"]
                    logger.info(
                        "[staff_notifier] DM sent staff={!r} session={} msg_id={}",
                        staff.name,
                        record.session_id,
                        msg_id,
                    )
                    return msg_id
                # Common case: the staff has not DM'd the bot yet.
                # Telegram returns 403 "Forbidden: bot can't initiate
                # conversation with a user". We log and move on.
                logger.warning(
                    "[staff_notifier] DM rejected staff={!r} status={} response={}",
                    staff.name,
                    resp.status,
                    data,
                )
                return None
        except aiohttp.ClientError as e:
            logger.error("[staff_notifier] network error staff={!r}: {}", staff.name, e)
            return None
        except Exception as e:
            logger.exception("[staff_notifier] unexpected error staff={!r}: {}", staff.name, e)
            return None

    async def notify_resolved(self, staff: Staff, record: TicketRecord) -> bool:
        """Send a thank-you DM after the ticket is resolved by someone else."""
        if staff.telegram_chat_id is None:
            return False
        text = (
            f"✓ Ticket {record.session_id} was marked resolved.\n"
            f"Thanks for your work — {record.ticket.category.value}, "
            f"room {record.ticket.room_number}."
        )
        payload = {
            "chat_id": staff.telegram_chat_id,
            "text": text,
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
                    return True
                logger.warning(
                    "[staff_notifier] resolved DM rejected staff={!r} response={}",
                    staff.name,
                    data,
                )
                return False
        except aiohttp.ClientError as e:
            logger.error(
                "[staff_notifier] resolved-DM network error staff={!r}: {}",
                staff.name,
                e,
            )
            return False
        except Exception as e:
            logger.exception(
                "[staff_notifier] resolved-DM unexpected error staff={!r}: {}",
                staff.name,
                e,
            )
            return False


def format_assignment_dm(staff: Staff, record: TicketRecord) -> str:
    """Render the DM body for the staff member at assignment time.

    Public (no leading underscore) because the DM-side handlers in
    ``button_actions`` re-render this same body when state changes.

    The leading color emoji mirrors the channel post — so the staff and
    the manager see the same urgency at a glance, on different surfaces.
    """
    t = record.ticket
    # Lazy import to avoid circular dependency: button_actions imports
    # this module, so we can't import it at module top.
    from voxtera.actions.button_actions import _color_band_for  # noqa: PLC0415

    color = _color_band_for(record)
    lines = [
        f"{color} 🛎  You have a new {t.category.value} assignment.",
        "",
        f"Room: {t.room_number}",
        f"Issue: {t.summary}",
        "",
        f"Guest spoke in: {t.language_detected}",
        f'"{t.original_quote}"',
        "",
        f"Session: {t.session_id}",
        f"ETA expected: {staff.eta_min} min",
    ]
    if record.status == TicketStatus.EN_ROUTE:
        lines.append("")
        lines.append("Status: 🚶 ON MY WAY")
    elif record.status == TicketStatus.ON_SITE:
        lines.append("")
        lines.append("Status: ✅ ON-SITE")
    elif record.status == TicketStatus.RESOLVED:
        lines.append("")
        lines.append("Status: ✓ RESOLVED")
    return "\n".join(lines)


def dm_action_keyboard(record: TicketRecord) -> list[list[dict]]:
    """Buttons shown inside the assigned staff's DM.

    Buttons appear/disappear based on the current state so already-done
    actions are not offered twice:

    - ASSIGNED: 🚶 On my way · ✅ On-site · ✓ Done
    - EN_ROUTE: ✅ On-site · ✓ Done
    - ON_SITE:  ✓ Done
    - RESOLVED: (no action buttons, only the deep-link)

    The deep-link "View in channel" button is always present so the staff
    can jump to the manager-facing post for context.
    """
    sid = record.session_id
    rows: list[list[dict]] = []
    if record.status in (TicketStatus.ASSIGNED, TicketStatus.CLAIMED):
        rows.append([{"text": "🚶 I'm on my way", "callback_data": f"enroute|{sid}"}])
    if record.status not in (TicketStatus.ON_SITE, TicketStatus.RESOLVED):
        rows.append([{"text": "✅ I'm on-site", "callback_data": f"onsite|{sid}"}])
    if record.status != TicketStatus.RESOLVED:
        rows.append([{"text": "✓ Done", "callback_data": f"dmdone|{sid}"}])
    rows.extend(_deep_link_keyboard(record))
    return rows


def _deep_link_keyboard(record: TicketRecord) -> list[list[dict]]:
    """Build a 'View in channel' button that deep-links to the original post.

    For private channels (-100… IDs), Telegram's deep-link format is
    ``https://t.me/c/<channel_id_without_-100>/<message_id>``.
    """
    chat_id = record.chat_id
    # Strip the -100 prefix that Telegram adds to private channel IDs.
    if chat_id.startswith("-100"):
        public_part = chat_id[4:]
    elif chat_id.startswith("-"):
        public_part = chat_id[1:]
    else:
        public_part = chat_id
    url = f"https://t.me/c/{public_part}/{record.message_id}"
    return [[{"text": "View in channel ↗", "url": url}]]
