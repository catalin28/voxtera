"""Unit tests for SourceRouter (Phase 3.5)."""

from __future__ import annotations

from typing import Any

from voxtera.call_center.router import (
    PATH_BROAD,
    PATH_DESTINATION,
    PATH_ESCALATE,
    PATH_HOTEL_RESOLVE,
    PATH_HYBRID,
    PATH_NEEDS_GEOGRAPHY,
    PATH_SCOPED,
    PATH_WEB,
    SourceRouter,
)


def _decomp(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hotel_mention": None,
        "city": None,
        "region": None,
        "district": None,
        "intent": "recommendation",
        "query_type": "broad",
        "urgency": "normal",
        "language": "en",
    }
    base.update(overrides)
    return base


# ----------------- 1. escalation -----------------


def test_escalate_query_type_routes_to_escalate() -> None:
    out = SourceRouter().route(_decomp(query_type="escalate", intent="policy"))
    assert out["path"] == PATH_ESCALATE
    assert out["sources"] == []


def test_immediate_escalation_urgency_routes_to_escalate() -> None:
    out = SourceRouter().route(_decomp(query_type="broad", urgency="immediate_escalation"))
    assert out["path"] == PATH_ESCALATE


# ----------------- 2. time-sensitive → web -----------------


def test_web_query_with_geography_routes_to_web() -> None:
    out = SourceRouter().route(_decomp(query_type="web", intent="event", city="Bodrum"))
    assert out["path"] == PATH_WEB
    assert out["sources"] == ["web"]


def test_web_query_without_geography_needs_destination() -> None:
    out = SourceRouter().route(_decomp(query_type="web", intent="event"))
    assert out["path"] == PATH_NEEDS_GEOGRAPHY
    assert out["needs"] == "geography"


def test_weather_intent_routes_to_web() -> None:
    out = SourceRouter().route(_decomp(intent="weather", region="antalya"))
    assert out["path"] == PATH_WEB


def test_scoped_query_not_hijacked_to_web_by_practical_info_intent() -> None:
    # "is the hotel on the beach?" — explicitly scoped (hotel KB fact) but the
    # model tagged intent=practical_info. The time-sensitive web override must
    # NOT fire for a scoped query; it should reach the hotel path.
    out = SourceRouter().route(
        _decomp(query_type="scoped", intent="practical_info", region="antalya"),
        session={"active_hotel_id": "crystal_tat_beach"},
    )
    assert out["path"] == PATH_SCOPED
    assert out["sources"] == ["hotel_kb"]


# ----------------- 3. local operator → hybrid / web -----------------


def test_hybrid_with_resolved_hotel_routes_to_hybrid() -> None:
    out = SourceRouter().route(
        _decomp(query_type="hybrid", intent="local_operator"),
        session={"active_hotel_id": "rixos_belek"},
    )
    assert out["path"] == PATH_HYBRID
    assert out["sources"] == ["hotel_kb", "web"]


def test_hybrid_with_hotel_mention_routes_to_hybrid() -> None:
    out = SourceRouter().route(
        _decomp(query_type="hybrid", intent="local_operator", hotel_mention="Rixos Belek")
    )
    assert out["path"] == PATH_HYBRID


def test_local_operator_no_hotel_with_geography_routes_to_web() -> None:
    out = SourceRouter().route(_decomp(intent="local_operator", region="antalya"))
    assert out["path"] == PATH_WEB


def test_local_operator_no_hotel_no_geography_needs_geography() -> None:
    out = SourceRouter().route(_decomp(intent="local_operator"))
    assert out["path"] == PATH_NEEDS_GEOGRAPHY


# ----------------- 4. specific hotel -----------------


def test_new_mention_reresolves_even_with_session_hotel() -> None:
    # A freshly named hotel re-resolves even when a hotel is already active in
    # the session — the named hotel may differ from the session's, so trusting
    # session.active_hotel_id here would answer about the wrong hotel.
    out = SourceRouter().route(
        _decomp(query_type="scoped", intent="amenities", hotel_mention="Crystal Tat Beach"),
        session={"active_hotel_id": "akra_kemer"},
    )
    assert out["path"] == PATH_HOTEL_RESOLVE
    assert out["needs"] == "hotel_resolve"


def test_scoped_with_unresolved_hotel_routes_to_resolver() -> None:
    out = SourceRouter().route(
        _decomp(query_type="scoped", intent="amenities", hotel_mention="Rixosta")
    )
    assert out["path"] == PATH_HOTEL_RESOLVE
    assert out["needs"] == "hotel_resolve"


def test_hotel_mention_without_scoped_query_type_still_resolves_hotel() -> None:
    # broad query_type but with hotel_mention — caller named a hotel, so we
    # need to resolve it before deciding final retrieval.
    out = SourceRouter().route(
        _decomp(query_type="broad", intent="amenities", hotel_mention="Rixos", region="antalya")
    )
    assert out["path"] == PATH_HOTEL_RESOLVE


# ----------------- 5. broad / comparison / compound -----------------


def test_broad_with_geography_routes_to_broad() -> None:
    out = SourceRouter().route(_decomp(query_type="broad", region="antalya"))
    assert out["path"] == PATH_BROAD
    assert out["sources"] == ["hotel_kb"]


def test_comparison_with_geography_routes_to_broad() -> None:
    out = SourceRouter().route(_decomp(query_type="comparison", region="antalya"))
    assert out["path"] == PATH_BROAD
    assert out["reason"] == "broad_comparison"


def test_compound_with_geography_routes_to_broad() -> None:
    out = SourceRouter().route(_decomp(query_type="compound", city="Istanbul"))
    assert out["path"] == PATH_BROAD


def test_broad_without_geography_needs_geography() -> None:
    out = SourceRouter().route(_decomp(query_type="broad"))
    assert out["path"] == PATH_NEEDS_GEOGRAPHY


# ----------------- 6. destination -----------------


def test_destination_query_type_routes_to_destination_kb() -> None:
    out = SourceRouter().route(
        _decomp(query_type="destination", intent="destination_info", city="Cappadocia")
    )
    assert out["path"] == PATH_DESTINATION
    assert out["sources"] == ["destination_kb"]


def test_visa_intent_routes_to_destination_kb() -> None:
    out = SourceRouter().route(_decomp(intent="visa", query_type="destination"))
    assert out["path"] == PATH_DESTINATION


# ----------------- session carry-over -----------------


def test_session_active_region_satisfies_geography() -> None:
    out = SourceRouter().route(
        _decomp(query_type="broad"),
        session={"active_region": "antalya"},
    )
    assert out["path"] == PATH_BROAD


def test_session_active_hotel_id_satisfies_hotel_resolution() -> None:
    out = SourceRouter().route(
        _decomp(query_type="scoped", hotel_mention=None),
        session={"active_hotel_id": "rixos_belek"},
    )
    assert out["path"] == PATH_SCOPED


# ----------------- contract -----------------


def test_response_shape_contract() -> None:
    out = SourceRouter().route(_decomp(query_type="broad", region="antalya"))
    assert set(out.keys()) == {"path", "sources", "reason", "needs"}
    assert isinstance(out["sources"], list)
