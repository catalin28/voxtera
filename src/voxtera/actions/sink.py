"""The abstract :class:`TicketSink` contract.

A ``TicketSink`` is whatever delivers a :class:`~voxtera.actions.ticket.Ticket`
to its destination — a Telegram channel today, a Freshdesk or Zendesk REST
API tomorrow. The bot's conversation logic and LLM tool definitions stay
identical across sinks; only the transport changes. Wiring the active sink
into the pipeline is the job of bot startup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from voxtera.actions.ticket import Ticket


class TicketSink(ABC):
    """Contract for any backend that can receive Tickets."""

    @abstractmethod
    async def send(self, ticket: Ticket) -> bool:
        """Deliver ``ticket`` to the backend.

        Returns ``True`` on successful delivery, ``False`` on any error
        (network, auth, rate limit, malformed payload, etc.).

        Implementations MUST NOT raise. The bot's voice loop calls this from
        an LLM tool callback and cannot tolerate exceptions from a side-channel
        action — the call must always complete so the bot can apologize to
        the guest if needed. Catch every exception, log it via loguru, and
        return ``False``.
        """
        ...
