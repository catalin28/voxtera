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

_EXTRACT_PROMPT = """You turn ONE hotel-guest request into a staff ticket.

Guest's request (verbatim): {utterance}

Recent conversation (may add context like a room number):
{transcript}

Return ONLY a JSON object, no prose:
{{
  "category": one of [{categories}],
  "summary": "one-sentence summary of the request, written in {official_language}",
  "room_number": "the room number if mentioned anywhere, else 'unknown'",
  "language_detected": "the guest's language as a short label, e.g. 'English'"
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

    async def file_from_turn(
        self,
        *,
        hotel_id: str,
        utterance: str,
        transcript: str,
        language: str | None,
    ) -> dict[str, Any] | None:
        """Create + deliver a ticket for one actionable guest turn.

        Returns ``{"session_id", "category"}`` on success, None on any
        failure (caller falls back to the plain escalation answer).
        """
        runtime = self._runtime(hotel_id)
        if runtime is None:
            return None

        from voxtera.actions.ticket import Ticket

        fields = await self._extract_fields(
            utterance=utterance,
            transcript=transcript,
            language=language,
            hotel_config=runtime.hotel_config,
        )
        ticket = Ticket(
            category=fields["category"],
            summary=fields["summary"],
            room_number=fields["room_number"],
            original_quote=utterance[:1000],
            language_detected=fields["language_detected"],
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
        """Fill the ticket fields the old LLM tool call used to produce.

        One small Anthropic call; deterministic fallback (category OTHER or
        the hotel's first allowed category, raw utterance as summary) when
        the extraction fails — a clumsy ticket beats a silent drop.
        """
        from voxtera.actions.ticket import Category

        allowed = tuple(hotel_config.allowed_categories) or (Category.OTHER,)
        fallback = {
            "category": Category.OTHER if Category.OTHER in allowed else allowed[0],
            "summary": utterance[:500],
            "room_number": "unknown",
            "language_detected": language or "unknown",
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
            category = Category(str(data.get("category", "")).strip())
            if category not in allowed:
                category = fallback["category"]
            return {
                "category": category,
                "summary": str(data.get("summary") or "").strip()[:500] or fallback["summary"],
                "room_number": str(data.get("room_number") or "unknown").strip()[:64] or "unknown",
                "language_detected": str(data.get("language_detected") or "").strip()[:64]
                or fallback["language_detected"],
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("[property-actions] field extraction failed ({}) — using fallback", e)
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
