"""Function handler for the ``create_ticket`` LLM tool.

When Claude invokes the ``create_ticket`` tool, Pipecat calls the handler
returned by :func:`make_create_ticket_handler` with a
:class:`~pipecat.services.llm_service.FunctionCallParams` object. The handler:

1. Validates and coerces the arguments into a :class:`~voxtera.actions.ticket.Ticket`.
2. Calls the active sink's ``send`` method.
3. Reports the outcome back to Claude via ``params.result_callback`` so
   Claude can confirm to the guest aloud.

The handler is constructed via a closure over the sink, so the registration
side does not need to know which sink (Telegram, Freshdesk, ...) is active.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallHandler, FunctionCallParams

from voxtera.actions.hotel_config import HotelConfig
from voxtera.actions.sink import TicketSink
from voxtera.actions.ticket import Category, Ticket


def make_create_ticket_handler(sink: TicketSink, hotel_config: HotelConfig) -> FunctionCallHandler:
    """Build a Pipecat function handler closed over ``sink`` and ``hotel_config``.

    The returned handler matches Pipecat's ``FunctionCallHandler`` protocol:
    a single async function taking :class:`FunctionCallParams` and calling
    ``params.result_callback`` exactly once.
    """

    async def handler(params: FunctionCallParams) -> None:
        args = params.arguments or {}
        try:
            ticket = _build_ticket_from_args(args, hotel_config)
        except ValueError as e:
            logger.warning("[actions] create_ticket called with invalid args: {}", e)
            await _safe_callback(
                params,
                {
                    "status": "rejected",
                    "reason": str(e),
                    "guidance": (
                        "The arguments did not parse. Apologize briefly to the guest "
                        "and ask them to clarify the missing information."
                    ),
                },
            )
            return

        # Defense in depth: the sink contract says send() never raises, but
        # we wrap anyway. A buggy future sink (Freshdesk, Zendesk, ...) cannot
        # be allowed to crash the voice loop.
        try:
            ok = await sink.send(ticket)
        except Exception as e:
            logger.exception(
                "[actions] sink raised unexpectedly session={}: {}",
                ticket.session_id,
                e,
            )
            ok = False

        if ok:
            logger.info(
                "[actions] Ticket filed session={} category={} room={}",
                ticket.session_id,
                ticket.category.value,
                ticket.room_number,
            )
            await _safe_callback(
                params,
                {
                    "status": "filed",
                    "category": ticket.category.value,
                    "session_id": ticket.session_id,
                    "guidance": (
                        f"Confirm to the guest in their language that the "
                        f"{ticket.category.value} team has been notified. "
                        f"Keep it to one short sentence."
                    ),
                },
            )
        else:
            logger.error(
                "[actions] Ticket sink failed session={} category={}",
                ticket.session_id,
                ticket.category.value,
            )
            await _safe_callback(
                params,
                {
                    "status": "failed",
                    "session_id": ticket.session_id,
                    "guidance": (
                        "The ticket could not be delivered. Apologize briefly in the "
                        "guest's language and suggest they call the front desk directly."
                    ),
                },
            )

    return handler


async def _safe_callback(params: FunctionCallParams, result: Any) -> None:
    """Invoke the LLM result callback, swallowing any exception it raises.

    Pipecat's ``result_callback`` is expected to be reliable, but if the LLM
    service is shutting down or an internal queue is closed, the callback can
    raise. Such an exception would otherwise propagate into Pipecat's event
    loop and could disrupt the call. Logging and continuing is preferred —
    the voice loop will recover on the next user turn.
    """
    try:
        await params.result_callback(result)
    except Exception as e:
        logger.exception("[actions] result_callback raised: {}", e)


def _build_ticket_from_args(args: Any, hotel_config: HotelConfig) -> Ticket:
    """Coerce raw tool arguments into a Ticket. Raises ValueError on bad input."""
    # Require a callable .get(); a class with a `get` *attribute* that is not a
    # method would otherwise sneak past a `hasattr(...)` check and fail later
    # with a confusing error.
    get = getattr(args, "get", None)
    if not callable(get):
        raise ValueError(f"Expected mapping for arguments, got {type(args).__name__}")

    # Length bounds keep a runaway argument from a misbehaving model from
    # blowing up the Telegram payload (which has its own truncation, but we'd
    # rather reject early with a clear error than ship garbage).
    category_raw = _require_str(args, "category", max_len=64)
    summary = _require_str(args, "summary", max_len=500)
    room_number = _require_str(args, "room_number", max_len=64)
    original_quote = _require_str(args, "original_quote", max_len=1000)
    language_detected = _require_str(args, "language_detected", max_len=64)

    try:
        category = Category(category_raw)
    except ValueError as e:
        valid = ", ".join(c.value for c in hotel_config.allowed_categories)
        raise ValueError(
            f"category={category_raw!r} is not one of the allowed categories ({valid})"
        ) from e

    if category not in hotel_config.allowed_categories:
        valid = ", ".join(c.value for c in hotel_config.allowed_categories)
        raise ValueError(f"category={category.value!r} is not enabled for this hotel ({valid})")

    return Ticket(
        category=category,
        summary=summary,
        room_number=room_number,
        original_quote=original_quote,
        language_detected=language_detected,
    )


def _require_str(args: Any, key: str, *, max_len: int) -> str:
    """Pull ``key`` from the arg map and confirm it is a sane non-empty string."""
    value = args.get(key)
    if value is None:
        raise ValueError(f"missing required argument {key!r}")
    if not isinstance(value, str):
        raise ValueError(f"argument {key!r} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"argument {key!r} must not be empty")
    if len(stripped) > max_len:
        raise ValueError(f"argument {key!r} exceeds max length {max_len} (got {len(stripped)})")
    if "\x00" in stripped:
        # Null bytes break common transports (Telegram, JSON serializers in
        # some clients) and are never legitimately part of guest speech.
        raise ValueError(f"argument {key!r} contains a null byte")
    return stripped
