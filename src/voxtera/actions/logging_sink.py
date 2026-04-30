"""A dummy :class:`TicketSink` that logs tickets to the console.

Use this when you don't have a Telegram bot token (or any real backend)
but want to verify the LLM is calling the ``create_ticket`` tool correctly.
"""

from __future__ import annotations

from loguru import logger

from voxtera.actions.sink import TicketSink
from voxtera.actions.ticket import Ticket


class LoggingSink(TicketSink):
    """Prints the ticket to the terminal instead of posting anywhere."""

    async def send(self, ticket: Ticket) -> bool:
        logger.info(
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  📋 TICKET FILED (LoggingSink — no Telegram)            ║\n"
            "╠══════════════════════════════════════════════════════════╣\n"
            "║  Category:  {category:<42} ║\n"
            "║  Room:      {room:<42} ║\n"
            "║  Summary:   {summary:<42} ║\n"
            "║  Quote:     {quote:<42} ║\n"
            "║  Language:  {lang:<42} ║\n"
            "║  Session:   {session:<42} ║\n"
            "╚══════════════════════════════════════════════════════════╝",
            category=ticket.category.value,
            room=ticket.room_number,
            summary=ticket.summary[:42],
            quote=ticket.original_quote[:42],
            lang=ticket.language_detected,
            session=ticket.session_id[:42],
        )
        return True
