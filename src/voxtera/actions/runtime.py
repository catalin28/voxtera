"""ActionRuntime — bundles every long-lived object the actions feature needs.

When the voice bot starts up with ``ACTIONS_ENABLED=true``, we build one
:class:`ActionRuntime` and pass it through to:

- ``pipeline.py`` — uses ``runtime.sink`` and ``runtime.hotel_config`` to
  compose the system prompt and register the ``create_ticket`` LLM tool.
- ``bot.py`` — runs ``runtime.listener`` as a background task alongside
  the voice pipeline so button taps in Telegram are dispatched while
  the bot is also handling voice.

Keeping the wiring inside one factory makes it trivially testable and
keeps ``pipeline.py`` / ``bot.py`` clean of feature-specific imports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from loguru import logger

from voxtera.actions.button_actions import ActionRegistry
from voxtera.actions.hotel_config import HotelConfig, load_hotel_config
from voxtera.actions.interactive_sink import InteractiveTelegramSink
from voxtera.actions.listener import TelegramListener
from voxtera.actions.staff import StaffDirectory
from voxtera.actions.staff_notifier import TelegramStaffNotifier
from voxtera.actions.state import TicketStateStore, UrgencyThresholds


@dataclass
class ActionRuntime:
    """Bundle of every object the actions feature needs at runtime.

    Construct via :func:`build_action_runtime`. ``hotel_config`` and
    ``sink`` are what ``pipeline.py`` reads to wire the ``create_ticket``
    tool into the LLM service. ``listener`` is what ``bot.py`` runs as a
    background task to handle button taps.
    """

    hotel_config: HotelConfig
    store: TicketStateStore
    directory: StaffDirectory
    sink: InteractiveTelegramSink
    notifier: TelegramStaffNotifier
    registry: ActionRegistry
    listener: TelegramListener


def build_action_runtime(hotel_id: str = "demo") -> ActionRuntime:
    """Construct an ActionRuntime for the given hotel.

    Reads ``TELEGRAM_BOT_TOKEN`` from the environment (the channel ID
    comes from the hotel config). Raises :class:`RuntimeError` if the
    token is missing — the caller must gate this behind ``actions_enabled``
    so the voice bot still starts when actions are off.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise RuntimeError(
            "ACTIONS_ENABLED=true but TELEGRAM_BOT_TOKEN is not set. "
            "Either disable actions or add the token to .env."
        )

    hotel_config = load_hotel_config(hotel_id)
    store = TicketStateStore()
    directory = StaffDirectory.for_hotel(hotel_config.hotel_id)
    notifier = TelegramStaffNotifier(bot_token=bot_token)
    sink = InteractiveTelegramSink(
        bot_token=bot_token,
        channel_id=hotel_config.telegram_channel_id,
        store=store,
        directory=directory,
    )
    registry = ActionRegistry(directory=directory, notifier=notifier)
    listener = TelegramListener(
        bot_token=bot_token,
        store=store,
        registry=registry,
        urgency_thresholds=UrgencyThresholds.from_env(),
    )
    logger.info(
        "[actions_runtime] built for hotel={!r} channel={} staff_categories={}",
        hotel_config.hotel_name,
        hotel_config.telegram_channel_id,
        len(directory.categories()),
    )
    return ActionRuntime(
        hotel_config=hotel_config,
        store=store,
        directory=directory,
        sink=sink,
        notifier=notifier,
        registry=registry,
        listener=listener,
    )
