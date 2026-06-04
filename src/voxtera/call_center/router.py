"""Source Router — deterministic 5-path retrieval decision (Phase 3.5).

Sits between Triage and the retrieval layer. Pure function — no LLM,
no IO — so the decision tree is testable in isolation and identical
across every call.

Decision tree (Voxtera_RAG_Architecture_v0.3 §5.1):

  1. escalate            → human agent, no retrieval
  2. time-sensitive      → Path 4 (Web) if geography known
                           else needs_geography
  3. local-operator      → Path 5 (Hybrid) if hotel known
                           else Path 4 (Web) with location filter
  4. specific hotel      → Path 1 (Scoped Qdrant) if hotel_id resolved
                           else hotel_resolve (Elasticsearch step)
  5. multi-hotel reco    → Path 2 (Broad Qdrant)
  6. destination-level   → Path 3 (Destination KB)
  7. fallthrough         → Path 2 (Broad Qdrant)

Decision contract (returned by ``SourceRouter.route``):

    {
      "path":      str,          # one of PATHS
      "sources":   list[str],    # subset of {"hotel_kb", "destination_kb", "web"}
      "reason":    str,          # short audit string for logs/UI
      "needs":     str | None,   # "geography" / "hotel_resolve" / None
    }
"""

from __future__ import annotations

from typing import Any

# Path identifiers — kept stable across versions; downstream retrievers
# dispatch on these strings.
PATH_ESCALATE = "escalate"
PATH_SCOPED = "scoped_qdrant"          # Path 1
PATH_BROAD = "broad_qdrant"            # Path 2
PATH_DESTINATION = "destination_kb"    # Path 3
PATH_WEB = "web_search"                # Path 4
PATH_HYBRID = "hybrid"                 # Path 5
PATH_HOTEL_RESOLVE = "hotel_resolve"   # Elasticsearch step before Path 1
PATH_NEEDS_GEOGRAPHY = "needs_geography"  # triage gap that slipped through

PATHS = {
    PATH_ESCALATE, PATH_SCOPED, PATH_BROAD, PATH_DESTINATION,
    PATH_WEB, PATH_HYBRID, PATH_HOTEL_RESOLVE, PATH_NEEDS_GEOGRAPHY,
}

# Intents that flag a time-sensitive query (must go to web).
_TIME_SENSITIVE_INTENTS = {"event", "weather", "practical_info"}

# Intents that indicate a local small-business query.
_LOCAL_OPERATOR_INTENTS = {"local_operator"}

# Intents that indicate destination-level (cultural / geographic) knowledge.
_DESTINATION_INTENTS = {
    "destination_info", "etiquette", "landmarks", "visa",
}


def _has_geography(decomposition: dict[str, Any], session: dict[str, Any] | None) -> bool:
    for k in ("city", "region", "district", "hotel_mention"):
        v = decomposition.get(k)
        if isinstance(v, str) and v.strip():
            return True
    if session:
        for k in ("active_region", "active_hotel_id"):
            v = session.get(k)
            if isinstance(v, str) and v.strip():
                return True
    return False


def _resolved_hotel_id(decomposition: dict[str, Any], session: dict[str, Any] | None) -> str | None:
    """Return a canonical hotel_id when one is locked in session, else None.

    Note: ``decomposition.hotel_mention`` is a raw caller phrase — it is
    NOT a resolved id. Only ``session.active_hotel_id`` (set after the
    hotel resolver succeeds) counts as "resolved".
    """
    if session:
        v = session.get("active_hotel_id")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _decide(
    decomposition: dict[str, Any],
    session: dict[str, Any] | None,
) -> dict[str, Any]:
    qt = (decomposition.get("query_type") or "").lower()
    intent = (decomposition.get("intent") or "").lower()
    hotel_mention = decomposition.get("hotel_mention")
    hotel_id = _resolved_hotel_id(decomposition, session)
    has_geo = _has_geography(decomposition, session)

    # 1. Escalation short-circuit.
    if qt == "escalate" or (decomposition.get("urgency") == "immediate_escalation"):
        return {"path": PATH_ESCALATE, "sources": [], "reason": "escalation_trigger", "needs": None}

    # 2. Time-sensitive → web.
    if qt == "web" or intent in _TIME_SENSITIVE_INTENTS:
        if not has_geo:
            return {
                "path": PATH_NEEDS_GEOGRAPHY, "sources": [],
                "reason": "web_query_missing_geography", "needs": "geography",
            }
        return {"path": PATH_WEB, "sources": ["web"], "reason": "time_sensitive", "needs": None}

    # 3. Local operators → hybrid if hotel known, else web with location.
    if qt == "hybrid" or intent in _LOCAL_OPERATOR_INTENTS:
        if hotel_id or (isinstance(hotel_mention, str) and hotel_mention.strip()):
            return {
                "path": PATH_HYBRID, "sources": ["hotel_kb", "web"],
                "reason": "local_operator_with_hotel", "needs": None,
            }
        if not has_geo:
            return {
                "path": PATH_NEEDS_GEOGRAPHY, "sources": [],
                "reason": "local_operator_missing_geography", "needs": "geography",
            }
        return {
            "path": PATH_WEB, "sources": ["web"],
            "reason": "local_operator_no_hotel", "needs": None,
        }

    # 4. Specific hotel query.
    if qt == "scoped" or (isinstance(hotel_mention, str) and hotel_mention.strip()):
        if hotel_id:
            return {
                "path": PATH_SCOPED, "sources": ["hotel_kb"],
                "reason": "hotel_resolved", "needs": None,
            }
        return {
            "path": PATH_HOTEL_RESOLVE, "sources": [],
            "reason": "hotel_mention_unresolved", "needs": "hotel_resolve",
        }

    # 5. Multi-hotel recommendation / comparison / compound.
    if qt in {"broad", "comparison", "compound"}:
        if not has_geo:
            return {
                "path": PATH_NEEDS_GEOGRAPHY, "sources": [],
                "reason": "broad_query_missing_geography", "needs": "geography",
            }
        return {
            "path": PATH_BROAD, "sources": ["hotel_kb"],
            "reason": f"broad_{qt}", "needs": None,
        }

    # 6. Destination-level knowledge.
    if qt == "destination" or intent in _DESTINATION_INTENTS:
        return {
            "path": PATH_DESTINATION, "sources": ["destination_kb"],
            "reason": "destination_level", "needs": None,
        }

    # 7. Fallthrough — broad Qdrant if we have geography, else ask.
    if not has_geo:
        return {
            "path": PATH_NEEDS_GEOGRAPHY, "sources": [],
            "reason": "fallthrough_missing_geography", "needs": "geography",
        }
    return {
        "path": PATH_BROAD, "sources": ["hotel_kb"],
        "reason": "fallthrough_default", "needs": None,
    }


class SourceRouter:
    """Deterministic 5-path routing per Architecture §5.1."""

    def route(
        self,
        decomposition: dict[str, Any],
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _decide(decomposition or {}, session)
