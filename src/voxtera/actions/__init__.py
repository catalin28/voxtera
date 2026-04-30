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
        wire_actions,
        compose_system_prompt,
    )
"""

from voxtera.actions.button_actions import (
    ActionRegistry,
    ActionResult,
    ButtonEvent,
)
from voxtera.actions.hotel_config import HotelConfig, load_hotel_config
from voxtera.actions.integration import wire_actions
from voxtera.actions.interactive_sink import InteractiveTelegramSink
from voxtera.actions.listener import TelegramListener
from voxtera.actions.prompt import build_actions_prompt_fragment, compose_system_prompt
from voxtera.actions.sink import TicketSink
from voxtera.actions.state import (
    TicketRecord,
    TicketStateStore,
    TicketStatus,
)
from voxtera.actions.telegram_sink import TelegramSink
from voxtera.actions.ticket import Category, Ticket
from voxtera.actions.tool import CREATE_TICKET_FUNCTION_NAME, build_create_ticket_tool

__all__ = [
    "CREATE_TICKET_FUNCTION_NAME",
    "ActionRegistry",
    "ActionResult",
    "ButtonEvent",
    "Category",
    "HotelConfig",
    "InteractiveTelegramSink",
    "TelegramListener",
    "TelegramSink",
    "Ticket",
    "TicketRecord",
    "TicketSink",
    "TicketStateStore",
    "TicketStatus",
    "build_actions_prompt_fragment",
    "build_create_ticket_tool",
    "compose_system_prompt",
    "load_hotel_config",
    "wire_actions",
]
