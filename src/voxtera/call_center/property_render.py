"""Property render — ONE LLM that talks AND files tickets (legacy parity).

The proper port of the old hotel brain's action design. The previous
iteration split the roles: the render LLM talked (and happily PROMISED
actions it couldn't take — "I'll have the front desk ring the restaurant"),
while a classifier-triggered state machine filed tickets (and missed
requests entirely when call STT chopped them into fragments like
"to book a table." / "at La Petite Terrasse.").

Here the answering LLM itself holds the ``create_ticket`` tool, with the
legacy prompt fragment (``voxtera.actions.prompt``) verbatim: it gathers the
room number, summarizes, asks "shall I send this?", and files only on the
guest's yes — across turns, via the conversation transcript, exactly like
the old voice bot. Promises and actions cannot diverge because they come
from the same model call.

When the ticket layer is unavailable (no TELEGRAM_BOT_TOKEN), the tool is
absent and an explicit no-promises rule is added instead, so the model
gracefully refers the guest to the front desk rather than inventing actions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

# Reuse the shared render plumbing so prompts/formatting stay byte-identical
# with the travel path (private-by-underscore but same package by design).
from voxtera.call_center.concierge import (
    LLM_STOP_SEQUENCES,
    _anthropic,
    _build_render_user_msg,
    _with_persona,
)
from voxtera.call_center.hotel_time import hotel_time_note
from voxtera.call_center.intents import booking_guidance_block, booking_recap
from voxtera.call_center.session import build_message_turns

# Max talk→tool→talk rounds. One ticket per turn is the contract (the old
# handler allowed a single retry on argument rejection — round 3 covers it).
_MAX_ROUNDS = 3

_NO_ACTIONS_RULE = """

ACTIONS — read carefully.
You CANNOT perform actions: no bookings, no reservations, no orders, no
messages to staff. NEVER say you will arrange, book, reserve, pass along or
"have the front desk" do anything — those are promises nothing will keep.
When the guest asks for an action, answer any informational part from the
evidence and warmly direct them to the front desk or concierge desk for the
action itself."""


def _anthropic_tool_schema(hotel_config: Any) -> dict[str, Any]:
    """The legacy create_ticket schema in Anthropic ``tools`` format."""
    from voxtera.actions.tool import _build_create_ticket_spec

    spec = _build_create_ticket_spec(hotel_config)
    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "input_schema": spec.get("parameters") or {"type": "object", "properties": {}},
    }


def _validate_ticket_args(args: Any, hotel_config: Any) -> dict[str, str]:
    """Coerce tool args to ticket fields. Raises ValueError on bad input.

    Mirrors the legacy ``actions.handler._build_ticket_from_args`` checks
    without importing the Pipecat-coupled handler module.
    """
    from voxtera.actions.ticket import Category

    if not callable(getattr(args, "get", None)):
        raise ValueError(f"expected mapping for arguments, got {type(args).__name__}")

    def require(key: str, max_len: int) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"missing or empty required argument {key!r}")
        value = value.strip()
        if len(value) > max_len:
            raise ValueError(f"argument {key!r} exceeds max length {max_len}")
        if "\x00" in value:
            raise ValueError(f"argument {key!r} contains a null byte")
        return value

    category_raw = require("category", 64)
    fields = {
        "summary": require("summary", 500),
        "room_number": require("room_number", 64),
        "original_quote": require("original_quote", 1000),
        "language_detected": require("language_detected", 64),
    }
    try:
        category = Category(category_raw)
    except ValueError as e:
        valid = ", ".join(c.value for c in hotel_config.allowed_categories)
        raise ValueError(f"category={category_raw!r} not one of ({valid})") from e
    if category not in hotel_config.allowed_categories:
        valid = ", ".join(c.value for c in hotel_config.allowed_categories)
        raise ValueError(f"category={category.value!r} not enabled for this hotel ({valid})")
    fields["category"] = category.value
    return fields


def booking_fingerprint(category: str, summary: str) -> str:
    """Stable id for a ticket's content, for cross-turn duplicate suppression.

    Normalises case/whitespace/punctuation so the same booking filed twice
    (e.g. the model re-confirming on a trailing fragment turn) hashes equal.
    """
    norm = re.sub(r"[^a-z0-9]+", " ", f"{category}|{summary}".lower()).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()  # noqa: S324 — dedup id, not security


async def _execute_create_ticket(
    args: Any,
    *,
    hotel_id: str,
    ticketer: Any,
    hotel_config: Any,
    recent_fps: set[str],
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Run one create_ticket call. Returns (tool_result_payload, ticket, fingerprint).

    The payloads mirror the legacy handler's statuses + guidance strings, so
    the model's follow-up behaviour carries over unchanged. ``recent_fps`` holds
    fingerprints of bookings already filed earlier in the session; an identical
    booking is NOT delivered again (idempotency) so a re-confirmation on a
    fragment turn can't post a duplicate ticket.
    """
    try:
        fields = _validate_ticket_args(args, hotel_config)
    except ValueError as e:
        logger.warning("[property-render] create_ticket invalid args: {}", e)
        return (
            {
                "status": "rejected",
                "reason": str(e),
                "guidance": (
                    "The arguments did not parse. Apologize briefly to the guest "
                    "and ask them to clarify the missing information."
                ),
            },
            None,
            None,
        )

    fp = booking_fingerprint(fields["category"], fields["summary"])
    if fp in recent_fps:
        logger.info("[property-render] duplicate booking suppressed (fp={})", fp[:8])
        return (
            {
                "status": "already_filed",
                "guidance": (
                    "This exact booking was ALREADY filed moments ago — do NOT "
                    "file it again. Briefly reassure the guest it is taken care of."
                ),
            },
            None,
            fp,
        )

    ticket = await ticketer.file(
        hotel_id=hotel_id,
        fields=fields,
        original_quote=fields["original_quote"],
    )
    if ticket:
        return (
            {
                "status": "filed",
                "category": ticket["category"],
                "session_id": ticket["session_id"],
                "guidance": (
                    f"Confirm to the guest in their language that the "
                    f"{ticket['category']} team has been notified. "
                    f"Keep it to one short sentence."
                ),
            },
            ticket,
            fp,
        )
    return (
        {
            "status": "failed",
            "guidance": (
                "The ticket could not be delivered. Apologize briefly in the "
                "guest's language and suggest they call the front desk directly."
            ),
        },
        None,
        None,
    )


