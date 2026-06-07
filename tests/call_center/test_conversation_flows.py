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
# "antalya" returns 3 (below the narrowing threshold → presented directly);
# "belek" returns 4 (>= threshold → triggers progressive narrowing).
_REGION_HOTELS = {
    "antalya": ["crystal_tat_beach", "akra_kemer", "regnum_carya"],
    "belek": ["crystal_tat_beach", "akra_kemer", "regnum_carya", "selectum_belek"],
}


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
        rerank: bool = True,
    ) -> dict[str, Any]:
        self.calls.append(
            {"region": region, "requirements": list(requirements or []), "hotel_id": hotel_id}
        )
        norm = [r for r in (requirements or []) if r and r.strip()]
        if not norm:
            return _empty(region, "empty_requirements")
        # Test marker: the structured requirement deliberately finds nothing, so
        # the pipeline's semantic fallback (which searches the raw utterance) can
        # be exercised.
        if "empty_marker" in norm:
            return _empty(region, "no_match_above_threshold")
        # Vector name-detection simulation: a query carrying a hotel's distinctive
        # name tokens returns that hotel alone, dominant (mimics Qdrant isolating
        # a named hotel via the relative-margin filter).
        if not hotel_id:
            joined = " ".join(norm).lower()
            matched = [
                hid
                for hid, name in _KB_NAMES.items()
                if sum(1 for t in name.lower().split() if len(t) > 2 and t in joined) >= 2
            ]
            if len(matched) == 1:
                return {
                    "region": region,
                    "requirements": norm,
                    "normalized_requirements": norm,
                    "top_score": 0.85,
                    "count": 1,
                    "hotels": [
                        {
                            "hotel_id": matched[0],
                            "score": 0.85,
                            "payload": {"hotel_name": _KB_NAMES[matched[0]]},
                            "evidence": {},
                        }
                    ],
                    "missing_requirements": [],
                    "reason": None,
                }
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
        if not (region or "").strip():
            # all-regions search → every hotel (the real discovery layer
            # drops the region filter and searches the whole collection).
            ids = list(_KB_NAMES.keys())
        else:
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

    async def fetch_hotel_chunks(self, *, hotel_id, query, region="", k=6):
        """Mimic multi-chunk retrieval for a resolved hotel — several distinct
        passages, including one the single-chunk path would have missed."""
        self.calls.append({"fetch_chunks": hotel_id, "query": query})
        if hotel_id not in _KB_NAMES:
            return []
        name = _KB_NAMES[hotel_id]
        return [
            {
                "chunk_id": "c1",
                "category": "distance_to_beach",
                "text": f"{name} adresi: Torba Mahallesi, Bodrum.",
                "text_en": f"{name} address: Torba, Bodrum.",
                "score": 0.78,
                "hotel_name": name,
            },
            {
                "chunk_id": "c2",
                "category": "location",
                "text": f"{name} denize sıfır konumdadır.",
                "text_en": f"{name} is located right on the beach.",
                "score": 0.74,
                "hotel_name": name,
            },
            {
                "chunk_id": "c3",
                "category": "overview",
                "text": f"{name} 5 yıldızlı bir oteldir.",
                "text_en": f"{name} is a 5-star hotel.",
                "score": 0.71,
                "hotel_name": name,
            },
        ][:k]


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


class _FakeHit:
    def __init__(self, title: str, url: str, content: str, score: float) -> None:
        self.title, self.url, self.content, self.score = title, url, content, score


class _FakeSearchResult:
    def __init__(self, answer: str | None, hits: list[Any] | None = None) -> None:
        self.answer = answer
        self.hits = hits or []
        self.elapsed_ms = 12.0


async def _fake_web_search(query: str, *, max_results: int = 5) -> _FakeSearchResult:
    return _FakeSearchResult(
        answer=f"Web answer for: {query}",
        hits=[_FakeHit("Example Source", "https://example.com", "snippet text", 0.9)],
    )


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


