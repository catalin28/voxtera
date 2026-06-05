"""Unit tests for Triage layer (Phase 3.4)."""

from __future__ import annotations

from typing import Any

from voxtera.call_center.triage import (
    MAX_CLARIFICATIONS,
    SLOT_GEOGRAPHY,
    SLOT_HOTEL_OR_RECOMMEND,
    SLOT_NON_NEGOTIABLE,
    Triage,
)


def _decomp(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hotel_mention": None,
        "city": None,
        "region": None,
        "district": None,
        "intent": "recommendation",
        "query_type": "broad",
        "source_required": ["hotel_kb"],
        "requirements": [],
        "dietary_religious": [],
        "accessibility_needs": [],
        "language": "en",
    }
    base.update(overrides)
    return base


# ----------------- Priority 1: geography -----------------


def test_scoped_hotel_specific_no_triage_needed() -> None:
    out = Triage().assess(
        _decomp(hotel_mention="Rixos Belek", query_type="scoped", intent="amenities")
    )
    assert out["ask"] is False
    assert out["reason"] == "sufficient_context"


def test_broad_recommendation_missing_geography_asks() -> None:
    out = Triage().assess(_decomp(query_type="broad", intent="recommendation"))
    assert out["ask"] is True
    assert out["slot"] == SLOT_GEOGRAPHY
    assert out["reason"] == "missing_geography"
    assert "destination" in out["question"].lower()


def test_web_query_missing_geography_asks() -> None:
    out = Triage().assess(_decomp(query_type="web", intent="event"))
    assert out["ask"] is True
    assert out["slot"] == SLOT_GEOGRAPHY


def test_session_carryover_satisfies_geography() -> None:
    out = Triage().assess(
        _decomp(query_type="broad", intent="recommendation"),
        session={"active_region": "antalya", "clarification_count": 0},
    )
    assert out["ask"] is False
    assert out["reason"] == "sufficient_context"


def test_region_in_decomposition_satisfies_geography() -> None:
    out = Triage().assess(_decomp(query_type="web", region="antalya", intent="event"))
    assert out["ask"] is False


# ----------------- Priority 2: ambiguous intent -----------------


def test_ambiguous_food_intent_asks_hotel_or_recommend() -> None:
    out = Triage().assess(_decomp(query_type="broad", intent="food", region="antalya"))
    assert out["ask"] is True
    assert out["slot"] == SLOT_HOTEL_OR_RECOMMEND


def test_clear_recommendation_intent_does_not_ask() -> None:
    out = Triage().assess(_decomp(query_type="broad", intent="recommendation", region="antalya"))
    assert out["ask"] is False


# ----------------- Priority 3: non-negotiable -----------------


def test_food_intent_with_no_dietary_asks_non_negotiable() -> None:
    # BROAD food recommendation (requirements present so hotel-or-recommend
    # doesn't fire, region present so geography is satisfied), no dietary -> ask.
    out = Triage().assess(
        _decomp(
            query_type="broad",
            intent="food",
            region="antalya",
            requirements=["seafood restaurant"],
        )
    )
    assert out["ask"] is True
    assert out["slot"] == SLOT_NON_NEGOTIABLE


def test_food_intent_with_dietary_does_not_ask() -> None:
    out = Triage().assess(
        _decomp(
            query_type="broad",
            intent="food",
            region="antalya",
            requirements=["restaurant"],
            dietary_religious=["halal"],
        )
    )
    assert out["ask"] is False


def test_scoped_food_query_does_not_ask_non_negotiable() -> None:
    # "do they have bars?" about a known hotel — a scoped factual question.
    # Triage must NOT interrupt with a dietary clarification; just answer.
    out = Triage().assess(
        _decomp(
            query_type="scoped",
            intent="food",
            hotel_mention=None,
            requirements=["bars", "restaurants"],
        ),
        session={"active_hotel_id": "crystal_tat_beach", "clarification_count": 0},
    )
    assert out["ask"] is False
    assert out["reason"] == "sufficient_context"


# ----------------- 2-turn clarification budget -----------------


def test_max_clarifications_reached_proceeds_without_ask() -> None:
    out = Triage().assess(
        _decomp(query_type="broad", intent="recommendation"),  # would normally ask geography
        session={"clarification_count": MAX_CLARIFICATIONS},
    )
    assert out["ask"] is False
    assert out["reason"] == "max_clarifications_reached"


def test_one_clarification_already_done_still_allowed_to_ask() -> None:
    out = Triage().assess(
        _decomp(query_type="broad", intent="recommendation"),
        session={"clarification_count": 1},
    )
    assert out["ask"] is True
    assert out["slot"] == SLOT_GEOGRAPHY


# ----------------- escalation never reaches triage by design,
# ----------------- but we don't blow up if it does -----------------


def test_escalation_short_circuits() -> None:
    out = Triage().assess(_decomp(query_type="escalate", intent="policy"))
    assert out["ask"] is False
    assert out["reason"] == "escalation_skips_triage"


# ----------------- localisation -----------------


def test_turkish_question_returned_when_decomposition_is_tr() -> None:
    out = Triage().assess(_decomp(query_type="broad", intent="recommendation", language="tr"))
    assert out["language"] == "tr"
    assert "Nereye" in out["question"]


def test_session_language_used_when_decomp_lang_missing() -> None:
    out = Triage().assess(
        _decomp(query_type="broad", intent="recommendation", language=""),
        session={"language": "tr", "clarification_count": 0},
    )
    assert out["language"] == "tr"


def test_unknown_language_falls_back_to_english() -> None:
    out = Triage().assess(_decomp(query_type="broad", intent="recommendation", language="xx"))
    assert out["language"] == "en"
    assert "destination" in out["question"].lower()


# ----------------- single-question-per-turn invariant -----------------


def test_geography_takes_precedence_over_ambiguous_intent() -> None:
    # Both gaps present — geography wins by priority.
    out = Triage().assess(_decomp(query_type="broad", intent="food"))
    assert out["slot"] == SLOT_GEOGRAPHY


def test_only_one_pending_slot_returned() -> None:
    out = Triage().assess(_decomp(query_type="broad", intent="food"))
    assert len(out["pending_slots"]) == 1
