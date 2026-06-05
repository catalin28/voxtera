"""Triage Layer — single-gap clarification gate (Phase 3).

Runs AFTER ``EscalationClassifier`` says "no escalation" and the
``QueryDecomposer`` has produced its structured payload. Triage's job
(per Voxtera_RAG_Architecture_v0.3 §3) is to identify the **single most
critical missing context** and ask **one** focused question — at most
**two** clarification turns across the entire call.

Priority hierarchy (Architecture §3.1):

  1. Geography (country / region / city / hotel)   — blocking
  2. Hotel vs recommendation intent                — blocking
  3. Non-negotiable requirement (dietary / access) — sometimes
  4. Traveller type                                — non-blocking
  5. Budget                                        — non-blocking
  6. Vibe / atmosphere                             — non-blocking

Decision contract (returned by ``Triage.assess``):

    {
      "ask":            bool,            # if True, send the question to the caller
      "question":       str | None,      # localised clarification prompt
      "slot":           str | None,      # which slot we're filling
                                         # ("geography" | "hotel_or_recommend" |
                                         #  "non_negotiable" | None)
      "reason":         str,             # short audit string for logs/UI
      "language":       str,             # ISO-639-1 of the question
      "pending_slots":  list[str],       # mirror of slot for downstream session.append_turn
    }

If the session has already been clarified twice, triage forces
``ask=False`` with ``reason="max_clarifications_reached"`` so the
caller is never trapped in a Q&A loop.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from voxtera.call_center.prompts import load_localised_prompts

# Max clarification turns per call (architecture §3 design principle).
MAX_CLARIFICATIONS = 2

# Slot names (stable strings used by session.pending_slots).
SLOT_GEOGRAPHY = "geography"
SLOT_HOTEL_OR_RECOMMEND = "hotel_or_recommend"
SLOT_NON_NEGOTIABLE = "non_negotiable"

# Query types where geography is non-negotiable (web queries fail
# without a location; broad/comparison/destination need region or city).
GEOGRAPHY_REQUIRED_QUERY_TYPES = {
    "broad",
    "compound",
    "comparison",
    "destination",
    "web",
    "hybrid",
}

# Localised clarification prompts. Loaded at import time from
# src/voxtera/call_center/prompts/triage_questions.md so non-engineers
# (and the future admin UI) can edit wording without touching code.
# Schema: {locale: {slot: question_text}}.
_PROMPTS: dict[str, dict[str, str]] = load_localised_prompts("triage_questions")

_DEFAULT_LANG = "en"


def _pick_language(decomposition: dict[str, Any], session: dict[str, Any] | None) -> str:
    """Prefer decomposition.language, then session.language, then English."""
    lang = (decomposition.get("language") or "").lower().strip()
    if lang and lang in _PROMPTS:
        return lang
    if session:
        slang = (session.get("language") or "").lower().strip()
        if slang and slang in _PROMPTS:
            return slang
    return _DEFAULT_LANG


def _has_geography(decomposition: dict[str, Any], session: dict[str, Any] | None) -> bool:
    """True if the decomposition or carry-over session has any geographic anchor."""
    for k in ("hotel_mention", "city", "region", "district"):
        v = decomposition.get(k)
        if isinstance(v, str) and v.strip():
            return True
    if session:
        for k in ("active_hotel_id", "active_region"):
            v = session.get(k)
            if isinstance(v, str) and v.strip():
                return True
    return False


def _needs_hotel_vs_recommend(decomposition: dict[str, Any]) -> bool:
    """True when intent is ambiguous between specific-hotel and recommendation.

    Only ask when the caller said something genuinely under-specified —
    broad query, no hotel named, no requirements / vibe / budget / traveller
    signal at all. If the caller listed any criterion ("with a spa",
    "near the beach", "under $200") they obviously want a recommendation
    and we should not interrupt them.
    """
    if decomposition.get("hotel_mention"):
        return False
    if decomposition.get("query_type") != "broad":
        return False
    if decomposition.get("intent") not in {"amenities", "food", "policy", "atmosphere"}:
        return False
    # Strong recommendation signals → trust the caller, don't ask.
    if decomposition.get("requirements"):
        return False
    if decomposition.get("vibe_preferences"):
        return False
    if decomposition.get("budget_tier") or decomposition.get("budget_signal"):
        return False
    if decomposition.get("traveller_type"):
        return False
    return True


def _needs_non_negotiable(decomposition: dict[str, Any]) -> bool:
    """True when the intent strongly signals a dietary/accessibility
    requirement but none was extracted. Asking up front prevents
    delivering useless results (e.g. a "halal-required" caller getting
    a non-halal hotel as the top hit).
    """
    # A scoped query is about a specific, already-known hotel — the caller is
    # asking a factual question ("do they have bars?"), not requesting a
    # recommendation. Never interrupt it with a dietary/accessibility prompt;
    # just retrieve and answer.
    if decomposition.get("query_type") == "scoped":
        return False

    intent = decomposition.get("intent")
    if intent == "food" and not decomposition.get("dietary_religious"):
        return True
    if intent == "policy" and not (
        decomposition.get("dietary_religious") or decomposition.get("accessibility_needs")
    ):
        # Only ask for policy queries when the requirements list is empty too —
        # otherwise the caller is clearly asking about something specific.
        return not decomposition.get("requirements")
    return False


def _no_ask(reason: str, language: str) -> dict[str, Any]:
    return {
        "ask": False,
        "question": None,
        "slot": None,
        "reason": reason,
        "language": language,
        "pending_slots": [],
    }


def _ask(slot: str, language: str, reason: str) -> dict[str, Any]:
    question = _PROMPTS.get(language, _PROMPTS[_DEFAULT_LANG]).get(slot)
    if not question:
        question = _PROMPTS[_DEFAULT_LANG][slot]
    return {
        "ask": True,
        "question": question,
        "slot": slot,
        "reason": reason,
        "language": language,
        "pending_slots": [slot],
    }


class Triage:
    """Single-gap clarification gate. Pure-Python, no LLM call."""

    def __init__(
        self,
        *,
        max_clarifications: int = MAX_CLARIFICATIONS,
    ) -> None:
        self._max_clarifications = max_clarifications

    def assess(
        self,
        decomposition: dict[str, Any],
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return whether to ask a clarifying question, and which one."""
        language = _pick_language(decomposition, session)

        # Hard stop on the 2-turn clarification budget.
        clarification_count = 0
        if session:
            try:
                clarification_count = int(session.get("clarification_count") or 0)
            except (TypeError, ValueError):
                clarification_count = 0
        if clarification_count >= self._max_clarifications:
            logger.debug(
                "Triage: max clarifications reached ({}); proceeding without ask",
                clarification_count,
            )
            return _no_ask("max_clarifications_reached", language)

        # Escalation should never reach triage, but be safe.
        if decomposition.get("query_type") == "escalate":
            return _no_ask("escalation_skips_triage", language)

        query_type = decomposition.get("query_type") or "broad"

        # --- Priority 1: Geography ---
        if query_type in GEOGRAPHY_REQUIRED_QUERY_TYPES:
            if not _has_geography(decomposition, session):
                return _ask(SLOT_GEOGRAPHY, language, "missing_geography")

        # --- Priority 2: Hotel vs recommendation intent ---
        if _needs_hotel_vs_recommend(decomposition):
            return _ask(SLOT_HOTEL_OR_RECOMMEND, language, "ambiguous_intent")

        # --- Priority 3: Non-negotiable requirement (only if strongly signalled) ---
        if _needs_non_negotiable(decomposition):
            return _ask(SLOT_NON_NEGOTIABLE, language, "missing_non_negotiable")

        # Priorities 4-6 are progressive / non-blocking — never asked here.
        return _no_ask("sufficient_context", language)
