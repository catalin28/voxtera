#!/usr/bin/env python3
"""Tier-2 decomposer eval — grade the LIVE decomposer against labelled cases.

Tier 1 (tests/call_center/test_conversation_flows.py) proves the machinery
around the decomposer is correct by *scripting* the decomposition. Tier 2
removes the script: it runs the real QueryDecomposer (live LLM) over a labelled
set of utterances and measures how often the model produces the right
structured output — the layer where the remaining failures live (e.g. a
follow-up classified as "broad" instead of "scoped").

It grades the fields that drive routing/retrieval:
  query_type · intent · region · hotel_named · language · requirements

and reports per-field accuracy, query_type stability across repeated runs
(non-determinism), and the specific failing cases. Pass two models to compare
(e.g. Haiku vs nano) and decide with numbers instead of anecdotes.

Usage (repo root, venv with anthropic + openai):
    python scripts/eval_decompose.py
    python scripts/eval_decompose.py --runs 3 --models claude-haiku-4-5-20251001,gpt-4.1-nano
    python scripts/eval_decompose.py --selftest      # offline check of the scorer

Needs ANTHROPIC_API_KEY / OPENAI_API_KEY (from .env). Costs tokens.
Companion spec: docs/call-center/conversation-eval.md (§7 Tier 2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_CASES = (
    Path(__file__).resolve().parents[1] / "tests/call_center/eval_data/decompose_cases.jsonl"
)
GRADED_FIELDS = ["query_type", "intent", "region", "hotel_named", "language", "requires"]


# ----------------------------- scoring (pure) -----------------------------


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _as_set(v: Any) -> set[str]:
    if v is None:
        return set()
    if isinstance(v, list | tuple | set):
        return {_norm(x) for x in v}
    return {_norm(v)}


def score_region(expected: Any, got: dict[str, Any]) -> float:
    region = _norm(got.get("region"))
    city = _norm(got.get("city"))
    if expected is None:
        # Expected no geography -> correct iff the model also found none.
        return 1.0 if not region and not city else 0.0
    exp = _norm(expected)
    return 1.0 if exp in (region, city) or exp in region or exp in city else 0.0


def score_requires(concepts: list[str], got: dict[str, Any]) -> float:
    reqs = got.get("requirements") or []
    joined = " ".join(_norm(r) for r in reqs)
    if not concepts:
        return 1.0
    hit = 0
    for c in concepts:
        cl = _norm(c)
        if cl in joined or all(tok in joined for tok in cl.split()):
            hit += 1
    return hit / len(concepts)


def score_run(expect: dict[str, Any], got: dict[str, Any]) -> dict[str, float]:
    """Return {field: score in [0,1]} for every field the case specifies."""
    out: dict[str, float] = {}
    if "query_type" in expect:
        out["query_type"] = (
            1.0 if _norm(got.get("query_type")) in _as_set(expect["query_type"]) else 0.0
        )
    if "intent" in expect:
        out["intent"] = 1.0 if _norm(got.get("intent")) in _as_set(expect["intent"]) else 0.0
    if "region" in expect:
        out["region"] = score_region(expect["region"], got)
    if "hotel_named" in expect:
        named = bool(_norm(got.get("hotel_mention")))
        out["hotel_named"] = 1.0 if named == bool(expect["hotel_named"]) else 0.0
    if "language" in expect:
        out["language"] = 1.0 if _norm(got.get("language")) == _norm(expect["language"]) else 0.0
    if "requires" in expect:
        out["requires"] = score_requires(expect["requires"], got)
    return out


# ----------------------------- selftest -----------------------------


def _selftest() -> int:
    """Offline sanity check of the scorer (no network)."""
    exp = {
        "query_type": "scoped",
        "intent": ["food", "amenities"],
        "region": "antalya",
        "hotel_named": False,
        "requires": ["bars", "restaurants"],
    }
    perfect = {
        "query_type": "scoped",
        "intent": "food",
        "region": "antalya",
        "city": None,
        "hotel_mention": None,
        "requirements": ["bars", "restaurants"],
    }
    s = score_run(exp, perfect)
    assert s == {
        "query_type": 1.0,
        "intent": 1.0,
        "region": 1.0,
        "hotel_named": 1.0,
        "requires": 1.0,
    }, s

    wrong = {
        "query_type": "broad",
        "intent": "policy",
        "region": "paris",
        "city": None,
        "hotel_mention": "akra_kemer",
        "requirements": ["spa"],
    }
    s = score_run(exp, wrong)
    assert s == {
        "query_type": 0.0,
        "intent": 0.0,
        "region": 0.0,
        "hotel_named": 0.0,
        "requires": 0.0,
    }, s

    # partial requires + accept-list query_type + None-region
    s = score_run(
        {"query_type": ["broad", "compound"], "region": None, "requires": ["spa", "kids club"]},
        {"query_type": "compound", "region": None, "city": None, "requirements": ["spa"]},
    )
    assert s["query_type"] == 1.0 and s["region"] == 1.0 and s["requires"] == 0.5, s

    # region matches via city; hotel_named true
    s = score_run(
        {"region": "antalya", "hotel_named": True},
        {"region": None, "city": "Antalya", "hotel_mention": "Crystal Tat"},
    )
    assert s["region"] == 1.0 and s["hotel_named"] == 1.0, s
    print("selftest OK — scorer behaves as expected")
    return 0


# ----------------------------- live runner -----------------------------


def _load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


async def _run_model(model: str, cases: list[dict[str, Any]], runs: int) -> dict[str, Any]:
    from voxtera.call_center.decompose import QueryDecomposer

    decomposer = QueryDecomposer(model=model)

    field_scores: dict[str, list[float]] = {f: [] for f in GRADED_FIELDS}
    qtype_stability: list[float] = []
    failures: list[str] = []

    for case in cases:
        utt, ctx, expect = case["utterance"], case.get("context") or {}, case["expect"]
        per_run_scores: list[dict[str, float]] = []
        qtypes: list[str] = []
        for _ in range(runs):
            try:
                got = await decomposer.decompose(utt, ctx)
            except Exception as e:  # noqa: BLE001
                got = {"_error": str(e)}
            per_run_scores.append(score_run(expect, got))
            qtypes.append(_norm(got.get("query_type")))

        for f in GRADED_FIELDS:
            vals = [s[f] for s in per_run_scores if f in s]
            if vals:
                field_scores[f].append(statistics.mean(vals))

        if "query_type" in expect:
            modal, n = Counter(qtypes).most_common(1)[0]
            qtype_stability.append(n / len(qtypes))
            if statistics.mean([s.get("query_type", 0.0) for s in per_run_scores]) < 0.5:
                failures.append(
                    f"  [{case['id']}] {utt!r}  expected query_type={expect['query_type']} "
                    f"got {Counter(qtypes).most_common()}"
                )

    summary = {f: (round(statistics.mean(v), 3), len(v)) for f, v in field_scores.items() if v}
    return {
        "model": model,
        "summary": summary,
        "qtype_stability": round(statistics.mean(qtype_stability), 3) if qtype_stability else None,
        "failures": failures,
    }


def _print_report(res: dict[str, Any]) -> None:
    print(f"\n=== {res['model']} ===")
    print(f"  {'field':12s} {'accuracy':>9s}  cases")
    for f in GRADED_FIELDS:
        if f in res["summary"]:
            acc, n = res["summary"][f]
            print(f"  {f:12s} {acc:9.3f}  {n}")
    if res["qtype_stability"] is not None:
        print(f"  {'qtype_stab':12s} {res['qtype_stability']:9.3f}  (run-to-run agreement)")
    if res["failures"]:
        print(f"  query_type failures ({len(res['failures'])}):")
        for line in res["failures"]:
            print(line)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--models",
        default="claude-haiku-4-5-20251001",
        help="comma-separated model strings to grade/compare",
    )
    ap.add_argument("--runs", type=int, default=3, help="repeats per case (non-determinism)")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--selftest", action="store_true", help="offline scorer check, no network")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    cases = _load_cases(Path(args.cases))
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    print(f"cases={len(cases)}  runs={args.runs}  models={models}")

    results = []
    for m in models:
        results.append(await _run_model(m, cases, args.runs))
        _print_report(results[-1])

    if len(results) == 2:
        a, b = results
        print(f"\n=== {a['model']}  vs  {b['model']} ===")
        print(f"  {'field':12s} {'A':>8s} {'B':>8s} {'Δ(B-A)':>8s}")
        for f in GRADED_FIELDS:
            if f in a["summary"] and f in b["summary"]:
                av, bv = a["summary"][f][0], b["summary"][f][0]
                print(f"  {f:12s} {av:8.3f} {bv:8.3f} {bv-av:+8.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
