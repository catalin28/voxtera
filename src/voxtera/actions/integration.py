"""One-call wiring helper for the action-taking feature.

:func:`wire_actions` registers the ``create_ticket`` function with a Pipecat
LLM service and attaches the matching tool schema to the ``LLMContext``.
This is the single entry point bot startup uses; it keeps ``bot.py`` clean
and means the wiring logic is unit-testable in isolation.

Sibling wirings (:func:`wire_web_search`, :func:`wire_find_videos`,
:func:`wire_find_reviews`) follow the same shape: each is gated by an
``*_ENABLED`` env flag **and** the presence of its API key — see
:mod:`voxtera.external_flags` — so the LLM never sees a tool it cannot
actually call. Bot startup is expected to call each wire fn it wants
enabled; missing prerequisites just log a warning and skip.

This module does NOT load hotel config, build the sink, or modify the system
prompt. Those happen in bot startup, and the results are passed in. The
separation is deliberate: each piece is replaceable.
"""

from __future__ import annotations

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallHandler, LLMService

from voxtera import external_flags
from voxtera.actions.find_reviews_handler import make_find_reviews_handler
from voxtera.actions.find_videos_handler import make_find_videos_handler
from voxtera.actions.handler import make_create_ticket_handler
from voxtera.actions.hotel_config import HotelConfig
from voxtera.actions.sink import TicketSink
from voxtera.actions.tool import (
    CREATE_TICKET_FUNCTION_NAME,
    FIND_REVIEWS_FUNCTION_NAME,
    FIND_VIDEOS_FUNCTION_NAME,
    WEB_SEARCH_FUNCTION_NAME,
    build_create_ticket_tool,
    build_find_reviews_tool,
    build_find_videos_tool,
    build_web_search_tool,
)
from voxtera.actions.web_search_handler import make_web_search_handler


def _merge_schema_into_context(context: LLMContext, schema: FunctionSchema) -> None:
    """Append ``schema`` to ``context.tools``, replacing any prior entry of the same name.

    The voice loop must keep running even if upstream registered a weird
    tools value, so this is defensive: a non-iterable or non-``ToolsSchema``
    tools value is treated as empty and logged.
    """
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


def _wire_simple_tool(
    *,
    llm: LLMService,
    context: LLMContext,
    function_name: str,
    schema: FunctionSchema,
    handler: FunctionCallHandler,
    log_tag: str,
) -> None:
    """Common wiring path for a no-config, no-hotel-specific LLM tool."""
    _merge_schema_into_context(context, schema)
    llm.register_function(function_name, handler)
    logger.info("[{}] wired {} tool", log_tag, function_name)


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
    _merge_schema_into_context(context, schema)
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
    """Register the ``web_search`` tool (Tavily) on ``llm`` / ``context``.

    Gated by ``WEB_SEARCH_ENABLED`` env flag AND ``TAVILY_API_KEY``. If
    either is missing, logs a one-line reason and returns without wiring.
    """
    if not external_flags.is_web_search_enabled():
        reason = external_flags.disabled_reason(
            external_flags.ENV_WEB_SEARCH, external_flags.ENV_TAVILY_API_KEY,
        )
        logger.warning("[web-search] tool disabled: {}", reason)
        return

    _wire_simple_tool(
        llm=llm,
        context=context,
        function_name=WEB_SEARCH_FUNCTION_NAME,
        schema=build_web_search_tool(),
        handler=make_web_search_handler(),
        log_tag="web-search",
    )


def wire_find_videos(
    *,
    llm: LLMService,
    context: LLMContext,
) -> None:
    """Register the ``find_hotel_videos`` tool (YouTube Data v3).

    Gated by ``YOUTUBE_SEARCH_ENABLED`` env flag AND ``YOUTUBE_API_KEY``.
    """
    if not external_flags.is_youtube_search_enabled():
        reason = external_flags.disabled_reason(
            external_flags.ENV_YOUTUBE_SEARCH, external_flags.ENV_YOUTUBE_API_KEY,
        )
        logger.warning("[find-videos] tool disabled: {}", reason)
        return

    _wire_simple_tool(
        llm=llm,
        context=context,
        function_name=FIND_VIDEOS_FUNCTION_NAME,
        schema=build_find_videos_tool(),
        handler=make_find_videos_handler(),
        log_tag="find-videos",
    )


def wire_find_reviews(
    *,
    llm: LLMService,
    context: LLMContext,
) -> None:
    """Register the ``find_hotel_reviews`` tool (Google Places API New).

    Gated by ``PLACES_SEARCH_ENABLED`` env flag AND ``GOOGLE_PLACES_API_KEY``.
    The Places key may be unset at code-ship time (operator will configure
    it later); the gate logs the reason and exits cleanly in that case.
    """
    if not external_flags.is_places_search_enabled():
        reason = external_flags.disabled_reason(
            external_flags.ENV_PLACES_SEARCH, external_flags.ENV_GOOGLE_PLACES_API_KEY,
        )
        logger.warning("[find-reviews] tool disabled: {}", reason)
        return

    _wire_simple_tool(
        llm=llm,
        context=context,
        function_name=FIND_REVIEWS_FUNCTION_NAME,
        schema=build_find_reviews_tool(),
        handler=make_find_reviews_handler(),
        log_tag="find-reviews",
    )

