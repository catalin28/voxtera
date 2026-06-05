"""Conversation-level eval — multi-turn flows through the REAL pipeline.

Why this file exists
--------------------
The component unit tests check each stage in isolation with hand-written,
perfect decompositions. Every bug we hit in manual testing instead came from
the *integration* of three things: a real (sometimes sloppy) decomposition,
multi-turn session state, and the routing/triage branches. Those never met in
the unit suite, so it stayed green while live conversations broke.

This harness runs whole conversations through the real ConciergePipeline
(EscalationClassifier + QueryDecomposer coerce + Triage + SourceRouter +
session store + scoped/empty/guard logic). The only fakes are the leaves that
need network: the KB retriever, the hotel resolver, and the render LLM. Each
turn scripts what the decomposer *returns* (including the messy outputs that
exposed real bugs — slug echoes, generic refs, empty requirements, a
practical_info intent on a scoped query) and asserts the path, the hotel(s)
retrieved, and the resulting session state.

Companion spec: docs/call-center/conversation-eval.md
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.pipeline import ConciergePipeline
from voxtera.call_center.resolver import HotelResolver
from voxtera.call_center.session import SessionStore
from voxtera.call_center.triage import Triage

# ---------------------------------------------------------------------------
# Fake leaves (network boundaries only)
# ---------------------------------------------------------------------------

_KB_NAMES = {
    "crystal_tat_beach": "Crystal Tat Beach Pearl Collection",
    "akra_kemer": "Akra Kemer",
    "regnum_carya": "Regnum Carya Golf & Spa Resort",
    "selectum_belek": "Selectum Luxury Resort Belek",
}
_REGION_HOTELS = {"antalya": ["crystal_tat_beach", "akra_kemer", "regnum_carya", "selectum_belek"]}


def _hotels_payload(ids: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "hotel_id": hid,
            "score": round(0.80 - i * 0.01, 3),
            "payload": {"hotel_name": _KB_NAMES[hid]},
            "evidence": {},
        }
        for i, hid in enumerate(ids)
    ]


def _empty(region: str, reason: str) -> dict[str, Any]:
    return {
        "region": region,
        "requirements": [],
        "normalized_requirements": [],
        "top_score": 0.0,
        "count": 0,
        "hotels": [],
        "missing_requirements": [],
        "reason": reason,
    }


class FakeKB:
    """Stands in for CompoundAndDiscovery — mirrors its contract closely."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def discover(
        self,
        *,
        region: str,
        requirements: list[str],
        activity_tags: Any = None,
        category_hint: Any = None,
        hotel_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"region": region, "requirements": list(requirements or []), "hotel_id": hotel_id}
        )
        norm = [r for r in (requirements or []) if r and r.strip()]
        if not norm:
            return _empty(region, "empty_requirements")
        if hotel_id:
            ids = [hotel_id] if hotel_id in _KB_NAMES else []
            return (
                {
                    "region": region,
                    "requirements": norm,
                    "normalized_requirements": norm,
                    "top_score": 0.8,
                    "count": len(ids),
                    "hotels": _hotels_payload(ids),
                    "missing_requirements": [],
                    "reason": None,
                }
                if ids
                else _empty(region, "no_match_above_threshold")
            )
        ids = _REGION_HOTELS.get((region or "").strip().lower(), [])
        if not ids:
            return _empty(region, "no_match_above_threshold")
        return {
            "region": region,
            "requirements": norm,
            "normalized_requirements": norm,
            "top_score": 0.8,
            "count": len(ids[:4]),
            "hotels": _hotels_payload(ids[:4]),
            "missing_requirements": [],
            "reason": None,
        }


async def _fake_resolver_search(query: str, size: int) -> list[dict[str, Any]]:
    """Token-overlap match of a typed name -> hotel id (mimics ES BM25)."""
    q = {w for w in query.lower().split() if len(w) > 2}
    best, best_score = None, 0.0
    for hid, name in _KB_NAMES.items():
        nt = {w for w in name.lower().split() if len(w) > 2}
        if not nt:
            continue
        overlap = len(q & nt) / len(nt)
        if overlap > best_score:
            best, best_score = (hid, name), overlap
    if best and best_score >= 0.5:
        return [{"_score": 0.95, "_source": {"hotel_id": best[0], "name": best[1]}}]
    return []


