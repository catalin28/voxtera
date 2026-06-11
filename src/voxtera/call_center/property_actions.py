"""Property actions — Telegram ticket creation for the concierge's hotel mode.

Port of the legacy hotel brain's ``create_ticket`` capability (P1.4): the old
``BOT_BRAIN=hotel`` pipeline registered ``create_ticket`` as an LLM tool, so
"can you book me a massage?" produced a ticket in the hotel's Telegram channel
with staff buttons. The one-brain switch lost that — actionable requests hit
the escalation classifier and promised "a colleague" who was never notified.

This module restores the capability for the property fast path:

- the concierge's escalation verdict (already computed concurrently on every
  hotel-mode turn) is the trigger — no extra latency on normal turns;
- ONE small LLM call fills the ticket fields the old tool call used to
  produce (category from the hotel's allowed list, summary translated to the
  hotel's official language, room number if mentioned), with a deterministic
  fallback so a flaky extraction still files SOMETHING;
- delivery reuses the existing self-contained actions runtime
  (``voxtera.actions``): InteractiveTelegramSink + state store + staff
  buttons, exactly as the old demo;
- the Telegram button listener is started once per process (background task)
  so staff taps keep working — disable with ``CONCIERGE_TELEGRAM_LISTENER=false``
  if another process (e.g. an old-style Daily hotel bot with
  ``ACTIONS_ENABLED=true``) already long-polls the same bot token.

Everything is best-effort: any failure here degrades to the plain escalation
hand-off line, never breaks a turn.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from loguru import logger

_EXTRACT_PROMPT = """You prepare a hotel-staff ticket from ONE guest request.

Guest's request (verbatim): {utterance}

Recent conversation (may contain details the guest already gave — room
number, time, party size; never re-ask for something present here):
{transcript}

A ticket is READY only when staff could actually act on it:
- a restaurant/spa/activity reservation needs at least a date or time and a
  party size (room number helps but is optional);
- an in-room issue (maintenance, housekeeping, room service) needs the room
  number;
- a complaint or a lost item is ready as long as the problem is clear.

