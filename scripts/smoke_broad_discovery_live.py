"""Live-Qdrant smoke harness for BroadHotelDiscovery (Phase 2b follow-up).

Hits the real `hotel_kb` Qdrant collection via the production
`BroadHotelDiscovery` end-to-end (real `multilingual-e5-large`
embeddings). No DI overrides.

NOTE on region casing: the ingested payload stores region as
"Turkish Riviera" (mixed case). The discovery filter passes the value
verbatim into Qdrant must-match, so scenarios use the exact stored value.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_broad_discovery_live.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from voxtera.call_center.discovery import BroadHotelDiscovery


REGION = "Turkish Riviera"

# (label, region, query, tags, category_hint, expected_reason, extra_check)
SCENARIOS = [
    ("region happy path",            REGION, "luxury hotel spa wellness",          None,        None,            None,                       None),
    ("activity_tags narrows",        REGION, "diving snorkel scuba",               ["diving"],  None,            None,                       "tags_include_diving"),
    ("empty region",                 "  ",   "anything",                           None,        None,            "no_region_scope",          None),
    ("empty query",                  REGION, "   ",                                None,        None,            "empty_query",              None),
    # E5-large compressed score range: nonsense queries still surface weak matches above 0.70.
    # See kb_config.DEFAULT_MIN_SCORE comment.
    ("junk query (E5 floor ~0.77)",  REGION, "xyzzy plugh zorkmid grue",           None,        None,            None,                       None),
    ("dedup aggregation",            REGION, "water park aquapark slides",         None,        None,            None,                       "no_dup_hotels"),
    ("region scope respected",       REGION, "beach sea sunset",                   None,        None,            None,                       "region_all_in_scope"),
    ("category_hint food_beverage",  REGION, "buffet restaurant dinner",           None,        "food_beverage", None,                       "evidence_in_fb_or_overview"),
]


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if "QDRANT_URL" not in os.environ:
        print("ERROR: QDRANT_URL not in env (check .env)")
        return 2

    print(f"Target Qdrant: {os.environ['QDRANT_URL']}")
    print("Loading multilingual-e5-large (first run downloads weights)...\n")

    async with aiohttp.ClientSession() as session:
        discovery = BroadHotelDiscovery(session=session)

        header = f"{'Scenario':<32}{'Got':<5}{'Top':<8}{'Reason':<28}{'Verdict'}"
        print(header)
        print("-" * len(header))

        passed = failed = 0
        top_scores: list[tuple[str, float]] = []
        for label, region, query, tags, hint, expected_reason, extra in SCENARIOS:
            result = await discovery.discover(
                region=region.strip(), query=query,
                activity_tags=tags, category_hint=hint,
            )
            count = result["count"]
            top = result["top_score"]
            reason = result["reason"]

            ok_reason = (reason == expected_reason)
            ok_extra = True
            if extra == "tags_include_diving":
                ok_extra = (count >= 1) and all(
                    "diving" in h["payload"]["activity_tags"] for h in result["hotels"]
                )
            elif extra == "no_dup_hotels":
                ids = [h["hotel_id"] for h in result["hotels"]]
                ok_extra = len(ids) == len(set(ids))
            elif extra == "region_all_in_scope":
                ok_extra = all(h["payload"]["region"] == REGION for h in result["hotels"])
            elif extra == "evidence_in_fb_or_overview":
                ok_extra = all(
                    h["evidence_chunk"]["category"] in {"food_beverage", "overview"}
                    for h in result["hotels"]
                )
            ok = ok_reason and ok_extra
            verdict = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            top_scores.append((label, top))
            print(f"{label:<32}{count:<5}{top:<8.3f}{str(reason):<28}{verdict}")

        print("-" * len(header))
        print(f"\nResults: {passed} passed, {failed} failed")

        print("\nTop-score distribution (for threshold calibration):")
        for label, score in top_scores:
            print(f"  {score:>6.3f}   {label}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