async def _fake_render(payload: dict[str, Any]) -> str:
    hotels = (payload.get("retrieval") or {}).get("hotels") or []
    names = ", ".join(h["payload"]["hotel_name"] for h in hotels)
    return f"Here are matches: {names}."


async def _classify_fn(utterance: str) -> dict[str, Any]:
    u = utterance.lower()
    if "room is not ready" in u or "odama gir" in u or "not ready" in u:
        return {"type": "live_complaint", "confidence": 0.95, "signal": utterance}
    return {"type": "none", "confidence": 0.0, "signal": None}


class _ScriptedDecomposer:
    """Emits the per-turn raw decomposition in order (pre-coerce)."""

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self._outputs = list(outputs)
        self._i = 0

    async def __call__(self, utterance: str, ctx: dict[str, Any]) -> dict[str, Any]:
        out = dict(self._outputs[self._i])
        self._i += 1
        return out


def _build_pipeline(decomps: list[dict[str, Any]]) -> tuple[ConciergePipeline, FakeKB, str]:
    kb = FakeKB()
    sid = "evo-" + str(id(kb))
    p = ConciergePipeline(
        session_store=SessionStore(),
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(decompose_fn=_ScriptedDecomposer(decomps)),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    return p, kb, sid


def _D(**kw: Any) -> dict[str, Any]:
    """A raw decomposition with sensible defaults (pre-coerce)."""
    base = {
        "hotel_mention": None,
        "city": None,
        "region": "antalya",
        "district": None,
        "intent": "amenities",
        "query_type": "broad",
        "requirements": [],
        "requirements_logic": "AND",
        "language": "en",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Golden conversations: (name, [ (utterance, raw_decomp, expect) ... ])
# expect keys: path, hotel_ids (list), clarify(bool), escalate(bool),
#              active_hotel_after (str|None|"__skip__")
# ---------------------------------------------------------------------------

SKIP = "__skip__"

CONVERSATIONS: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {
    "spa_then_scoped_then_followup_then_broad": [
        (
            "I want a nice spa hotel to relax",
            _D(query_type="broad", intent="amenities", requirements=["spa", "relaxation"]),
            {"path": "broad_qdrant", "min_hotels": 1, "active_hotel_after": None},
        ),
        (
            "tell me about Crystal Tat Beach Pearl Collection",
            _D(
                query_type="scoped",
                intent="amenities",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["hotel overview", "amenities"],
            ),
            {
                "path": "scoped_qdrant",
                "hotel_ids": ["crystal_tat_beach"],
                "active_hotel_after": "crystal_tat_beach",
            },
        ),
        (
            "do they have bars and restaurants?",
            _D(
                query_type="scoped",
                intent="food",
                hotel_mention=None,
                requirements=["bars", "restaurants"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"], "clarify": False},
        ),
        (
            "give me some romantic resorts",
            _D(
                query_type="broad",
                intent="atmosphere",
                hotel_mention=None,
                requirements=["romantic", "resort"],
            ),
            {"path": "broad_qdrant", "min_hotels": 1, "active_hotel_after": None},
        ),
    ],
    "new_mention_overrides_session": [
        (
            "tell me about Akra Kemer",
            _D(query_type="scoped", hotel_mention="Akra Kemer", requirements=["overview"]),
            {
                "path": "scoped_qdrant",
                "hotel_ids": ["akra_kemer"],
                "active_hotel_after": "akra_kemer",
            },
        ),
        (
            "how about Crystal Tat Beach Pearl Collection?",
            _D(
                query_type="comparison",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {
                "path": "scoped_qdrant",
                "hotel_ids": ["crystal_tat_beach"],
                "active_hotel_after": "crystal_tat_beach",
            },
        ),
    ],
    "slug_echo_does_not_hijack_broad": [
        (
            "tell me about Akra Kemer",
            _D(query_type="scoped", hotel_mention="Akra Kemer", requirements=["overview"]),
            {"path": "scoped_qdrant", "hotel_ids": ["akra_kemer"]},
        ),
        # decomposer echoes the slug + scoped on a clearly broad ask -> guards + broad route
        (
            "I want a spa hotel to relax",
            _D(
                query_type="broad",
                intent="amenities",
                hotel_mention="akra_kemer",
                city="Kemer",
                requirements=["spa", "relaxation"],
            ),
            {"path": "broad_qdrant", "min_hotels": 1, "active_hotel_after": None},
        ),
    ],
    "generic_reference_scopes_to_session_hotel": [
        (
            "tell me about Crystal Tat Beach Pearl Collection",
            _D(
                query_type="scoped",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
        (
            "is the hotel on the beach?",
            _D(
                query_type="scoped",
                intent="practical_info",
                hotel_mention="the hotel",
                requirements=["beach location"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
    ],
    "empty_requirements_injects_overview": [
        (
            "tell me about Crystal Tat Beach Pearl Collection",
            _D(
                query_type="scoped",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
        (
            "tell me more",
            _D(query_type="scoped", hotel_mention=None, requirements=[]),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"], "clarify": False},
        ),
    ],
    "scoped_food_not_clarified": [
        (
            "tell me about Crystal Tat Beach Pearl Collection",
            _D(
                query_type="scoped",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
        (
            "do they have bars?",
            _D(query_type="scoped", intent="food", hotel_mention=None, requirements=["bars"]),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"], "clarify": False},
        ),
    ],
    "broad_missing_geography_clarifies": [
        (
            "recommend a nice hotel",
            _D(query_type="broad", intent="recommendation", region=None, requirements=[]),
            {"path": "clarify", "clarify": True},
        ),
    ],
    "named_hotel_resolves_inline": [
        (
            "tell me about Regnum Carya Golf & Spa Resort",
            _D(
                query_type="scoped",
                hotel_mention="Regnum Carya Golf & Spa Resort",
                requirements=["overview"],
            ),
            {
                "path": "scoped_qdrant",
                "hotel_ids": ["regnum_carya"],
                "active_hotel_after": "regnum_carya",
            },
        ),
    ],
    "live_complaint_escalates": [
        (
            "I'm at the hotel and my room is not ready",
            _D(query_type="scoped", intent="policy", requirements=["room"]),
            {"path": "escalate", "escalate": True},
        ),
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(CONVERSATIONS.keys()))
async def test_conversation_flow(name: str) -> None:
    turns = CONVERSATIONS[name]
    p, kb, sid = _build_pipeline([t[1] for t in turns])

    for idx, (utterance, _decomp, expect) in enumerate(turns):
        ui_region = _decomp.get("region")  # simulate the region dropdown
        out = await p.run(utterance=utterance, session_id=sid, region=ui_region)
        ctx = f"[{name}] turn {idx}: {utterance!r}"

        if "path" in expect:
            assert (
                out["path"] == expect["path"]
            ), f"{ctx}: path {out['path']!r} != {expect['path']!r}"
        if expect.get("escalate"):
            assert out["path"] == "escalate", f"{ctx}: expected escalation"
            continue
        if expect.get("clarify"):
            assert out["path"] == "clarify", f"{ctx}: expected a clarification"
        else:
            if "clarify" in expect:
                assert (
                    out["path"] != "clarify"
                ), f"{ctx}: unexpected clarification ({out.get('reason')})"

        got_ids = [h["hotel_id"] for h in ((out.get("retrieval") or {}).get("hotels") or [])]
        if "hotel_ids" in expect:
            assert (
                got_ids == expect["hotel_ids"]
            ), f"{ctx}: hotels {got_ids} != {expect['hotel_ids']}"
        if "min_hotels" in expect:
            assert (
                len(got_ids) >= expect["min_hotels"]
            ), f"{ctx}: expected >= {expect['min_hotels']} hotels, got {got_ids}"

        if "active_hotel_after" in expect and expect["active_hotel_after"] != SKIP:
            sess = await p._sessions.load(sid)
            assert (
                sess.get("active_hotel_id") == expect["active_hotel_after"]
            ), f"{ctx}: active_hotel_id {sess.get('active_hotel_id')!r} != {expect['active_hotel_after']!r}"
