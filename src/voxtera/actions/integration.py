"""One-call wiring helper for the action-taking feature.

:func:`wire_actions` registers the ``create_ticket`` function with a Pipecat
LLM service and attaches the matching tool schema to the ``LLMContext``.
This is the single entry point bot startup uses; it keeps ``bot.py`` clean
and means the wiring logic is unit-testable in isolation.

This module does NOT load hotel config, build the sink, or modify the system
prompt. Those happen in bot startup, and the results are passed in. The
separation is deliberate: each piece is replaceable.
"""

from __future__ import annotations

import os

from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import LLMService

from voxtera.actions.handler import make_create_ticket_handler
from voxtera.actions.hotel_config import HotelConfig
from voxtera.actions.sink import TicketSink
from voxtera.actions.tool import (
    CREATE_TICKET_FUNCTION_NAME,
    WEB_SEARCH_FUNCTION_NAME,
    build_create_ticket_tool,
    build_web_search_tool,
)
from voxtera.actions.web_search_handler import make_web_search_handler


def wire_actions(
    *,
    llm: LLMService,
    context: LLMContext,
    hotel_config: HotelConfig,
    sink: TicketSink,
) -> None:
    """Register the ``create_ticket`` tool and handler on ``llm`` / ``context``.

    The function is keyword-only to make call sites self-documenting at the
    bot.py integration site, which juggles many similarly-typed objects.

    After this call:
    - ``llm`` has a registered handler for ``create_ticket``.
    - ``context.tools`` includes the ``create_ticket`` schema.
    - Any subsequent LLM completion will be told it can use the tool.

    The function is idempotent for the schema (calling twice merges into the
    existing ToolsSchema rather than duplicating). The handler registration
    will overwrite a prior handler with the same name, which is the safe
    behaviour during dev reloads.
    """
    schema = build_create_ticket_tool(hotel_config)
    handler = make_create_ticket_handler(sink=sink, hotel_config=hotel_config)

    # Merge into any existing tools the context already carries. We don't
    # know what else upstream may have registered; preserve their schemas.
    # Be defensive: `standard_tools` is documented as a list but we filter
    # `None` entries and handle the case where it is missing or non-iterable
    # rather than crash mid-call (the voice loop must keep running).
    existing = context.tools
    standard_tools: list = []
    if isinstance(existing, ToolsSchema):
        prior = existing.standard_tools or []
        try:
            standard_tools = [
                t for t in prior if t is not None and getattr(t, "name", None) != schema.name
            ]
        except TypeError:
            logger.warning(
                "[actions] existing context.tools.standard_tools was not iterable; "
                "starting from empty list"
            )
            standard_tools = []
    standard_tools.append(schema)
    context.set_tools(ToolsSchema(standard_tools=standard_tools))

    llm.register_function(CREATE_TICKET_FUNCTION_NAME, handler)
    logger.info(
        "[actions] wired create_ticket: hotel={!r} categories={} sink={}",
        hotel_config.hotel_name,
        len(hotel_config.allowed_categories),
        type(sink).__name__,
    )


def wire_web_search(
    *,
    llm: LLMService,
    context: LLMContext,
) -> None:
    """Register the ``web_search`` tool and handler on ``llm`` / ``context``.

    Only wires if ``TAVILY_API_KEY`` is set in the environment. If the key
    is missing, logs a warning and skips — the bot runs without web search.

    After this call:
    - ``llm`` has a registered handler for ``web_search``.
    - ``context.tools`` includes the ``web_search`` schema.
    """
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        logger.warning(
            "[web-search] TAVILY_API_KEY not set — web_search tool disabled. "
            "Add the key to .env to enable."
        )
        return

    schema = build_web_search_tool()
    handler = make_web_search_handler()

    existing = context.tools
    standard_tools: list = []
    if isinstance(existing, ToolsSchema):
        prior = existing.standard_tools or []
        try:
            standard_tools = [
                t for t in prior if t is not None and getattr(t, "name", None) != schema.name
            ]
        except TypeError:
            standard_tools = []
    standard_tools.append(schema)
    context.set_tools(ToolsSchema(standard_tools=standard_tools))

    llm.register_function(WEB_SEARCH_FUNCTION_NAME, handler)
    logger.info("[web-search] wired web_search tool")
