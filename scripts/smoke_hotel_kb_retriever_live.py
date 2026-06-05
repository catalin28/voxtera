"""Live-Qdrant smoke harness for HotelKBRetriever (Phase 2a follow-up).

Hits the real `hotel_kb` Qdrant collection on the dev droplet using the
production `HotelKBRetriever` end-to-end (real `multilingual-e5-large`
embeddings, real network round-trip). No DI overrides.

Pre-reqs:
    - .env has QDRANT_URL, QDRANT_API_KEY
    - `hotel_kb` collection is populated (92 points expected)
    - First run downloads / loads E5 weights (~2 GB), then cached

Run:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_hotel_kb_retriever_live.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from voxtera.call_center.kb_retriever import HotelKBRetriever

# (label, hotel_id, query, category_hint, expected_reason, min_chunks)
SCENARIOS = [
    ("scoped happy path",         "rixos_premium_belek", "water park land of legends",  None,            None,                       1),
    ("category hint food_beverage","rixos_premium_belek", "breakfast restaurants dining","food_beverage", None,                       1),
    # E5-large produces ~0.76 even for nonsense queries; absolute threshold cannot reject these.
    # See kb_config.DEFAULT_MIN_SCORE comment. We record the behaviour rather than assert rejection.
    ("junk query (E5 floor ~0.76)", "rixos_premium_belek", "xyzzy plugh zorkmid grue",   None,            None,                       0),
    ("empty hotel_id",             "",                    "water park",                 None,            "no_hotel_scope",           0),
    ("empty query rejected",       "rixos_premium_belek", "   ",                        None,            "empty_query",              0),
    ("no cross-hotel leak",        "maxx_royal_belek",    "water park",                 None,            None,                       0),
]


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if "QDRANT_URL" not in os.environ:
        print("ERROR: QDRANT_URL not in env (check .env)")
        return 2

    print(f"Target Qdrant: {os.environ['QDRANT_URL']}")
    print("Loading multilingual-e5-large (first run downloads weights)...\n")

    async with aiohttp.ClientSession() as session:
        retriever = HotelKBRetriever(session=session)

        header = (
            f"{'Scenario':<30}{'Hotel':<24}{'Got':<5}"
            f"{'Top':<8}{'Reason':<28}{'Verdict'}"
        )
        print(header)
        print("-" * len(header))

        passed = failed = 0
        top_scores: list[tuple[str, float]] = []
        for label, hotel_id, query, hint, expected_reason, min_chunks in SCENARIOS:
            result = await retriever.retrieve(
                hotel_id=hotel_id, query=query, category_hint=hint
            )
            count = result["count"]
            top = result["top_score"]
            reason = result["reason"]
            # Hard no-leak invariant: any returned chunk must have the right hotel_id.
            leak = any(
                (c.get("payload", {}).get("hotel_id") if "payload" in c else c.get("hotel_id", hotel_id)) not in ("", hotel_id, None)
                for c in result["chunks"]
            )
            ok_reason = (reason == expected_reason)
            ok_count = (count >= min_chunks)
            ok_hint = True
            if hint and count > 0:
                ok_hint = all(
                    c.get("category") in {hint, "overview"} for c in result["chunks"]
                )
            ok = ok_reason and ok_count and not leak and ok_hint
            verdict = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            top_scores.append((label, top))
            print(
                f"{label:<30}{(hotel_id or '(none)'):<24}{count:<5}"
                f"{top:<8.3f}{str(reason):<28}{verdict}"
            )

        print("-" * len(header))
        print(f"\nResults: {passed} passed, {failed} failed")

        print("\nTop-score distribution (for threshold calibration):")
        for label, score in top_scores:
            print(f"  {score:>6.3f}   {label}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
