#!/usr/bin/env python3
"""Tier-3 retrieval-quality eval — does the LIVE concierge return the RIGHT hotels?

Tier 1 proves the routing/state machinery; Tier 2 grades the decomposer's
structured output. Neither touches the actual KB. Tier 3 runs whole queries
through the **live** ConciergePipeline (real Qdrant + reranker + Claude render,
exactly as `demo-hotel/serve.py` wires it) and reports, per query:

  - the hotels returned (id · name · score) and the retrieval `reason`
  - the rendered answer + stage timings
  - PASS/FAIL on hotel-recall when the case lists `expect_hotels`
  - flags when the answer mentions a hotel NOT in the retrieved set
    (a cheap grounding smell-test)

Most cases won't have ground-truth `expect_hotels` until you (who know the KB)
fill them in — so by default this is a **retrieval report** you eyeball, with
automatic grading layered on wherever expectations are provided.

Usage (repo root, venv; needs Qdrant/ES/Redis reachable + ANTHROPIC_API_KEY):
    python scripts/eval_retrieval.py
    python scripts/eval_retrieval.py --out output/retrieval_report.txt
    python scripts/eval_retrieval.py --selftest        # offline grader check

Companion: docs/call-center/conversation-eval.md (Tier 3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

DEFAULT_CASES = (
    Path(__file__).resolve().parents[1] / "tests/call_center/eval_data/retrieval_cases.jsonl"
)


# ----------------------------- grading (pure) -----------------------------


def grade(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Grade one live result against a case. Only checks what the case specifies."""
    retrieval = result.get("retrieval") or {}
    hotels = retrieval.get("hotels") or []
    got_ids = [h.get("hotel_id") for h in hotels]
    got_names = [((h.get("payload") or {}).get("hotel_name") or "") for h in hotels]
    answer = result.get("answer") or ""

    out: dict[str, Any] = {"got_ids": got_ids, "checks": []}

    expect = case.get("expect_hotels")
    if expect:
        # Recall: each expected hotel id appears somewhere in the returned set.
        present = [e for e in expect if e in got_ids]
        ok = len(present) == len(expect)
        out["checks"].append(
            ("recall", ok, f"{len(present)}/{len(expect)} expected hotels returned")
        )

    if case.get("expect_top") is not None:
        top_ok = bool(got_ids) and got_ids[0] == case["expect_top"]
        out["checks"].append(("top_match", top_ok, f"top={got_ids[0] if got_ids else None}"))

    if case.get("expect_no_match"):
        nm_ok = len(got_ids) == 0
        out["checks"].append(("no_match", nm_ok, f"returned {len(got_ids)} hotels"))

    # Grounding smell-test: does the answer name a hotel that wasn't retrieved?
    if got_names:
        # crude: flag if a known returned-name is absent but answer is long & specific.
        named_in_answer = [n for n in got_names if n and n.split()[0].lower() in answer.lower()]
        out["checks"].append(
            (
                "grounding_smell",
                True,
                f"{len(named_in_answer)}/{len(got_names)} returned hotels referenced in answer",
            )
        )

    out["passed"] = all(ok for _, ok, _ in out["checks"]) if out["checks"] else None
    return out


def _selftest() -> int:
    case = {"id": "x", "utterance": "spa hotel", "expect_hotels": ["crystal_tat_beach"]}
    res = {
        "retrieval": {
            "hotels": [{"hotel_id": "crystal_tat_beach", "payload": {"hotel_name": "Crystal Tat"}}]
        },
        "answer": "Crystal Tat is great.",
    }
    g = grade(case, res)
    assert g["passed"] is True, g
    res2 = {
        "retrieval": {"hotels": [{"hotel_id": "akra_kemer", "payload": {"hotel_name": "Akra"}}]},
        "answer": "Akra.",
    }
    g2 = grade(case, res2)
    assert g2["passed"] is False, g2
    g3 = grade(
        {"id": "y", "utterance": "z", "expect_no_match": True},
        {"retrieval": {"hotels": []}, "answer": "no"},
    )
    assert g3["passed"] is True, g3
    print("selftest OK — retrieval grader behaves as expected")
    return 0


# ----------------------------- live runner -----------------------------


def _load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    import aiohttp

    from voxtera.call_center.classifier import EscalationClassifier
    from voxtera.call_center.compound import CompoundAndDiscovery
    from voxtera.call_center.concierge import _build_anthropic_render
    from voxtera.call_center.decompose import QueryDecomposer
    from voxtera.call_center.pipeline import ConciergePipeline
    from voxtera.call_center.router import SourceRouter
    from voxtera.call_center.session import SessionStore
    from voxtera.call_center.triage import Triage

    async with aiohttp.ClientSession() as http:
        pipeline = ConciergePipeline(
            session_store=SessionStore(),
            classifier=EscalationClassifier(),
            decomposer=QueryDecomposer(),
            triage=Triage(),
            router=SourceRouter(),
            compound=CompoundAndDiscovery(session=http),
            render_fn=_build_anthropic_render("claude-haiku-4-5-20251001"),
        )
        sid = case.get("session_id")  # share to test follow-ups; None = fresh
        return await pipeline.run(
            utterance=case["utterance"],
            session_id=sid,
            region=case.get("region"),
        )


def _fmt_report(case: dict[str, Any], result: dict[str, Any], g: dict[str, Any]) -> str:
    d = result.get("decomposition") or {}
    r = result.get("retrieval") or {}
    t = result.get("timings") or {}
    path_line = (
        f"  path={result.get('path')}  qtype={d.get('query_type')}  "
        f"reason={r.get('reason')}  count={r.get('count', len(r.get('hotels', [])))}"
    )
    timing_line = (
        f"  timings: decompose={t.get('decompose_ms')} "
        f"retrieve={t.get('retrieve_ms')} render={t.get('render_ms')} "
        f"total={t.get('total_ms')}"
    )
    lines = [
        "=" * 80,
        f"[{case['id']}] {case['utterance']!r}"
        + (f"  region={case.get('region')}" if case.get("region") else ""),
        path_line,
        "  hotels: "
        + (
            ", ".join(
                f"{h.get('hotel_id')}({round(float(h.get('score', 0)), 3)})"
                for h in (r.get("hotels") or [])
            )
            or "—"
        ),
        f"  answer: {(result.get('answer') or '')[:200]!r}",
        timing_line,
    ]
    if g["checks"]:
        verdict = "PASS" if g["passed"] else ("FAIL" if g["passed"] is False else "—")
        lines.append(
            f"  GRADE [{verdict}]: "
            + "; ".join(f"{n}={'ok' if ok else 'X'} ({msg})" for n, ok, msg in g["checks"])
        )
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--out", default=None, help="also write the report to this file")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    cases = _load_cases(Path(args.cases))
    report: list[str] = [f"Tier-3 retrieval eval — {len(cases)} cases\n"]
    graded = passed = 0
    for case in cases:
        try:
            result = await _run_case(case)
        except Exception as e:  # noqa: BLE001
            report.append(f"[{case['id']}] ERROR: {e}")
            continue
        g = grade(case, result)
        if g["passed"] is not None:
            graded += 1
            passed += 1 if g["passed"] else 0
        block = _fmt_report(case, result, g)
        report.append(block)
        print(block)

    summary = f"\n{'=' * 80}\nGRADED: {passed}/{graded} passed" + (
        f"  ({len(cases) - graded} report-only)" if len(cases) - graded else ""
    )
    report.append(summary)
    print(summary)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(report), encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
