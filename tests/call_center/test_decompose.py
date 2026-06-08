"""Unit tests for QueryDecomposer (Phase 3 — full schema + 27-type taxonomy).

The Claude call is stubbed via ``decompose_fn`` injection so tests run
fully offline. Tests cover:
  - happy paths for each query_type family (scoped / broad / destination /
    web / hybrid / escalate)
  - source_required derivation when the LLM omits it
  - defensive coercion (bad enums, oversized lists, missing fields)
  - context carry-over (active_region / active_hotel_id are passed through
    to the LLM call)
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.decompose import (
    MAX_REQUIREMENTS,
    QueryDecomposer,
    _default_sources_for,
)


def _scripted(payload: dict[str, Any]) -> Any:
    state = {"calls": []}

    async def decompose(utterance: str, context: dict[str, Any]) -> dict[str, Any]:
        state["calls"].append({"utterance": utterance, "context": dict(context)})
        return payload

    decompose.state = state  # type: ignore[attr-defined]
    return decompose


# ----------------- happy paths per query_type family -----------------


@pytest.mark.asyncio
async def test_scoped_hotel_specific_fact() -> None:
    fn = _scripted(
        {
            "hotel_mention": "Rixos Belek",
            "intent": "amenities",
            "query_type": "scoped",
            "query_type_id": 1,
            "source_required": ["hotel_kb"],
            "requirements": ["hamam"],
            "requirements_logic": "AND",
            "on_site_required": [True],
            "language": "tr",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("Rixos Belek'te hamam var mı?")
    assert out["query_type"] == "scoped"
    assert out["query_type_id"] == 1
    assert out["source_required"] == ["hotel_kb"]
    assert out["requirements"] == ["hamam"]
    assert out["on_site_required"] == [True]
    assert out["language"] == "tr"


@pytest.mark.asyncio
async def test_slug_shaped_hotel_mention_is_dropped() -> None:
    # The model echoed a carry-over hotel_id slug into hotel_mention on a broad
    # query. It must be dropped — a real mention is a typed name, never a slug.
    fn = _scripted(
        {
            "hotel_mention": "akra_kemer",
            "city": "Kemer",
            "region": "Antalya",
            "query_type": "scoped",
            "requirements": ["spa", "relaxation"],
            "language": "en",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("I want a spa hotel to relax")
    assert out["hotel_mention"] is None
    # A genuine multi-word name still survives.
    fn2 = _scripted(
        {
            "hotel_mention": "Akra Kemer",
            "query_type": "scoped",
            "requirements": ["spa"],
            "language": "en",
        }
    )
    out2 = await QueryDecomposer(decompose_fn=fn2).decompose("tell me about Akra Kemer")
    assert out2["hotel_mention"] == "Akra Kemer"


def test_ctx_block_active_hotel_adds_followup_hint_without_leaking_id() -> None:
    from voxtera.call_center.decompose import _build_ctx_block

    block = _build_ctx_block(
        {
            "active_hotel_id": "crystal_tat_beach",
            "active_region": "antalya",
        }
    )
    # Instruction is present so follow-ups classify as scoped...
    assert "scoped" in block.lower()
    assert "follow-up" in block.lower()
    # ...but the hotel id must NOT appear in the prompt (that caused the echo bug).
    assert "crystal_tat_beach" not in block


@pytest.mark.asyncio
async def test_generic_hotel_reference_is_dropped() -> None:
    # "is the hotel on the beach?" — "the hotel" is anaphora, not a named hotel.
    # It must be dropped so the router scopes to the session's active hotel
    # instead of trying (and failing) to resolve "the hotel".
    for ref in ("the hotel", "this hotel", "it", "the resort"):
        fn = _scripted(
            {
                "hotel_mention": ref,
                "query_type": "scoped",
                "requirements": ["beach location"],
                "language": "en",
            }
        )
        out = await QueryDecomposer(decompose_fn=fn).decompose(f"is {ref} on the beach?")
        assert out["hotel_mention"] is None, f"{ref!r} should be dropped"


@pytest.mark.asyncio
async def test_broad_family_recommendation() -> None:
    fn = _scripted(
        {
            "region": "antalya",
            "intent": "recommendation",
            "query_type": "broad",
            "query_type_id": 2,
            "source_required": ["hotel_kb"],
            "requirements": ["water sports", "kids club"],
            "requirements_logic": "AND",
            "on_site_required": [True, True],
            "traveller_type": "family",
            "children_ages": [6, 9],
            "language": "tr",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("Antalya'da çocuklarımla su sporları yapabileceğim bir yer.")
    assert out["query_type"] == "broad"
    assert out["region"] == "antalya"
    assert out["traveller_type"] == "family"
    assert out["children_ages"] == [6, 9]
    assert out["requirements"] == ["water sports", "kids club"]


@pytest.mark.asyncio
async def test_destination_general_info() -> None:
    fn = _scripted(
        {
            "city": "Cappadocia",
            "intent": "destination_info",
            "query_type": "destination",
            "query_type_id": 11,
            "source_required": ["destination_kb"],
            "language": "en",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("What is Cappadocia known for?")
    assert out["query_type"] == "destination"
    assert out["query_type_id"] == 11
    assert out["source_required"] == ["destination_kb"]


@pytest.mark.asyncio
async def test_web_event_query() -> None:
    fn = _scripted(
        {
            "city": "Playa del Carmen",
            "region": "riviera_maya_mexico",
            "intent": "event",
            "query_type": "web",
            "query_type_id": 16,
            "source_required": ["web"],
            "time_reference": "December",
            "language": "en",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("Festivals near Playa del Carmen in December?")
    assert out["query_type"] == "web"
    assert out["source_required"] == ["web"]
    assert out["time_reference"] == "December"


@pytest.mark.asyncio
async def test_hybrid_hotel_plus_web() -> None:
    fn = _scripted(
        {
            "hotel_mention": "Rixos Belek",
            "region": "antalya",
            "district": "belek",
            "intent": "local_operator",
            "query_type": "hybrid",
            "query_type_id": 20,
            "source_required": ["hotel_kb", "web"],
            "requirements": ["scuba diving"],
            "on_site_required": [False],
            "language": "tr",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("Rixos Belek yakınında dalış okulu var mı?")
    assert out["query_type"] == "hybrid"
    assert out["source_required"] == ["hotel_kb", "web"]
    assert out["on_site_required"] == [False]


@pytest.mark.asyncio
async def test_escalate_booking_intent() -> None:
    fn = _scripted(
        {
            "intent": "policy",
            "query_type": "escalate",
            "query_type_id": 24,
            "source_required": [],
            "urgency": "urgent",
            "language": "en",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("I want to book for next weekend")
    assert out["query_type"] == "escalate"
    assert out["source_required"] == []
    assert out["urgency"] == "urgent"


# ----------------- source_required derivation -----------------


def test_default_sources_table() -> None:
    assert _default_sources_for("scoped") == ["hotel_kb"]
    assert _default_sources_for("broad") == ["hotel_kb"]
    assert _default_sources_for("compound") == ["hotel_kb"]
    assert _default_sources_for("comparison") == ["hotel_kb"]
    assert _default_sources_for("destination") == ["destination_kb"]
    assert _default_sources_for("web") == ["web"]
    assert _default_sources_for("hybrid") == ["hotel_kb", "web"]
    assert _default_sources_for("escalate") == []


@pytest.mark.asyncio
async def test_source_required_derived_when_llm_omits() -> None:
    fn = _scripted({"query_type": "hybrid", "requirements": ["spa"]})
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("...")
    assert out["source_required"] == ["hotel_kb", "web"]


@pytest.mark.asyncio
async def test_source_required_dedupe_and_invalid_filter() -> None:
    fn = _scripted(
        {
            "query_type": "hybrid",
            "source_required": ["hotel_kb", "hotel_kb", "PIZZA", "web"],
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("...")
    assert out["source_required"] == ["hotel_kb", "web"]


# ----------------- defensive coercion -----------------


@pytest.mark.asyncio
async def test_empty_utterance_returns_empty_payload() -> None:
    fn = _scripted({"query_type": "scoped", "requirements": ["spa"]})
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("   ")
    assert out["requirements"] == []
    # decompose_fn must NOT have been called for an empty utterance.
    assert fn.state["calls"] == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_llm_exception_returns_safe_default() -> None:
    async def boom(_u: str, _c: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("network down")

    d = QueryDecomposer(decompose_fn=boom)
    out = await d.decompose("normal utterance")
    assert out["requirements"] == []
    assert out["query_type"] == "broad"
    assert out["source_required"] == ["hotel_kb"]


@pytest.mark.asyncio
async def test_bad_enums_coerce_to_defaults() -> None:
    fn = _scripted(
        {
            "intent": "INVALID_INTENT",
            "query_type": "INVALID_TYPE",
            "traveller_type": "ALIEN",
            "budget_tier": "ULTRA_LUXE",
            "urgency": "PANIC",
            "requirements_logic": "XOR",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("...")
    assert out["intent"] == "recommendation"
    assert out["query_type"] == "broad"
    assert out["traveller_type"] is None
    assert out["budget_tier"] is None
    assert out["urgency"] == "normal"
    assert out["requirements_logic"] == "AND"


@pytest.mark.asyncio
async def test_requirements_capped_and_on_site_padded() -> None:
    too_many = [f"req{i}" for i in range(20)]
    fn = _scripted(
        {
            "query_type": "compound",
            "requirements": too_many,
            "on_site_required": [True, False],  # too short
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("...")
    assert len(out["requirements"]) == MAX_REQUIREMENTS
    assert len(out["on_site_required"]) == MAX_REQUIREMENTS
    # First two preserved as given, rest default to True.
    assert out["on_site_required"][:2] == [True, False]
    assert all(v is True for v in out["on_site_required"][2:])


@pytest.mark.asyncio
async def test_children_ages_filter_out_garbage() -> None:
    fn = _scripted(
        {
            "query_type": "broad",
            "children_ages": [6, "nine", -1, 9, 999],
            "traveller_type": "family",
        }
    )
    d = QueryDecomposer(decompose_fn=fn)
    out = await d.decompose("...")
    assert out["children_ages"] == [6, 9]


# ----------------- context carry-over -----------------


@pytest.mark.asyncio
async def test_context_passed_through_to_decompose_fn() -> None:
    fn = _scripted({"query_type": "scoped", "region": "antalya"})
    d = QueryDecomposer(decompose_fn=fn)
    ctx = {"active_region": "antalya", "active_hotel_id": "rixos_belek", "language": "tr"}
    await d.decompose("kids club var mı?", ctx)
    assert fn.state["calls"][0]["context"] == ctx  # type: ignore[attr-defined]
