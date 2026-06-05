#!/usr/bin/env python3
"""A/B the query decomposer across models — latency + field agreement.

Runs the SAME utterances through two decompose models (default: Claude
Haiku 4.5 vs gpt-4.1-nano) and reports, per utterance:

  - median latency over N runs (warm — first run per model is discarded)
  - output_tokens + finish/stop reason (from the §3.5 usage logging)
  - the extracted fields that actually drive routing/retrieval, side by
    side, so you can see if the cheaper model drifts on region /
    requirements / query_type / intent.

Only the model changes — same system prompt, same context block — so any
difference is the model, not the harness.

Usage (from repo root, with the venv that has anthropic + openai):

    python scripts/ab_decompose.py
    python scripts/ab_decompose.py --runs 5
    python scripts/ab_decompose.py --models claude-haiku-4-5-20251001,gpt-4.1-nano

Requires ANTHROPIC_API_KEY and OPENAI_API_KEY (loaded from .env).
Costs a handful of Anthropic + OpenAI tokens per run.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001
    pass

from voxtera.call_center.decompose import QueryDecomposer

# (utterance, context) — context mimics carry-over from prior turns.
DEFAULT_CASES: list[tuple[str, dict[str, Any]]] = [
    ("hotel with spa and kids club", {}),
    ("I want to go to a hotel with spa to relax, what u can recommend  ?", {}),
    ("what u can tell me about Crystal Tat Beach Pearl Collection", {}),
    (
        "what are Standard Rooms ?",
        {"active_hotel_id": "crystal_tat_beach", "active_region": "antalya"},
    ),
    ("Antalya'da spa ve çocuk kulübü olan bir otel arıyorum", {}),
]

# Fields that actually drive routing + retrieval — what we compare.
DIFF_FIELDS = [
    "query_type",
    "intent",
    "hotel_mention",
    "city",
    "region",
    "requirements",
    "requirements_logic",
    "language",
]


async def _time_one(model: str, utterance: str, ctx: dict[str, Any], runs: int) -> dict[str, Any]:
    """Run a single utterance `runs+1` times; discard the first (cold)."""
    decomposer = QueryDecomposer(model=model)
    latencies: list[float] = []
    last: dict[str, Any] = {}
    for i in range(runs + 1):
        t0 = time.perf_counter()
        out = await decomposer.decompose(utterance, ctx)
        dt = (time.perf_counter() - t0) * 1000.0
        if i > 0:  # discard cold run
            latencies.append(dt)
        last = out
    usage = last.get("usage") or {}
    return {
        "median_ms": round(statistics.median(latencies), 0) if latencies else 0.0,
        "min_ms": round(min(latencies), 0) if latencies else 0.0,
        "output_tokens": usage.get("output_tokens"),
        "stop": last.get("stop_reason"),
        "fields": {k: last.get(k) for k in DIFF_FIELDS},
    }


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="timed runs per model (cold run discarded)")
    ap.add_argument(
        "--models",
        default="claude-haiku-4-5-20251001,gpt-4.1-nano",
        help="comma-separated model strings (A,B)",
    )
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if len(models) != 2:
        print("Need exactly two models (A,B).", file=sys.stderr)
        return 2

    a, b = models
    print(f"\nA = {a}\nB = {b}\nruns = {args.runs} (cold run discarded)\n")

    agg = {a: [], b: []}
    for utterance, ctx in DEFAULT_CASES:
        print("=" * 78)
        print(f"UTTERANCE: {utterance!r}" + (f"   ctx={ctx}" if ctx else ""))
        res = {}
        for m in (a, b):
            try:
                res[m] = await _time_one(m, utterance, ctx, args.runs)
            except Exception as e:  # noqa: BLE001
                print(f"  [{m}] ERROR: {e}")
                res[m] = None
        if not res[a] or not res[b]:
            continue

        ra, rb = res[a], res[b]
        agg[a].append(ra["median_ms"])
        agg[b].append(rb["median_ms"])
        speedup = (ra["median_ms"] / rb["median_ms"]) if rb["median_ms"] else 0.0
        print(
            f"  latency  A {ra['median_ms']:.0f} ms (min {ra['min_ms']:.0f})   "
            f"B {rb['median_ms']:.0f} ms (min {rb['min_ms']:.0f})   "
            f"→ B is {speedup:.2f}x"
        )
        print(
            f"  tokens   A out={ra['output_tokens']} stop={ra['stop']}   "
            f"B out={rb['output_tokens']} stop={rb['stop']}"
        )
        print("  fields   (✓ = same, ✗ = differs)")
        for f in DIFF_FIELDS:
            va, vb = ra["fields"][f], rb["fields"][f]
            mark = "✓" if va == vb else "✗"
            if va == vb:
                print(f"    {mark} {f:20s} {_fmt(va)}")
            else:
                print(f"    {mark} {f:20s} A={_fmt(va)!s:40s} B={_fmt(vb)}")
        print()

    print("=" * 78)
    if agg[a] and agg[b]:
        ma, mb = statistics.median(agg[a]), statistics.median(agg[b])
        print(
            f"OVERALL median latency:  A={ma:.0f} ms   B={mb:.0f} ms   "
            f"→ B is {ma/mb:.2f}x faster"
            if mb
            else ""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
