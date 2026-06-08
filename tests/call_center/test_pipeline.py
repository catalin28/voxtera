"""Unit tests for ConciergePipeline (Phase 3 Slice B orchestrator).

All five Slice-A modules are injected as either real instances (when
their behaviour is part of the test) or scripted fakes (when only
their output matters). No network, no Redis.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.pipeline import ConciergePipeline
from voxtera.call_center.router import (
    PATH_BROAD,
    PATH_ESCALATE,
    PATH_HOTEL_RESOLVE,
    PATH_SCOPED,
    PATH_WEB,
    SourceRouter,
)
from voxtera.call_center.session import SessionStore
from voxtera.call_center.triage import Triage

# ---------- minimal helpers ----------


def _scripted_classify(raw: dict[str, Any]):
    """Return a fake classify_fn whose raw shape is {type, confidence, signal}."""

    async def fn(_u: str) -> dict[str, Any]:
        return raw

    return fn


def _scripted_decompose(payload: dict[str, Any]):
    async def fn(_u: str, _c: dict[str, Any]) -> dict[str, Any]:
        return payload

    return fn


class _FakeCompound:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def discover(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._result


def _ok_retrieval() -> dict[str, Any]:
    return {
        "reason": None,
        "missing_requirements": [],
        "hotels": [
            {
                "hotel_id": "rixos_belek",
                "payload": {"hotel_name": "Rixos Belek"},
                "score": 0.82,
                "evidence": {},
            },
        ],
    }


def _build(
    *,
    classify: dict[str, Any] | None = None,
    decompose: dict[str, Any] | None = None,
    compound: _FakeCompound | None = None,
    render_fn: Any | None = None,
) -> ConciergePipeline:
    classify = classify or {"type": "none", "confidence": 0.1, "signal": None}
    decompose = decompose or {
        "query_type": "broad",
        "intent": "recommendation",
        "region": "antalya",
        "requirements": ["spa"],
        "language": "en",
    }
    return ConciergePipeline(
        session_store=SessionStore(),  # in-memory fallback
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify(classify), cache_get=None, cache_set=None
        ),
        decomposer=QueryDecomposer(decompose_fn=_scripted_decompose(decompose)),
        triage=Triage(),
        router=SourceRouter(),
        compound=compound,
        render_fn=render_fn,
    )


# ---------- 1. empty utterance ----------


@pytest.mark.asyncio
async def test_empty_utterance_short_circuits() -> None:
    out = await _build().run(utterance="   ")
    assert out["path"] == "empty"
    assert "didn't catch" in out["answer"]


# ---------- 2. escalation path ----------


@pytest.mark.asyncio
async def test_escalation_short_circuits_before_decompose() -> None:
    p = _build(
        classify={
            "type": "live_complaint",
            "confidence": 0.92,
            "signal": "odama giremiyorum",
        }
    )
    out = await p.run(utterance="Oteldeyim ve odama giremiyorum")
    assert out["path"] == PATH_ESCALATE
    assert out["escalation"]["escalation_type"] == "live_complaint"
    assert out["decomposition"] is None  # decompose was skipped
    assert "colleague" in out["answer"].lower()


# ---------- 3. clarification path ----------


@pytest.mark.asyncio
async def test_triage_asks_geography_and_persists_clarification_count() -> None:
    # broad query, no geography → triage should ask.
    p = _build(
        decompose={
            "query_type": "broad",
            "intent": "recommendation",
            "requirements": ["spa"],
            "language": "en",
        }
    )
    out = await p.run(utterance="recommend a hotel with a spa")
    assert out["path"] == "clarify"
    assert out["clarification"]["slot"] == "geography"
    assert "destination" in out["answer"].lower()

    # Same session, second turn — still missing geography but triage
    # should still allow one more ask (clarification_count went 0 → 1).
    sid = out["session_id"]
    out2 = await p.run(utterance="another spa hotel", session_id=sid)
    assert out2["path"] == "clarify"

    # Third turn — clarification budget exhausted, pipeline proceeds.
    out3 = await p.run(utterance="another spa hotel", session_id=sid)
    assert out3["path"] != "clarify"


# ---------- 4. broad KB path with retrieval + render ----------


@pytest.mark.asyncio
async def test_broad_path_runs_compound_and_renders() -> None:
    compound = _FakeCompound(_ok_retrieval())

    async def render(_payload: dict[str, Any]) -> str:
        return "Rixos Belek fits the bill."

    p = _build(
        decompose={
            "query_type": "broad",
            "intent": "recommendation",
            "region": "antalya",
            "requirements": ["spa"],
            "language": "en",
        },
        compound=compound,
        render_fn=render,
    )
    out = await p.run(utterance="spa hotel in antalya")
    assert out["path"] == PATH_BROAD
    assert out["retrieval"]["hotels"][0]["hotel_id"] == "rixos_belek"
    assert out["answer"] == "Rixos Belek fits the bill."
    assert compound.calls[0]["region"] == "antalya"
    assert compound.calls[0]["requirements"] == ["spa"]


@pytest.mark.asyncio
async def test_broad_path_with_default_render_uses_hotel_names() -> None:
    compound = _FakeCompound(_ok_retrieval())
    p = _build(
        decompose={
            "query_type": "broad",
            "intent": "recommendation",
            "region": "antalya",
            "requirements": ["spa"],
            "language": "en",
        },
        compound=compound,  # no render_fn -> default fallback
    )
    out = await p.run(utterance="spa hotel in antalya")
    assert "Rixos Belek" in out["answer"]


# ---------- 5. scoped path requires resolved hotel ----------


@pytest.mark.asyncio
async def test_scoped_with_unresolved_hotel_routes_to_hotel_resolve() -> None:
    p = _build(
        decompose={
            "query_type": "scoped",
            "intent": "amenities",
            "hotel_mention": "Rixosta",
            "language": "en",
        }
    )
    out = await p.run(utterance="Rixosta hamam var mı?")
    assert out["path"] == PATH_HOTEL_RESOLVE
    # Returns localised acknowledgement.
    assert out["answer"] is not None


@pytest.mark.asyncio
async def test_scoped_with_session_hotel_runs_kb_path() -> None:
    compound = _FakeCompound(_ok_retrieval())
    store = SessionStore()
    session = await store.load("preset")
    session["session_id"] = "preset"
    session["active_hotel_id"] = "rixos_belek"
    session["active_region"] = "antalya"
    await store.save(session)

    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify({"type": "none", "confidence": 0.0, "signal": None}),
            cache_get=None,
            cache_set=None,
        ),
        # True follow-up: NO hotel_mention this turn, so the router uses the
        # session's active hotel rather than re-resolving.
        decomposer=QueryDecomposer(
            decompose_fn=_scripted_decompose(
                {
                    "query_type": "scoped",
                    "intent": "amenities",
                    "hotel_mention": None,
                    "language": "en",
                    "requirements": ["hamam"],
                }
            )
        ),
        compound=compound,
    )
    out = await p.run(utterance="hamam var mı?", session_id="preset")
    assert out["path"] == PATH_SCOPED
    # Regression: the resolved hotel id lives in session.active_hotel_id on this
    # path (router returns PATH_SCOPED directly, the inline resolver never runs).
    # _run_kb must source it from the session, otherwise the scoped query
    # silently degrades to a generic broad search and returns the wrong hotel.
    assert compound.calls, "discover was never called"
    # (name-detection probes the store first with no hotel_id; the scoped
    # retrieval is the call that carries the hotel_id.)
    kb_calls = [c for c in compound.calls if c.get("hotel_id")]
    assert kb_calls and kb_calls[-1]["hotel_id"] == "rixos_belek"
    # And the post-filter keeps only the resolved hotel.
    assert [h["hotel_id"] for h in out["retrieval"]["hotels"]] == ["rixos_belek"]


@pytest.mark.asyncio
async def test_scoped_empty_requirements_injects_overview_default() -> None:
    # "tell me about X" / "how about X?" — decomposer sometimes yields zero
    # requirements. With a resolved hotel the pipeline must inject a generic
    # overview instead of failing closed with empty_requirements.
    compound = _FakeCompound(_ok_retrieval())
    store = SessionStore()
    session = await store.load("preset2")
    session["session_id"] = "preset2"
    session["active_hotel_id"] = "rixos_belek"
    session["active_region"] = "antalya"
    await store.save(session)

    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify({"type": "none", "confidence": 0.0, "signal": None}),
            cache_get=None,
            cache_set=None,
        ),
        decomposer=QueryDecomposer(
            decompose_fn=_scripted_decompose(
                {
                    "query_type": "scoped",
                    "intent": "amenities",
                    "hotel_mention": None,
                    "language": "en",
                    "requirements": [],  # <-- the failure mode from the live logs
                }
            )
        ),
        compound=compound,
    )
    out = await p.run(utterance="what are the standard rooms?", session_id="preset2")
    assert out["path"] == PATH_SCOPED
    # discover must be called with non-empty (injected) requirements + the hotel id.
    assert compound.calls, "discover was never called"
    kb_calls = [c for c in compound.calls if c.get("hotel_id")]
    assert kb_calls, "no scoped retrieval call was made"
    assert kb_calls[-1]["requirements"], "requirements should have been injected, not empty"
    assert kb_calls[-1]["hotel_id"] == "rixos_belek"
    # And it does NOT fail closed — the hotel comes back.
    assert [h["hotel_id"] for h in out["retrieval"]["hotels"]] == ["rixos_belek"]


@pytest.mark.asyncio
async def test_broad_query_not_hijacked_by_stale_session_hotel() -> None:
    # Session has a hotel active from a prior scoped turn. A fresh BROAD request
    # (no hotel named) must run a broad search and clear the stale hotel — not
    # get hijacked into a scoped lookup of the old hotel.
    compound = _FakeCompound(_ok_retrieval())
    store = SessionStore()
    session = await store.load("preset3")
    session["session_id"] = "preset3"
    session["active_hotel_id"] = "akra_kemer"
    session["active_region"] = "antalya"
    await store.save(session)

    p = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify({"type": "none", "confidence": 0.0, "signal": None}),
            cache_get=None,
            cache_set=None,
        ),
        decomposer=QueryDecomposer(
            decompose_fn=_scripted_decompose(
                {
                    "query_type": "broad",
                    "intent": "recommendation",
                    "hotel_mention": None,
                    "region": "antalya",
                    "requirements": ["spa", "relaxation"],
                    "language": "en",
                }
            )
        ),
        compound=compound,
    )
    out = await p.run(utterance="I want a spa hotel to relax", session_id="preset3")
    assert out["path"] == PATH_BROAD
    # Not scoped: discover called without a hotel filter.
    assert compound.calls and compound.calls[0]["hotel_id"] is None
    # Stale active hotel was cleared.
    reloaded = await store.load("preset3")
    assert reloaded.get("active_hotel_id") is None


# ---------- 6. web / destination / hybrid placeholders ----------


@pytest.mark.asyncio
async def test_web_path_returns_placeholder_when_retriever_missing() -> None:
    p = _build(
        decompose={
            "query_type": "web",
            "intent": "event",
            "city": "Bodrum",
            "language": "en",
        }
    )
    out = await p.run(utterance="festivals in Bodrum next month?")
    assert out["path"] == PATH_WEB
    assert "web" in out["answer"].lower()
    assert out["retrieval"] is None


# ---------- 7. session persistence ----------


@pytest.mark.asyncio
async def test_session_id_returned_and_persists_across_calls() -> None:
    compound = _FakeCompound(_ok_retrieval())
    p = _build(
        decompose={
            "query_type": "broad",
            "intent": "recommendation",
            "region": "antalya",
            "requirements": ["spa"],
            "language": "en",
        },
        compound=compound,
    )
    out = await p.run(utterance="spa hotel in antalya")
    sid = out["session_id"]
    assert sid

    # Second call without explicit region — session.active_region carries over.
    out2 = await p.run(utterance="another one", session_id=sid)
    assert out2["session_id"] == sid
    assert compound.calls[-1]["region"] == "antalya"


@pytest.mark.asyncio
async def test_turn_count_increments_on_full_answer() -> None:
    compound = _FakeCompound(_ok_retrieval())
    p = _build(
        decompose={
            "query_type": "broad",
            "intent": "recommendation",
            "region": "antalya",
            "requirements": ["spa"],
            "language": "en",
        },
        compound=compound,
    )
    out = await p.run(utterance="spa hotel")
    sid = out["session_id"]
    session = await p._sessions.load(sid)  # noqa: SLF001 — test-only access
    assert session["turn_count"] == 1
    assert session["clarification_count"] == 0


# ---------- 8. response contract ----------


@pytest.mark.asyncio
async def test_response_contract_shape() -> None:
    compound = _FakeCompound(_ok_retrieval())
    out = await _build(compound=compound).run(utterance="spa hotel in antalya")
    required = {
        "session_id",
        "utterance",
        "path",
        "reason",
        "escalation",
        "clarification",
        "decomposition",
        "router",
        "retrieval",
        "answer",
        "timings",
    }
    assert required.issubset(out.keys())
    assert "total_ms" in out["timings"]


# ---------- 9. clarification follow-up merges prior utterance ----------


class _RecordingDecomposer:
    """Captures every utterance fed to decompose so the test can assert merge behaviour."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.seen: list[str] = []

    async def __call__(self, utterance: str, _ctx: dict[str, Any]) -> dict[str, Any]:
        self.seen.append(utterance)
        return self._payloads[len(self.seen) - 1]