def _d(**kw: Any) -> dict[str, Any]:
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
            _d(query_type="broad", intent="amenities", requirements=["spa", "relaxation"]),
            {"path": "broad_qdrant", "min_hotels": 1, "active_hotel_after": None},
        ),
        (
            "tell me about Crystal Tat Beach Pearl Collection",
            _d(
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
            _d(
                query_type="scoped",
                intent="food",
                hotel_mention=None,
                requirements=["bars", "restaurants"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"], "clarify": False},
        ),
        (
            "give me some romantic resorts",
            _d(
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
            _d(query_type="scoped", hotel_mention="Akra Kemer", requirements=["overview"]),
            {
                "path": "scoped_qdrant",
                "hotel_ids": ["akra_kemer"],
                "active_hotel_after": "akra_kemer",
            },
        ),
        (
            "how about Crystal Tat Beach Pearl Collection?",
            _d(
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
            _d(query_type="scoped", hotel_mention="Akra Kemer", requirements=["overview"]),
            {"path": "scoped_qdrant", "hotel_ids": ["akra_kemer"]},
        ),
        # decomposer echoes the slug + scoped on a clearly broad ask -> guards + broad route
        (
            "I want a spa hotel to relax",
            _d(
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
            _d(
                query_type="scoped",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
        (
            "is the hotel on the beach?",
            _d(
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
            _d(
                query_type="scoped",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
        (
            "tell me more",
            _d(query_type="scoped", hotel_mention=None, requirements=[]),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"], "clarify": False},
        ),
    ],
    "scoped_food_not_clarified": [
        (
            "tell me about Crystal Tat Beach Pearl Collection",
            _d(
                query_type="scoped",
                hotel_mention="Crystal Tat Beach Pearl Collection",
                requirements=["overview"],
            ),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"]},
        ),
        (
            "do they have bars?",
            _d(query_type="scoped", intent="food", hotel_mention=None, requirements=["bars"]),
            {"path": "scoped_qdrant", "hotel_ids": ["crystal_tat_beach"], "clarify": False},
        ),
    ],
    "broad_missing_geography_clarifies": [
        (
            "recommend a nice hotel",
            _d(query_type="broad", intent="recommendation", region=None, requirements=[]),
            {"path": "clarify", "clarify": True},
        ),
    ],
    "named_hotel_resolves_inline": [
        (
            "tell me about Regnum Carya Golf & Spa Resort",
            _d(
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
            _d(query_type="scoped", intent="policy", requirements=["room"]),
            {"path": "escalate", "escalate": True},
        ),
    ],
    # Progressive narrowing: a broad query with 4+ matches (region "belek")
    # asks ONE differentiating question first; the answer turn then proceeds
    # (narrowed flag set → no second narrowing) and presents hotels.
    "broad_with_many_matches_narrows_once": [
        (
            "I want a nice spa hotel",
            _d(query_type="broad", intent="amenities", region="belek", requirements=["spa"]),
            {"path": "clarify", "clarify": True},
        ),
        (
            "mid-range budget, on the beach",
            _d(
                query_type="broad",
                intent="amenities",
                region="belek",
                requirements=["spa"],
                budget_tier="mid",
            ),
            {"path": "broad_qdrant", "min_hotels": 1},
        ),
    ],
    # Below the threshold (region "antalya" returns 3) → presented directly, no narrowing.
    "broad_with_few_matches_does_not_narrow": [
        (
            "I want a nice spa hotel",
            _d(query_type="broad", intent="amenities", region="antalya", requirements=["spa"]),
            {"path": "broad_qdrant", "min_hotels": 1, "clarify": False},
        ),
    ],
}


@pytest.mark.asyncio
async def test_web_path_returns_grounded_answer() -> None:
    """A live-web query (events/weather) returns the web answer + a caveat + sources."""
    from voxtera.call_center.web_retriever import WebRetriever

    p = ConciergePipeline(
        session_store=SessionStore(),
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="web",
                        intent="event",
                        city="Bodrum",
                        region=None,
                        requirements=[],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="are there festivals in Bodrum next week?", session_id="web1")
    assert out["path"] == "web_search"
    assert "Web answer for" in (out["answer"] or "")
    assert "web search" in (out["answer"] or "").lower()  # caveat present
    assert out["retrieval"]["web"]["sources"]


@pytest.mark.asyncio
async def test_explicit_web_request_reruns_prior_question() -> None:
    """A hotel question, then 'can you search online?' -> re-runs the PRIOR
    question on the web (hybrid, since a hotel is active)."""
    from voxtera.call_center.web_retriever import WebRetriever

    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        hotel_mention="Crystal Tat Beach Pearl Collection",
                        region="antalya",
                        requirements=["distance to beach"],
                    ),
                    _d(query_type="broad", requirements=[]),  # T2 decompose runs but is unused
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    sid = "webreq"
    await p.run(
        utterance="how far is Crystal Tat Beach from the beach?", session_id=sid, region="antalya"
    )
    out = await p.run(utterance="can you search on internet?", session_id=sid, region="antalya")
    assert out["path"] == "hybrid"
    assert out["reason"] == "web_request"
    assert "Web answer for" in (out["answer"] or "")
    # the web query was the PRIOR question, not the literal "can you search..."
    assert "Crystal Tat" in out["retrieval"]["web"]["query"]


@pytest.mark.asyncio
async def test_place_availability_question_routes_broad_and_searches_the_place() -> None:
    """'Do you have any hotel in Kaş?' — scoped-with-no-hotel but a PLACE in the
    turn must (1) route BROAD (not the 'which hotel exactly?' dead-end) and
    (2) inject the place into the semantic query, since 'Kaş' is not a
    filterable region — it only exists inside the chunk text."""
    kb = FakeKB()
    store = SessionStore()
    session = await store.load("kas1")
    session["session_id"] = "kas1"
    session["narrowed"] = True  # skip progressive narrowing; test the routing
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="recommendation",
                        hotel_mention=None,
                        region=None,
                        city="Kaş",
                        requirements=["hotels in Kaş"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="so u don't have any hotel in Kaş ?", session_id="kas1")
    assert out["path"] == "broad_qdrant"  # not hotel_resolve
    assert "Which hotel exactly" not in (out["answer"] or "")  # no dead-end
    retrieve_calls = [c for c in kb.calls if c.get("rerank", True)]
    assert any(
        any("kaş" in (r or "").lower() for r in (c.get("requirements") or []))
        for c in retrieve_calls
    )


@pytest.mark.asyncio
async def test_non_canonical_region_becomes_query_term_not_filter() -> None:
    """region='Lycia' isn't a canonical KB bucket — it must NOT become a Qdrant
    filter (which would zero out results); it joins the semantic query instead."""
    kb = FakeKB()
    p = ConciergePipeline(
        session_store=SessionStore(),
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="broad",
                        intent="recommendation",
                        hotel_mention=None,
                        region="Lycia",
                        city=None,
                        requirements=["scuba diving"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    await p.run(utterance="what are the hotels in that region", session_id="lyc1")
    call = [c for c in kb.calls if "scuba diving" in (c.get("requirements") or [])][-1]
    assert call["region"] == ""  # no bogus 'Lycia' filter
    assert "Lycia" in call["requirements"]  # place searched semantically


@pytest.mark.asyncio
async def test_destination_query_answers_from_web_until_destination_kb_ships() -> None:
    """A destination question (itinerary / 'which regions for historical sites')
    must answer from the LIVE WEB instead of dead-ending in the 'destination KB
    ships in the next release' placeholder."""
    from voxtera.call_center.web_retriever import WebRetriever

    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="destination",
                        intent="destination_info",
                        region=None,
                        requirements=["historical sites", "multi-region itinerary"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(
        utterance="I want 4 different places for historical sites, 2-3 days each, not Istanbul",
        session_id="dest",
        region="",
    )
    ans = out["answer"] or ""
    assert "next release" not in ans  # no placeholder dead-end
    assert out["retrieval"].get("web")  # answered from the live web
    assert "Web answer for" in ans  # grounded in the (fake) web result


@pytest.mark.asyncio
async def test_review_query_targets_review_sites_and_routes_to_web() -> None:
    """A reviews question about the active hotel must (1) route to web/hybrid (not
    dead-end in the KB) and (2) restrict the web search to review domains."""
    from voxtera.call_center.web_retriever import WebRetriever

    seen: dict[str, Any] = {}

    async def _capturing_search(query, *, max_results=5, include_domains=None):
        seen["query"] = query
        seen["include_domains"] = include_domains
        return _FakeSearchResult(answer="Guests rate it highly.")

    store = SessionStore()
    session = await store.load("rev")
    session["session_id"] = "rev"
    session["active_hotel_id"] = "crystal_tat_beach"
    session["active_region"] = "antalya"
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    # decomposer flags reviews as needing the web
                    _d(
                        query_type="scoped",
                        intent="amenities",
                        hotel_mention=None,
                        region="antalya",
                        requirements=["guest reviews", "ratings"],
                        source_required=["hotel_kb", "web"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_capturing_search),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(
        utterance="what are the reviews of this hotel?", session_id="rev", region="antalya"
    )
    assert out["path"] == "hybrid"  # routed to web, not KB-only
    assert "tripadvisor.com" in (seen.get("include_domains") or [])  # targeted review sites
    assert out["retrieval"].get("web")


@pytest.mark.asyncio
async def test_warm_lookup_offer_then_yes_runs_web_not_conversational() -> None:
    """The warm persona offers a lookup as 'would you like me to look into…?'
    (no literal 'online'). A bare 'yes' must run the actual web search, NOT fall
    into the conversational path that fabricates 'I'll get you the details'."""
    from voxtera.call_center.web_retriever import WebRetriever

    def _offer_render():
        async def render(payload: dict[str, Any]) -> str:
            return (
                "Casa Dell Arte has a spa. For diving, we can arrange it nearby. "
                "Would you like me to look into some recommendations?"
            )

        return render

    store = SessionStore()
    session = await store.load("offyes")
    session["session_id"] = "offyes"
    session["active_hotel_id"] = "crystal_tat_beach"
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="amenities",
                        hotel_mention=None,
                        region="antalya",
                        requirements=["spa", "diving"],
                    ),
                    _d(
                        query_type="conversational", requirements=[]
                    ),  # decomposer calls bare "yes" chitchat
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        render_fn=_offer_render(),
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    sid = "offyes"
    await p.run(utterance="do they have a spa and diving?", session_id=sid, region="antalya")
    out = await p.run(utterance="yes", session_id=sid, region="antalya")
    assert out["reason"] == "web_request"  # actually searched
    assert out["path"] != "conversational"  # did NOT fabricate
    assert out["retrieval"].get("web")  # a web result is present


@pytest.mark.asyncio
async def test_explicit_web_request_is_clean_for_voice() -> None:
    """After 'yes search online', the spoken answer must: use the synthesized
    (de-contradicted) web answer, carry NO 'Sources:' list, and NOT repeat the
    'would you like me to check online?' offer from the prior turn."""
    from voxtera.call_center.web_retriever import WebRetriever

    async def _synth(payload: dict[str, Any]) -> str:
        return "It is right on the beach."

    def _offer_render():
        async def render(payload: dict[str, Any]) -> str:
            return "I don't see the distance — would you like me to check online?"

        return render

    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        hotel_mention="Crystal Tat Beach Pearl Collection",
                        region="antalya",
                        requirements=["distance to beach"],
                    ),
                    _d(query_type="broad", requirements=[]),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        web_synth_fn=_synth,
        render_fn=_offer_render(),
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    sid = "webclean"
    await p.run(
        utterance="how far is Crystal Tat Beach from the beach?", session_id=sid, region="antalya"
    )
    out = await p.run(utterance="yes search the internet", session_id=sid, region="antalya")
    ans = out["answer"] or ""
    assert "It is right on the beach." in ans  # synthesized answer used
    assert "Sources:" not in ans  # no citation list (voice)
    assert "would you like me to check online" not in ans.lower()  # no repeated offer


@pytest.mark.asyncio
async def test_hybrid_path_combines_hotel_and_web() -> None:
    """Hotel KB + live web (e.g. 'dive shop near my hotel'): both parts present."""
    from voxtera.call_center.web_retriever import WebRetriever

    store = SessionStore()
    session = await store.load("hyb")
    session["session_id"] = "hyb"
    session["active_hotel_id"] = "crystal_tat_beach"
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="hybrid",
                        intent="local_operator",
                        hotel_mention=None,
                        region="antalya",
                        requirements=["dive shop"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="is there a dive shop near my hotel?", session_id="hyb")
    assert out["path"] == "hybrid"
    ans = out["answer"] or ""
    assert "Web answer for" in ans  # web part
    assert "Crystal Tat" in ans or "Here are matches" in ans  # hotel part
    assert out["retrieval"]["web"]["answer"]


@pytest.mark.asyncio
async def test_hybrid_uses_single_combined_synth_not_two_parts() -> None:
    """When a synth is wired, the hybrid path produces ONE combined answer (hotel
    facts woven with web) — not a hotel reply glued to 'Nearby (from a web
    search):'. The synth must receive the hotel's guide facts."""
    from voxtera.call_center.web_retriever import WebRetriever

    seen: dict[str, Any] = {}

    async def _synth(payload: dict[str, Any]) -> str:
        seen["payload"] = payload
        return "One coherent concierge reply combining spa and nearby diving."

    store = SessionStore()
    session = await store.load("hyb2")
    session["session_id"] = "hyb2"
    session["active_hotel_id"] = "crystal_tat_beach"
    session["active_region"] = "antalya"
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="hybrid",
                        intent="amenities",
                        hotel_mention=None,
                        region="antalya",
                        requirements=["scuba diving"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_fake_web_search),
        web_synth_fn=_synth,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="can they arrange diving there?", session_id="hyb2")
    assert out["path"] == "hybrid"
    assert out["answer"] == "One coherent concierge reply combining spa and nearby diving."
    assert "Nearby (from a web search)" not in (out["answer"] or "")
    # the synth was given the hotel's own guide facts to weave in
    assert "Hotel:" in (seen["payload"].get("hotel_facts") or "")


@pytest.mark.asyncio
async def test_named_hotel_detected_when_decomposer_misses_it() -> None:
    """Decomposer returns no hotel_mention (LLM miss), but the utterance names a
    hotel -> ES detection locks onto it, overriding a stale session hotel.
    This is the 'Casa Dell Arte' failure mode."""
    kb = FakeKB()
    store = SessionStore()
    session = await store.load("det")
    session["session_id"] = "det"
    session["active_hotel_id"] = "akra_kemer"  # stale from a prior turn
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="practical_info",
                        hotel_mention=None,
                        region=None,
                        requirements=["distance to beach"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="how far is Crystal Tat Beach from the beach?", session_id="det")
    assert out["path"] == "scoped_qdrant"
    # detected Crystal Tat from the utterance, NOT the stale Akra Kemer
    assert [h["hotel_id"] for h in out["retrieval"]["hotels"]] == ["crystal_tat_beach"]
    reloaded = await store.load("det")
    assert reloaded.get("active_hotel_id") == "crystal_tat_beach"


class _NearTieKB(FakeKB):
    """Detection (rerank=False, no hotel_id) returns two near-tied same-name
    hotels — top 0.838, runner-up 0.821 (gap 0.017 < _DETECT_MARGIN). This is
    the real 'Casa Dell Arte' shape: two hotels share the name. Scoped retrieval
    (with hotel_id) resolves the chosen hotel normally."""

    async def discover(
        self,
        *,
        region,
        requirements,
        activity_tags=None,
        category_hint=None,
        hotel_id=None,
        rerank=True,
    ):
        self.calls.append(
            {
                "region": region,
                "requirements": list(requirements or []),
                "hotel_id": hotel_id,
                "rerank": rerank,
            }
        )
        if hotel_id is None and not rerank:  # detection call
            return {
                "region": region,
                "requirements": list(requirements),
                "normalized_requirements": [],
                "top_score": 0.838,
                "count": 2,
                "hotels": [
                    {
                        "hotel_id": "casa_dell_arte_res",
                        "score": 0.838,
                        "payload": {},
                        "evidence": {},
                    },
                    {
                        "hotel_id": "casa_dell_arte_arts",
                        "score": 0.821,
                        "payload": {},
                        "evidence": {},
                    },
                ],
                "missing_requirements": [],
                "reason": None,
            }
        ids = [hotel_id] if hotel_id else []
        return {
            "region": region,
            "requirements": list(requirements),
            "normalized_requirements": [],
            "top_score": 0.84 if ids else 0.0,
            "count": len(ids),
            "hotels": [
                {
                    "hotel_id": hotel_id,
                    "score": 0.84,
                    "payload": {"hotel_name": "Casa Dell Arte Residance"},
                    "evidence": {},
                }
            ]
            if ids
            else [],
            "missing_requirements": [],
            "reason": None if ids else "no_match_above_threshold",
        }


@pytest.mark.asyncio
async def test_near_tied_same_name_hotels_accepts_top_via_strong_score() -> None:
    """Two same-name hotels sit near-tied (0.838 vs 0.821); the gap is below
    _DETECT_MARGIN, but the top is a strong absolute name match (>=0.82) so the
    strong-score short-circuit accepts it instead of falling through to a
    reranked 'beach' search that returns the wrong (Casa Fora) hotel."""
    kb = _NearTieKB()
    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="practical_info",
                        hotel_mention=None,
                        region=None,
                        requirements=["distance to beach"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="How far is Casa Dell Arte from the beach?", session_id="ct")
    assert [h["hotel_id"] for h in out["retrieval"]["hotels"]] == ["casa_dell_arte_res"]


class _GenericNearTieKB(FakeKB):
    """Detection returns a near-PERFECT tie (0.827 vs 0.826) — the signature of a
    nameless generic follow-up ("do they have spa?"), NOT a real name match."""

    async def discover(
        self,
        *,
        region,
        requirements,
        activity_tags=None,
        category_hint=None,
        hotel_id=None,
        rerank=True,
    ):
        self.calls.append(
            {
                "region": region,
                "requirements": list(requirements or []),
                "hotel_id": hotel_id,
                "rerank": rerank,
            }
        )
        if hotel_id is None and not rerank:  # detection probe
            return {
                "region": region,
                "requirements": list(requirements),
                "normalized_requirements": [],
                "top_score": 0.827,
                "count": 2,
                "hotels": [
                    {"hotel_id": "city_live_otel", "score": 0.827, "payload": {}, "evidence": {}},
                    {"hotel_id": "asia_beach", "score": 0.826, "payload": {}, "evidence": {}},
                ],
                "missing_requirements": [],
                "reason": None,
            }
        return _empty(region, "no_match_above_threshold")

    async def fetch_hotel_chunks(self, *, hotel_id, query, region="", k=6):
        return [
            {
                "chunk_id": "c1",
                "category": "overview",
                "text": f"{hotel_id} info",
                "text_en": f"{hotel_id} info",
                "score": 0.8,
                "hotel_name": hotel_id,
            }
        ]


class _ContentMatchKB(FakeKB):
    """Detection returns a hotel with a CLEAR score gap (0.822 vs 0.810) but whose
    NAME isn't in the utterance — a content match ('restaurants' query hitting a
    restaurant-dense hotel), not a name mention."""

    async def discover(
        self,
        *,
        region,
        requirements,
        activity_tags=None,
        category_hint=None,
        hotel_id=None,
        rerank=True,
    ):
        self.calls.append(
            {
                "region": region,
                "requirements": list(requirements or []),
                "hotel_id": hotel_id,
                "rerank": rerank,
            }
        )
        if hotel_id is None and not rerank:  # detection probe
            return {
                "region": region,
                "requirements": list(requirements),
                "normalized_requirements": [],
                "top_score": 0.822,
                "count": 2,
                "hotels": [
                    {
                        "hotel_id": "moonshine",
                        "score": 0.822,
                        "payload": {"hotel_name": "Moonshine Hotel & Suites"},
                        "evidence": {},
                    },
                    {"hotel_id": "other", "score": 0.810, "payload": {}, "evidence": {}},
                ],
                "missing_requirements": [],
                "reason": None,
            }
        return _empty(region, "no_match_above_threshold")

    async def fetch_hotel_chunks(self, *, hotel_id, query, region="", k=6):
        return [
            {
                "chunk_id": "c1",
                "category": "food_beverage",
                "text": f"{hotel_id} info",
                "text_en": f"{hotel_id} info",
                "score": 0.8,
                "hotel_name": hotel_id,
            }
        ]


@pytest.mark.asyncio
async def test_content_match_without_name_does_not_hijack_active_hotel() -> None:
    """A nameless follow-up ('what restaurants do they have?') that scores high
    against a restaurant-dense hotel must NOT switch away from the active hotel,
    because that hotel's NAME ('Moonshine') is nowhere in the utterance."""
    kb = _ContentMatchKB()
    store = SessionStore()
    session = await store.load("cm")
    session["session_id"] = "cm"
    session["active_hotel_id"] = "crystal_tat_beach"  # the hotel actually in context
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="amenities",
                        hotel_mention=None,
                        region=None,
                        requirements=["restaurant names"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(
        utterance="what type of restaurants do they have and their names?", session_id="cm"
    )
    assert [h["hotel_id"] for h in out["retrieval"]["hotels"]] == ["crystal_tat_beach"]
    reloaded = await store.load("cm")
    assert reloaded.get("active_hotel_id") == "crystal_tat_beach"  # Moonshine did NOT hijack


@pytest.mark.asyncio
async def test_generic_followup_does_not_hijack_active_hotel() -> None:
    """A nameless follow-up ('do they have spa?') must NOT let a near-tied generic
    detection override the hotel already in context — it stays on the active hotel."""
    kb = _GenericNearTieKB()
    store = SessionStore()
    session = await store.load("hj")
    session["session_id"] = "hj"
    session["active_hotel_id"] = "casa_dell_arte_res"  # resolved on a prior turn
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="amenities",
                        hotel_mention=None,
                        region=None,
                        requirements=["scuba diving", "spa"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(utterance="do they have scuba diving and spa?", session_id="hj")
    assert [h["hotel_id"] for h in out["retrieval"]["hotels"]] == ["casa_dell_arte_res"]
    reloaded = await store.load("hj")
    assert reloaded.get("active_hotel_id") == "casa_dell_arte_res"  # not hijacked


@pytest.mark.asyncio
async def test_scoped_resolved_hotel_passes_multiple_chunks_to_render() -> None:
    """A question about a KNOWN hotel must reach the render LLM with SEVERAL of
    that hotel's passages (not one best-matching chunk), so a detail buried in a
    different chunk ('located on the beach') is available to answer from."""
    captured: dict[str, Any] = {}

    async def _capturing_render(payload: dict[str, Any]) -> str:
        captured["payload"] = payload
        return "ok"

    kb = FakeKB()
    store = SessionStore()
    session = await store.load("sc")
    session["session_id"] = "sc"
    session["active_hotel_id"] = "crystal_tat_beach"  # resolved on a prior turn
    session["active_region"] = "antalya"
    await store.save(session)
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="scoped",
                        intent="practical_info",
                        hotel_mention=None,
                        region="antalya",
                        requirements=["distance to beach"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_capturing_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    await p.run(utterance="how far is it from the beach?", session_id="sc")
    evidence = captured["payload"]["retrieval"]["hotels"][0]["evidence"]
    # Multiple distinct passages, including the location chunk that states the
    # beach proximity the single-chunk path would have missed.
    assert len(evidence) >= 2
    blob = " ".join((c.get("text_en") or c.get("text") or "") for c in evidence.values()).lower()
    assert "on the beach" in blob


@pytest.mark.asyncio
async def test_semantic_fallback_used_when_structured_empty() -> None:
    """When the structured requirements find nothing, the pipeline searches the
    raw utterance and uses that result (Idea 1)."""
    kb = FakeKB()
    p = ConciergePipeline(
        session_store=SessionStore(),
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    # structured requirement is the marker -> FakeKB returns nothing
                    _d(
                        query_type="broad",
                        intent="amenities",
                        region="antalya",
                        requirements=["empty_marker"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(
        utterance="how far is Crystal Tat from the beach?", session_id="fb", region="antalya"
    )
    assert out["retrieval"] is not None
    assert out["retrieval"]["reason"] == "semantic_fallback"
    assert out["retrieval"]["hotels"], "fallback should have recovered hotels"
    # The fallback queried the full utterance, not the empty marker.
    assert kb.calls[-1]["requirements"] == ["how far is Crystal Tat from the beach?"]


@pytest.mark.asyncio
async def test_all_regions_clears_stale_region_and_searches_all() -> None:
    """Picking 'All regions' (region='') must clear a prior turn's region,
    not ask for geography, and search across all regions — the Casa Dell Arte bug.
    """
    kb = FakeKB()
    store = SessionStore()
    session = await store.load("ar")
    session["session_id"] = "ar"
    session["active_region"] = "antalya"  # stale from a prior turn
    await store.save(session)

    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(query_type="broad", intent="amenities", region=None, requirements=["spa"]),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    out = await p.run(
        utterance="a nice spa hotel", session_id="ar", region=""
    )  # explicit all-regions

    # Did NOT ask for geography (the all_regions flag satisfies it).
    assert out["reason"] != "missing_geography", out
    # Searched ALL regions, not the stale 'antalya'.
    assert kb.calls, "discover was never called"
    assert kb.calls[-1]["region"] == "", f"expected empty region, got {kb.calls[-1]['region']!r}"
    # Session reflects all-regions; stale region cleared.
    reloaded = await store.load("ar")
    assert reloaded.get("all_regions") is True
    assert not reloaded.get("active_region")


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
            assert sess.get("active_hotel_id") == expect["active_hotel_after"], (
                f"{ctx}: active_hotel_id {sess.get('active_hotel_id')!r} "
                f"!= {expect['active_hotel_after']!r}"
            )


# ---------------------------------------------------------------------------
# Conversational memory (full history in session, fed to the LLM, converse path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_conversation_history_persists_beyond_old_8_turn_cap() -> None:
    """The session keeps the WHOLE conversation (not an 8-turn ring) so the agent
    has real dialogue memory for a voice call."""
    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [_d(query_type="broad", requirements=["spa"]) for _ in range(12)]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    sid = "longconvo"
    for i in range(12):
        await p.run(utterance=f"spa hotel question {i}", session_id=sid, region="antalya")
    sess = await store.load(sid)
    assert len(sess["history"]) == 12  # all turns kept, not capped at 8


@pytest.mark.asyncio
async def test_transcript_is_fed_to_decomposer() -> None:
    """From the second turn on, the decomposer receives the prior conversation as
    a transcript so it can resolve follow-ups."""
    seen_ctx: list[dict[str, Any]] = []

    async def _capturing_decompose(utterance: str, ctx: dict[str, Any]) -> dict[str, Any]:
        seen_ctx.append(ctx)
        return _d(query_type="broad", requirements=["spa"])

    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(decompose_fn=_capturing_decompose),
        triage=Triage(),
        compound=FakeKB(),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    sid = "tx"
    await p.run(utterance="I want a spa hotel", session_id=sid, region="antalya")
    await p.run(utterance="and a pool?", session_id=sid, region="antalya")
    assert not (seen_ctx[0].get("transcript") or "")  # nothing before turn 1
    assert "I want a spa hotel" in (seen_ctx[1].get("transcript") or "")  # turn 1 visible on turn 2


@pytest.mark.asyncio
async def test_conversational_turn_answers_from_history_without_retrieval() -> None:
    """A 'what did I ask you?' turn is answered from the transcript by converse_fn
    and never touches the hotel KB."""
    captured: dict[str, Any] = {}

    async def _converse(payload: dict[str, Any]) -> str:
        captured["payload"] = payload
        return "You asked about a spa hotel."

    kb = FakeKB()
    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(query_type="broad", requirements=["spa"]),
                    _d(query_type="conversational", requirements=[]),
                ]
            )
        ),
        triage=Triage(),
        compound=kb,
        render_fn=_fake_render,
        converse_fn=_converse,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    sid = "convo"
    await p.run(utterance="I want a spa hotel", session_id=sid, region="antalya")
    kb_calls_before = len(kb.calls)
    out = await p.run(utterance="what did I ask you?", session_id=sid, region="antalya")
    assert out["path"] == "conversational"
    assert out["answer"] == "You asked about a spa hotel."
    assert "I want a spa hotel" in (captured["payload"]["transcript"] or "")
    assert len(kb.calls) == kb_calls_before  # no retrieval on the conversational turn


@pytest.mark.asyncio
async def test_hybrid_web_query_uses_hotel_name_not_pronoun() -> None:
    """A hybrid turn like 'can I do scuba diving there, do they have a spa?' must
    send the HOTEL NAME + requirements to the web — not the raw pronoun utterance,
    which returns generic chatter."""
    from voxtera.call_center.web_retriever import WebRetriever

    seen: dict[str, Any] = {}

    async def _capturing_search(query: str, *, max_results: int = 5) -> _FakeSearchResult:
        seen["query"] = query
        return _FakeSearchResult(answer="Yes, diving and spa available.")

    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(
                        query_type="hybrid",
                        intent="amenities",
                        hotel_mention="Crystal Tat Beach Pearl Collection",
                        region="antalya",
                        requirements=["scuba diving", "spa"],
                    ),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_capturing_search),
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    await p.run(
        utterance="can I do scuba diving there, do they have a spa?",
        session_id="hyb",
        region="antalya",
    )
    q = seen["query"].lower()
    assert "crystal tat" in q and "scuba diving" in q and "spa" in q
    assert "there" not in q.split()  # the pronoun utterance was not used verbatim


@pytest.mark.asyncio
async def test_dialog_rewrite_query_wins_over_heuristic() -> None:
    """When a dialog-aware query rewriter is wired, its query (built from the
    conversation, independent of ES/decomposition) is what hits the web — not the
    decomposition-derived heuristic."""
    from voxtera.call_center.web_retriever import WebRetriever

    seen: dict[str, Any] = {}

    async def _capturing_search(query: str, *, max_results: int = 5) -> _FakeSearchResult:
        seen["query"] = query
        return _FakeSearchResult(answer="ok")

    async def _rewrite(payload: dict[str, Any]) -> str:
        # Pretend the LLM read the dialog and produced a clean query.
        return "Casa Dell Arte Residance Torba Bodrum scuba diving"

    store = SessionStore()
    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(classify_fn=_classify_fn, cache_get=None, cache_set=None),
        decomposer=QueryDecomposer(
            decompose_fn=_ScriptedDecomposer(
                [
                    _d(query_type="web", intent="local_operator", requirements=["scuba diving"]),
                ]
            )
        ),
        triage=Triage(),
        compound=FakeKB(),
        web_retriever=WebRetriever(search_fn=_capturing_search),
        web_query_fn=_rewrite,
        render_fn=_fake_render,
        resolver=HotelResolver(search_fn=_fake_resolver_search),
    )
    await p.run(utterance="is there diving there?", session_id="dq", region="antalya")
    assert seen["query"] == "Casa Dell Arte Residance Torba Bodrum scuba diving"
