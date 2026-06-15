"""Booking slot extraction (Phase 4 slot-drift fix).

A dedicated, silent LLM call that reads the property conversation and returns
the restaurant/spa booking details the GUEST has stated, as structured slots.
It runs in PARALLEL with the spoken render (see pipeline), so it adds no
wall-clock latency; the pipeline persists its output and feeds it back to the
next turn's render as a LOCKED recap (``intents.booking_recap``).

Why a separate call instead of the spoken model self-reporting via a tool: on
the live model the spoken render either skipped the tool (slots never filled) or
narrated the tool into speech ("I'll record the booking details…"), leaking the
machinery. A standalone extractor is reliable and never reaches the guest's ear.

It is LLM-driven (the model decides the slot VALUES from what the guest said);
Python only stores and merges them — no heuristic guessing.

Dependency-injected via ``extract_fn`` so tests run offline. Returns ``{}`` (and
never raises) when there is no booking or the call fails — booking simply falls
back to prompt-only behaviour.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from voxtera.call_center.clients import anthropic_client as _anthropic
from voxtera.call_center.hotel_time import hotel_time_note
from voxtera.call_center.intents import BOOKING_SLOT_KEYS
from voxtera.call_center.prompts import load_prompt
from voxtera.call_center.session import build_transcript

DEFAULT_MODEL = os.environ.get("DECOMPOSE_MODEL", "claude-haiku-4-5-20251001")

# Small budget: the output is a tiny JSON object.
_MAX_TOKENS = 256

ExtractFn = Callable[[str], Awaitable[dict[str, Any]]]


def _coerce(raw: dict[str, Any] | None, slot_keys: tuple[str, ...]) -> dict[str, str]:
    """Keep only known string slots with non-empty values."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k in slot_keys:
        v = raw.get(k)
        if isinstance(v, str | int) and str(v).strip():
            out[k] = str(v).strip()
    return out


def merge_slots(prior: dict[str, Any] | None, extracted: dict[str, str]) -> dict[str, str]:
    """Merge a fresh extraction onto prior slots (new non-empty values win)."""
    merged: dict[str, str] = {k: str(v).strip() for k, v in (prior or {}).items() if str(v).strip()}
    merged.update(extracted)
    return merged


def _build_user_msg(
    *, utterance: str, history: list[dict[str, Any]] | None, hotel_timezone: str | None
) -> str:
    transcript = build_transcript(history)
    anchor = hotel_time_note(hotel_timezone)
    parts = [anchor]
    if transcript:
        parts.append(f"Conversation so far:\n{transcript}")
    parts.append(f"Guest's current message: {utterance}")
    return "\n\n".join(parts)


def _build_anthropic_extract(model: str, prompt_name: str) -> ExtractFn:
    async def extract(user_msg: str) -> dict[str, Any]:
        client = _anthropic()
        msg = await client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": load_prompt(prompt_name),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        text = ""
        for block in getattr(msg, "content", []) or []:
            t = getattr(block, "text", None)
            if t:
                text = t
                break
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        return json.loads(text) if text else {}

    return extract


async def extract_booking_slots(
    *,
    utterance: str,
    history: list[dict[str, Any]] | None,
    prior_slots: dict[str, Any] | None,
    hotel_timezone: str | None = None,
    extract_fn: ExtractFn | None = None,
    model: str = DEFAULT_MODEL,
    slot_keys: tuple[str, ...] = BOOKING_SLOT_KEYS,
    prompt_name: str = "booking_slot_extractor",
    log_label: str = "booking-extract",
) -> dict[str, str]:
    """Return the booking slots stated so far, merged onto ``prior_slots``.

    Never raises: on any failure it returns ``prior_slots`` unchanged (cleaned),
    so a flaky extraction degrades to prompt-only behaviour rather than dropping
    the booking. ``slot_keys`` + ``prompt_name`` select the booking domain — the
    restaurant/spa default, or the travel hotel-stay set (see
    :func:`extract_stay_slots`).
    """
    fn = extract_fn or _build_anthropic_extract(model, prompt_name)
    user_msg = _build_user_msg(utterance=utterance, history=history, hotel_timezone=hotel_timezone)
    try:
        raw = await fn(user_msg)
    except Exception as e:  # noqa: BLE001 — booking degrades to prompt-only
        logger.warning("[{}] failed: {}", log_label, e)
        return merge_slots(prior_slots, {})
    extracted = _coerce(raw, slot_keys)
    merged = merge_slots(prior_slots, extracted)
    if extracted:
        logger.info("[{}] slots={}", log_label, merged)
    return merged


async def extract_stay_slots(
    *,
    utterance: str,
    history: list[dict[str, Any]] | None,
    prior_slots: dict[str, Any] | None,
    hotel_timezone: str | None = None,
    extract_fn: ExtractFn | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, str]:
    """Travel-path hotel-stay variant of :func:`extract_booking_slots`."""
    from voxtera.call_center.travel_booking import STAY_SLOT_KEYS

    return await extract_booking_slots(
        utterance=utterance,
        history=history,
        prior_slots=prior_slots,
        hotel_timezone=hotel_timezone,
        extract_fn=extract_fn,
        model=model,
        slot_keys=STAY_SLOT_KEYS,
        prompt_name="stay_slot_extractor",
        log_label="stay-extract",
    )