@pytest.mark.asyncio
async def test_followup_after_clarification_merges_prior_utterance() -> None:
    """When triage asked for geography, the next turn must be combined with
    the original question before re-decomposing — otherwise the decomposer
    sees a context-free fragment like "In Antalya" and returns garbage."""
    rec = _RecordingDecomposer(
        [
            # turn 1: ambiguous broad query, no region → triage will ask
            {
                "query_type": "broad",
                "intent": "recommendation",
                "requirements": ["spa"],
                "language": "en",
            },
            # turn 2: must see the merged "I want a hotel with a spa\nFollow-up: In Antalya"
            {
                "query_type": "broad",
                "intent": "recommendation",
                "region": "antalya",
                "requirements": ["spa"],
                "language": "en",
            },
        ]
    )
    compound = _FakeCompound(_ok_retrieval())
    p = ConciergePipeline(
        session_store=SessionStore(),
        classifier=EscalationClassifier(
            classify_fn=_scripted_classify({"type": "none", "confidence": 0.0, "signal": None}),
            cache_get=None,
            cache_set=None,
        ),
        decomposer=QueryDecomposer(decompose_fn=rec),
        compound=compound,
    )
    out1 = await p.run(utterance="I want a hotel with a spa")
    assert out1["path"] == "clarify"

    out2 = await p.run(utterance="In Antalya", session_id=out1["session_id"])
    assert out2["path"] == PATH_BROAD
    # second decompose call saw the merged utterance
    assert "spa" in rec.seen[1].lower()
    assert "antalya" in rec.seen[1].lower()


