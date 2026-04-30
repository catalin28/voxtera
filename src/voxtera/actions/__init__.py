"""Action layer — guest-request tickets and the sinks that deliver them.

Importing from this package gives access to the high-level types the rest of
Voxtera needs to work with the action-taking feature::

    from voxtera.actions import (
        Category,
        HotelConfig,
        Ticket,
        TicketSink,
        TelegramSink,
        load_hotel_config,
    )
"""

from voxtera.actions.hotel_config import HotelConfig, load_hotel_config
from voxtera.actions.sink import TicketSink
from voxtera.actions.telegram_sink import TelegramSink
from voxtera.actions.ticket import Category, Ticket

__all__ = [
    "Category",
    "HotelConfig",
    "Ticket",
    "TicketSink",
    "TelegramSink",
    "load_hotel_config",
]