async def render_property_turn(
    *,
    payload: dict[str, Any],
    hotel_id: str,
    ticketer: Any | None,
    model: str,
    on_delta: Callable[[str], Awaitable[None]] | None = None,
    client: Any | None = None,
    force_tool: bool = False,
    recent_fps: list[str] | None = None,
) -> dict[str, Any]:
    """Answer one hotel-mode turn; the model may file a Telegram ticket.

    Args:
        payload: The standard render payload (utterance, transcript,
            retrieval evidence, brief flag) — same shape as ``_render``.
        hotel_id: The property scope.
        ticketer: PropertyTicketer (or None → no tool, no-promises rule).
        model: Anthropic model id.
        on_delta: Streamed text callback (voice path); None for sync JSON.
        client: Anthropic client override (tests).
        force_tool: When True, the first round is forced to call create_ticket
            (``tool_choice`` = the tool). Set by the pipeline when the guest just
            affirmed a pending booking, so a spoken "confirmed" can't diverge
            from an actual filing. Released after the first round so a rejected
            (incomplete) call falls back to asking for the missing detail.
        recent_fps: Fingerprints of bookings already filed earlier this session;
            an identical booking is suppressed (not delivered twice).

    Returns:
        {"answer": str, "ticket": {...} | None, "filed_fp": str | None}
    """
    runtime = ticketer.runtime(hotel_id) if ticketer is not None else None

    brief = bool(payload.get("brief"))
    prompt_name = "travel_agent_voice_render_brief" if brief else "concierge_render"
    # Photo offers ([OFFER:<id>]) only when the channel can deliver an image.
    # Text/chat (brief=False) keeps its historical default of True (the
    # WhatsApp webhook strips the tag + delivers). Voice (brief=True) must
    # OPT IN via the payload "images" flag — only the WhatsApp call bot sets
    # it; the web orb doesn't, so it never speaks a tag it cannot fulfil.
    include_images = bool(payload.get("images", not brief))
    # Menu PDF offers: the channel can deliver a document to the guest's chat.
    # Set by the WhatsApp call bot (voice); the web orb leaves it off so it
    # never speaks a [MENU:] tag it cannot fulfil.
    include_menus = bool(payload.get("menus", False))
    system_text = _with_persona(
        prompt_name,
        include_images=include_images,
        include_menus=include_menus,
        hotel_id=hotel_id,  # per-hotel prompt override (prompts/<hotel_id>/…)
    )
    tools: list[dict[str, Any]] = []
    can_book = runtime is not None
    if can_book:
        from voxtera.actions.prompt import build_actions_prompt_fragment

        system_text += "\n" + build_actions_prompt_fragment(runtime.hotel_config)
        tools.append(_anthropic_tool_schema(runtime.hotel_config))
        # Phase 4 — always include the booking-collection rules when the
        # property can file tickets. This does NOT tell the model a booking is
        # happening: the MODEL decides that from the conversation. The block
        # only shapes HOW it collects once a booking is underway (guest_type
        # first, one slot per turn, in-house→room / external→phone·email,
        # absolute-date read-back, optional extras last). Generated from the
        # declarative schemas in intents.py so the two never drift.
        system_text += "\n\n" + booking_guidance_block()
    else:
        system_text += _NO_ACTIONS_RULE

    client = client or _anthropic()
    # Prior dialogue rides as role-separated ``messages`` (P0 prompt-leak fix).
    # The current turn's payload (utterance + retrieval JSON) is the FINAL user
    # message. Tool-call rounds below append assistant/user pairs on top.
    history_messages = build_message_turns(payload.get("history"))
    user_content = _build_render_user_msg(payload)
    # The booking time anchor carries a changing timestamp, so it rides on the
    # CURRENT user message (never the cached system prompt) — same rule as
    # TimeContextInjector. It lets the model turn "tomorrow 7pm" into an
    # absolute date in the hotel's timezone and read it back at confirmation.
    # Included whenever the property can book; harmless on non-booking turns.
    if can_book:
        user_content += "\n\n" + hotel_time_note(payload.get("hotel_timezone"))
        # Slots extracted on prior turns (by the parallel booking_extract call,
        # persisted by the pipeline) ride back as a LOCKED recap so the model
        # never re-asks or silently changes a detail the guest already gave.
        recap = booking_recap(payload.get("booking_slots"))
        if recap:
            user_content += "\n\n" + recap
    messages: list[dict[str, Any]] = [
        *history_messages,
        {"role": "user", "content": user_content},
    ]
    parts: list[str] = []
    ticket: dict[str, Any] | None = None
    filed_fp: str | None = None
    recent = set(recent_fps or [])
    # Voice (brief=True) caps at ~200 chars — see
    # travel_agent_voice_render_brief.md. 140 tokens gives headroom for
    # Turkish/Russian (worse char-to-token ratio) while still pushing the
    # model to stay short.
    max_tokens = 140 if brief else 512

    # Force the file on the confirm turn so the model can't speak a confirmation
    # without actually calling the tool (the divergence that left bookings off
    # Telegram). Only meaningful when the property can book.
    tool_choice: dict[str, str] | None = (
        {"type": "tool", "name": "create_ticket"} if force_tool and can_book and tools else None
    )
    for round_no in range(_MAX_ROUNDS):
        kwargs: dict[str, Any] = {}
        forcing_tool = False
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
                forcing_tool = tool_choice.get("type") == "tool"
        # A forced tool round emits ONLY the create_ticket arguments (no speech),
        # which easily exceed the brief ~140-token spoken cap and truncate into
        # an invalid tool call (the booking then silently never files). Give the
        # JSON args room; the spoken confirmation comes on the next round at the
        # normal cap.
        round_max_tokens = 512 if forcing_tool else max_tokens
        async with client.messages.stream(
            model=model,
            max_tokens=round_max_tokens,
            stop_sequences=LLM_STOP_SEQUENCES,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            **kwargs,
        ) as stream:
            async for delta in stream.text_stream:
                parts.append(delta)
                if on_delta is not None:
                    await on_delta(delta)
            final = await stream.get_final_message()

        if getattr(final, "stop_reason", None) != "tool_use":
            break
        tool_use = next((b for b in final.content if getattr(b, "type", "") == "tool_use"), None)
        if tool_use is None:  # defensive: stop_reason lied
            break
        logger.info(
            "[property-render] create_ticket call (round {}): {}",
            round_no + 1,
            {k: str(v)[:60] for k, v in (tool_use.input or {}).items()},
        )
        result_payload, ticket, fp = await _execute_create_ticket(
            tool_use.input,
            hotel_id=hotel_id,
            ticketer=ticketer,
            hotel_config=runtime.hotel_config,
            recent_fps=recent,
        )

        # Spoken text flows across the tool boundary — keep a natural pause.
        if parts and parts[-1] and not parts[-1].endswith((" ", "\n")):
            parts.append(" ")
        messages.append({"role": "assistant", "content": final.content})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": json.dumps(result_payload),
                    }
                ],
            }
        )
        status = result_payload.get("status")
        if status == "filed" and ticket is not None:
            # One ticket per turn: the model speaks its confirmation in the
            # next round but may not call the tool again. (The tools param
            # must stay present — the conversation now contains tool blocks.)
            filed_fp = fp
            if fp:
                recent.add(fp)
            tool_choice = {"type": "none"}
        elif status == "already_filed":
            # Duplicate suppressed — let the model reassure, don't file again.
            # Surface the fp so the caller treats the booking as accounted for.
            filed_fp = fp
            tool_choice = {"type": "none"}
        elif tool_choice is not None and tool_choice.get("type") == "tool":
            # A forced call that was rejected/failed: release the force so the
            # next round asks for the missing detail instead of re-calling blind.
            tool_choice = None

    return {"answer": "".join(parts).strip(), "ticket": ticket, "filed_fp": filed_fp}
