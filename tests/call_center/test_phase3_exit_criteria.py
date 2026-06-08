"""Phase 3 exit-criteria evaluation against the live pipeline.

Spec (docs/call-center/Voxtera_RAG_Development_Plan.md §Phase 3 Exit Criteria):

    "Given 30 test queries across all types: decomposition correctly identifies
     query_type and source_required on ≥ 90%. Triage asks the right question
     or passes through correctly on ≥ 90%. Escalation triggers fire on 100%
     of escalation cases. Two-turn maximum enforced."

This module is marked ``live_eval`` and skipped by default (see pyproject
``addopts = "-v -m 'not live_eval'"``). Run explicitly with:

    pytest tests/call_center/test_phase3_exit_criteria.py -m live_eval -s

Requires OPENAI_API_KEY (classifier) and ANTHROPIC_API_KEY (decomposer)
in the environment.

The evaluator scores each fixture row independently and reports per-metric
hit rates plus a verdict table. A failed metric does not fail individual
asserts beyond the final threshold checks, so the full report is always
visible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.pipeline import ConciergePipeline
from voxtera.call_center.router import (
    PATH_BROAD,
    PATH_DESTINATION,
    PATH_ESCALATE,
    PATH_HYBRID,
    PATH_SCOPED,
    PATH_WEB,
    SourceRouter,
)
from voxtera.call_center.session import SessionStore, new_session_id
from voxtera.call_center.triage import Triage

pytestmark = pytest.mark.live_eval

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase3_exit_criteria.json"

# Map expected query_type -> the router path we expect the pipeline to choose.
_PATH_FOR_QUERY_TYPE = {
    "scoped": PATH_SCOPED,
    "broad": PATH_BROAD,
    "compound": PATH_BROAD,    # compound flows through broad Qdrant
    "comparison": PATH_BROAD,  # comparison flows through broad Qdrant
    "destination": PATH_DESTINATION,
    "web": PATH_WEB,
    "hybrid": PATH_HYBRID,
    "escalate": PATH_ESCALATE,
}


def _load_fixture() -> list[dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _require_keys() -> None:
    missing = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
    if missing:
        pytest.skip(f"Live eval skipped — missing env: {', '.join(missing)}")


def _seed_session(store: SessionStore, sid: str, seed: dict[str, Any]) -> None:
    """Pre-populate a session row so the pipeline sees realistic context."""
    if not seed:
        return
    # Touch the store synchronously via its in-memory fallback so the
    # pipeline's subsequent load() picks up our seeded state.
    import asyncio
    async def _do() -> None:
        s = await store.load(sid)
        s["session_id"] = sid
        for k, v in seed.items():
            s[k] = v
        await store.save(s)
    asyncio.get_event_loop().run_until_complete(_do())


@pytest.mark.asyncio
async def test_phase3_exit_criteria() -> None:
    _require_keys()
    fixture = _load_fixture()

    store = SessionStore()
    pipeline = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(),
        decomposer=QueryDecomposer(),
        triage=Triage(),
        router=SourceRouter(),
        compound=None,       # eval scores routing/triage, not retrieval quality
        render_fn=None,
    )

    rows: list[dict[str, Any]] = []
    qt_hits = 0
    src_hits = 0
    triage_hits = 0
    esc_hits = 0
    esc_total = 0

    for case in fixture:
        exp = case["expected"]
        sid = new_session_id()
        if case.get("session_seed"):
            _seed_session(store, sid, case["session_seed"])

        out = await pipeline.run(
            utterance=case["utterance"],
            session_id=sid,
            region=(case.get("session_seed") or {}).get("active_region"),
        )

        decomp = out.get("decomposition") or {}
        got_qt = decomp.get("query_type") or out.get("path")
        got_src = set(decomp.get("source_required") or [])
        exp_src = set(exp.get("source_required") or [])
        triage_asked = out.get("path") == "clarify"
        got_slot = (out.get("clarification") or {}).get("slot")
        is_escalated = (out.get("escalation") or {}).get("escalate", False)
        got_esc_type = (out.get("escalation") or {}).get("escalation_type")

        # ---- scoring ----
        # Escalation cases bypass decompose, so query_type/source come from path.
        if exp.get("escalation"):
            esc_total += 1
            qt_ok = (out.get("path") == PATH_ESCALATE)
            src_ok = True  # n/a for escalation
            triage_ok = (not triage_asked)  # MUST NOT clarify on escalations
            esc_ok = is_escalated and (got_esc_type == exp.get("escalation_type"))
            if esc_ok:
                esc_hits += 1
        else:
            qt_ok = (got_qt == exp["query_type"])
            src_ok = exp_src.issubset(got_src) if exp_src else True
            if exp.get("triage_ask"):
                triage_ok = triage_asked and (got_slot == exp.get("triage_slot"))
            else:
                triage_ok = (not triage_asked)
            esc_ok = (not is_escalated)

        if qt_ok:
            qt_hits += 1
        if src_ok:
            src_hits += 1
        if triage_ok:
            triage_hits += 1

        rows.append({
            "id": case["id"],
            "utt": case["utterance"][:60],
            "exp_qt": exp.get("query_type"),
            "got_qt": got_qt,
            "qt_ok": qt_ok,
            "src_ok": src_ok,
            "triage_ok": triage_ok,
            "esc_ok": esc_ok,
        })

    total = len(fixture)
    qt_rate = qt_hits / total
    src_rate = src_hits / total
    triage_rate = triage_hits / total
    esc_rate = (esc_hits / esc_total) if esc_total else 1.0

    # Pretty report (printed via -s).
    print("\n\n=== Phase 3 Exit-Criteria Report ===")
    print(f"Total cases: {total}  (escalation cases: {esc_total})")
    print(f"query_type      : {qt_hits}/{total} = {qt_rate:.0%}  (threshold ≥ 90%)")
    print(f"source_required : {src_hits}/{total} = {src_rate:.0%}  (threshold ≥ 90%)")
    print(f"triage decision : {triage_hits}/{total} = {triage_rate:.0%}  (threshold ≥ 90%)")
    print(f"escalation fire : {esc_hits}/{esc_total} = {esc_rate:.0%}  (threshold == 100%)")
    print("\nFailures:")
    for r in rows:
        if not (r["qt_ok"] and r["src_ok"] and r["triage_ok"] and r["esc_ok"]):
            print(
                f"  #{r['id']:>2}  qt={r['qt_ok']!s:>5}  src={r['src_ok']!s:>5}"
                f"  triage={r['triage_ok']!s:>5}  esc={r['esc_ok']!s:>5}"
                f"  exp={r['exp_qt']!s:<11} got={r['got_qt']!s:<11} | {r['utt']}"
            )

    # ---- thresholds ----
    assert qt_rate >= 0.90, f"query_type accuracy {qt_rate:.0%} below 90%"
    assert src_rate >= 0.90, f"source_required accuracy {src_rate:.0%} below 90%"
    assert triage_rate >= 0.90, f"triage accuracy {triage_rate:.0%} below 90%"
    assert esc_rate == 1.0, f"escalation fire rate {esc_rate:.0%} not 100%"


@pytest.mark.asyncio
async def test_two_turn_clarification_maximum() -> None:
    """Triage must enforce a two-turn maximum (no third consecutive clarification)."""
    _require_keys()

    store = SessionStore()
    pipeline = ConciergePipeline(
        session_store=store,
        classifier=EscalationClassifier(),
        decomposer=QueryDecomposer(),
        triage=Triage(),
        router=SourceRouter(),
        compound=None,
        render_fn=None,
    )

    sid = new_session_id()
    # Three consecutive geography-less broad queries.
    paths: list[str] = []
    for _ in range(3):
        out = await pipeline.run(utterance="recommend a hotel with a spa", session_id=sid)
        paths.append(out["path"])

    # First two may clarify; the third MUST NOT.
    assert paths.count("clarify") <= 2, (
        f"two-turn max violated: paths={paths}"
    )
    assert paths[-1] != "clarify", f"third consecutive turn still clarifying: {paths}"