# ---------- 10. render fails closed on empty retrieval ----------


@pytest.mark.asyncio
async def test_render_fails_closed_when_retrieval_returns_no_hotels() -> None:
    """If CompoundAndDiscovery returns 0 hotels, the LLM must NOT be called —
    it tends to invent geography. Pipeline returns deterministic copy."""
    compound = _FakeCompound(
        {"reason": "no_match_above_threshold", "missing_requirements": ["spa"], "hotels": []}
    )
    called = {"n": 0}

    async def render(_payload: dict[str, Any]) -> str:
        called["n"] += 1
        return "LLM SHOULD NOT BE CALLED"

    p = _build(
        decompose={
            "query_type": "broad",
            "intent": "recommendation",
            "region": "antalya",
            "requirements": ["spa"],
            "language": "en",
        },
        compound=compound,
        render_fn=render,
    )
    out = await p.run(utterance="spa hotel in antalya")
    assert out["path"] == PATH_BROAD
    assert called["n"] == 0
    assert "antalya" in out["answer"].lower()
    # Fail-closed: the deterministic no-match reply admits the gap rather than
    # inventing hotels. Assert intent, not exact copy (the wording is tuned
    # for the concierge voice and may evolve).
    assert "don't have" in out["answer"].lower() or "couldn't find" in out["answer"].lower()
