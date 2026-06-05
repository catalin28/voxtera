"""Live-Qdrant smoke harness for CompoundAndDiscovery (Phase 2c).

Hits the real `hotel_kb` Qdrant collection via the production
`CompoundAndDiscovery` end-to-end (real `multilingual-e5-large`
embeddings, real BroadHotelDiscovery fan-out, real relative-margin
filter at the per-requirement level).

KNOWN LIMITATION (documented in phase2c-test-report.md):
With absolute floor DEFAULT_MIN_SCORE=0.70, e5-large surfaces low-score
"junk" hits (0.74-0.79) even for genuinely nonsensical queries. This
means a compound intersection across two nonsense requirements does NOT
collapse to `no_match_above_threshold` against the live corpus the way
it does in the mock harness (token-overlap scorer = true zero). The
mock smoke (smoke_compound_discovery.py) is the canonical verification
of partial_match / no_match paths; this live smoke verifies wire-up,
fan-out, and happy-path intersections only.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_compound_discovery_live.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from voxtera.call_center.compound import CompoundAndDiscovery

REGION = "Turkish Riviera"

# (label, region, requirements, expected_reason, expected_missing_subset)
SCENARIOS = [
    ("strict: spa+pool",
     REGION, ["luxury spa wellness", "outdoor pool aquapark"], None, set()),
    ("strict: kids+diving",
     REGION, ["kids club children programs", "scuba diving snorkel"], None, set()),
    ("strict: 3-way beach+spa+restaurant",
     REGION, ["beach front sea view", "spa massage wellness", "restaurant dinner buffet"],
     None, set()),
    ("info-only: nonsense reqs (known e5 junk-overlap)",
     REGION, ["xyzzy plugh", "grue zorkmid"], None, set()),
    ("empty region",
     "  ", ["spa"], "no_region_scope", set()),
    ("empty requirements",
     REGION, ["  ", ""], "empty_requirements", set()),
]


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if "QDRANT_URL" not in os.environ:
        print("ERROR: QDRANT_URL not in env (check .env)")
        return 2

    print(f"Target Qdrant: {os.environ['QDRANT_URL']}")
    print("Loading multilingual-e5-large (first run downloads weights)...\n")

    async with aiohttp.ClientSession() as session:
        compound = CompoundAndDiscovery(session=session)

        header = f"{'Scenario':<36}{'Got':<5}{'Top':<8}{'Reason':<28}{'Missing':<24}{'Verdict'}"
        print(header)
        print("-" * len(header))

        passed = failed = 0
        for label, region, reqs, expected_reason, expected_missing in SCENARIOS:
            result = await compound.discover(region=region.strip(), requirements=reqs)
            count = result["count"]
            top = result["top_score"]
            reason = result["reason"]
            missing = result["missing_requirements"]

            ok_reason = (reason == expected_reason)
            ok_missing = expected_missing.issubset(set(missing))
            if expected_reason is None or expected_reason == "partial_match_only":
                ok_count = count >= 1
            else:
                ok_count = count == 0
            ok = ok_reason and ok_missing and ok_count
            verdict = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            missing_str = (",".join(missing)[:22]) or "-"
            print(f"{label:<36}{count:<5}{top:<8.3f}{str(reason):<28}{missing_str:<24}{verdict}")

        print("-" * len(header))
        print(f"\nResults: {passed} passed, {failed} failed")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