Return ONLY a JSON object, no prose:
{{
  "category": one of [{categories}],
  "summary": "one-sentence summary of the request, written in {official_language}",
  "room_number": "the room number if mentioned anywhere, else 'unknown'",
  "language_detected": "the guest's language as a short label, e.g. 'English'",
  "ready": true or false,
  "question": "if not ready: ONE short spoken question in the GUEST's language \
asking for ALL missing details at once, else null",
  "confirm": "if ready: ONE short spoken confirmation in the GUEST's language — \
restate the request in a few words and ask whether to send it to the team, else null"
}}"""


def _listener_enabled() -> bool:
    return os.environ.get("CONCIERGE_TELEGRAM_LISTENER", "true").strip().lower() not in (
        "0",
        "false",
        "no",
    )


class PropertyTicketer:
    """Files guest-request tickets to the hotel's Telegram channel.

    One instance lives on the shared concierge deps; runtimes are built
    lazily per hotel and cached (the Telegram sink and state store are
    long-lived by design).
    """

    def __init__(self) -> None:
        self._runtimes: dict[str, Any] = {}  # hotel_id -> ActionRuntime | None
        self._listener_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # Runtime plumbing                                                    #
    # ------------------------------------------------------------------ #

    def _runtime(self, hotel_id: str):
        """Build (once) and return the hotel's ActionRuntime, or None.

        None when the feature can't run: no TELEGRAM_BOT_TOKEN, missing or
        invalid hotel config. Cached either way so we don't re-attempt on
        every escalation.
        """
        if hotel_id in self._runtimes:
            return self._runtimes[hotel_id]
        runtime = None
        try:
            from voxtera.actions import build_action_runtime

            runtime = build_action_runtime(hotel_id)
        except Exception as e:  # noqa: BLE001 — feature off, never break a turn
            logger.warning(
                "[property-actions] ticket runtime unavailable for hotel={!r}: {}", hotel_id, e
            )
        self._runtimes[hotel_id] = runtime
        if runtime is not None and self._listener_task is None and _listener_enabled():
            # Staff button taps (acknowledge / assign / resolve) need the
            # long-poll listener. One per process is both sufficient and
            # required — two pollers on one bot token make Telegram 409.
            self._listener_task = asyncio.create_task(
                runtime.listener.run(), name="telegram-button-listener"
            )
            logger.info("[property-actions] Telegram button listener started")
        return runtime

    # ------------------------------------------------------------------ #
    # Ticket creation                                                     #
    # ------------------------------------------------------------------ #

    async def assess(
        self,
        *,
        hotel_id: str,
        utterance: str,
        transcript: str,
        language: str | None,
    ) -> dict[str, Any] | None:
        """Assess one actionable turn: ticket fields + what's still missing.

        Mirrors the legacy tool prompt's flow: collect the details staff need
        (room number for in-room issues, time + party size for reservations),
        then ALWAYS confirm before filing.

        Returns None when the ticket layer is unavailable, else::

            {"ready": bool,
             "question": str | None,   # not ready: ask for ALL missing info
             "confirm":  str | None,   # ready: "shall I send it?" line
             "fields": {"category", "summary", "room_number",
                        "language_detected"}}   # JSON-safe (Redis session)
        """
        runtime = self._runtime(hotel_id)
        if runtime is None:
            return None
        return await self._extract_fields(
            utterance=utterance,
            transcript=transcript,
            language=language,
            hotel_config=runtime.hotel_config,
        )

    async def file(
        self,
        *,
        hotel_id: str,
        fields: dict[str, Any],
        original_quote: str,
    ) -> dict[str, Any] | None:
        """Deliver a confirmed ticket. None on any failure."""
        runtime = self._runtime(hotel_id)
        if runtime is None:
            return None

        from voxtera.actions.ticket import Category, Ticket

        try:
            category = Category(fields.get("category") or Category.OTHER.value)
        except ValueError:
            category = Category.OTHER
        ticket = Ticket(
            category=category,
            summary=(fields.get("summary") or original_quote)[:500],
            room_number=(fields.get("room_number") or "unknown")[:64],
            original_quote=original_quote[:1000],
            language_detected=(fields.get("language_detected") or "unknown")[:64],
        )
        try:
            ok = await runtime.sink.send(ticket)
        except Exception as e:  # noqa: BLE001 — sink contract says no-raise; belt+braces
            logger.exception("[property-actions] sink raised session={}: {}", ticket.session_id, e)
            ok = False
        if not ok:
            logger.error("[property-actions] ticket delivery failed session={}", ticket.session_id)
            return None
        logger.info(
            "[property-actions] ticket filed session={} category={} room={}",
            ticket.session_id,
            ticket.category.value,
            ticket.room_number,
        )
        return {"session_id": ticket.session_id, "category": ticket.category.value}

    async def _extract_fields(
        self,
        *,
        utterance: str,
        transcript: str,
        language: str | None,
        hotel_config: Any,
    ) -> dict[str, Any]:
        """One small Anthropic call fills fields + readiness + the next line.

        Deterministic fallback when the extraction fails: treat the request
        as ready with the raw utterance as summary and a generic confirm
        question — a clumsy ticket flow beats a silent drop.
        """
        from voxtera.actions.ticket import Category

        allowed = tuple(hotel_config.allowed_categories) or (Category.OTHER,)
        fallback_category = Category.OTHER if Category.OTHER in allowed else allowed[0]
        fallback = {
            "ready": True,
            "question": None,
            "confirm": (
                "I'll pass this request to our team — shall I send it now?"
            ),
            "fields": {
                "category": fallback_category.value,
                "summary": utterance[:500],
                "room_number": "unknown",
                "language_detected": language or "unknown",
            },
        }
        try:
            from voxtera.call_center.clients import anthropic_client
            from voxtera.call_center.deps import llm_model

            prompt = _EXTRACT_PROMPT.format(
                utterance=utterance[:1000],
                transcript=(transcript or "—")[-2000:],
                categories=", ".join(f'"{c.value}"' for c in allowed),
                official_language=hotel_config.official_language,
            )
            resp = await anthropic_client().messages.create(
                model=llm_model(),
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start : end + 1])
            try:
                category = Category(str(data.get("category", "")).strip())
            except ValueError:
                category = fallback_category
            if category not in allowed:
                category = fallback_category
            ready = bool(data.get("ready"))
            question = str(data.get("question") or "").strip() or None
            confirm = str(data.get("confirm") or "").strip() or None
            if not ready and question is None:
                # Model said "not ready" without a question — degrade to ready
                # with the generic confirm rather than dead-ending the guest.
                ready, confirm = True, fallback["confirm"]
            if ready and confirm is None:
                confirm = fallback["confirm"]
            return {
                "ready": ready,
                "question": None if ready else question,
                "confirm": confirm if ready else None,
                "fields": {
                    "category": category.value,
                    "summary": str(data.get("summary") or "").strip()[:500]
                    or fallback["fields"]["summary"],
                    "room_number": str(data.get("room_number") or "unknown").strip()[:64]
                    or "unknown",
                    "language_detected": str(data.get("language_detected") or "").strip()[:64]
                    or fallback["fields"]["language_detected"],
                },
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("[property-actions] ticket assessment failed ({}) — using fallback", e)
            return fallback


def ticket_filed_answer(lang: str, category: str) -> str:
    """Localized spoken confirmation after a ticket reached the staff channel."""
    return {
        "tr": (
            "Elbette — talebinizi hemen ekibimize ilettim. Kısa süre içinde sizinle ilgilenecekler."
        ),
        "fr": (
            "Bien sûr — j'ai transmis votre demande à notre équipe. "
            "Ils s'en occupent et reviennent vers vous très vite."
        ),
        "de": (
            "Selbstverständlich — ich habe Ihr Anliegen an unser Team weitergegeben. "
            "Es kümmert sich gleich darum."
        ),
        "es": (
            "Por supuesto — he pasado su solicitud a nuestro equipo. Se ocuparán de ello enseguida."
        ),
    }.get(
        lang,
        "Of course — I've passed your request to our team. "
        "They're on it and will confirm with you shortly.",
    )
