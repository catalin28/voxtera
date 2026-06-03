"""Live end-to-end smoke for the Phase 3 ConciergeAgent.

Exercises the FULL production path:
  - real Anthropic Claude for decompose + render
  - real CompoundAndDiscovery -> live Qdrant `hotel_kb` collection
  - real multilingual-e5-large embeddings

KNOWN LIMITATION (carried from Phase 2c live smoke):
With absolute floor DEFAULT_MIN_SCORE=0.70 the e5-large model surfaces
junk hits (0.74-0.79) for nonsensical queries, so a no_match assertion
against the live corpus is not reliable. This smoke verifies decompose
shape, compound wiring, render coherence, and language detection.
Hard pass/fail is limited to wire-up; output quality is a manual review.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_concierge_live.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Force UTF-8 on Windows consoles so Turkish/Cyrillic utterances print cleanly.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import aiohttp
from dotenv import load_dotenv

from voxtera.call_center.concierge import ConciergeAgent

REGION = "Turkish Riviera"

SCENARIOS = [
    ("EN: spa + scuba diving",
     "I'd like a hotel with a great spa and scuba diving for my partner.",
     REGION),
    ("EN: family + kids club + beach",
     "Looking for a family resort with a kids club right on the beach.",
     REGION),
    ("EN: single requirement",
     "Where can I find a luxury wellness retreat?",
     REGION),
    ("TR: Turkish utterance (language detection)",
     "Ailecek tatil için çocuk kulübü ve özel plajı olan bir otel arıyorum.",
     REGION),
]


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    for key in ("ANTHROPIC_API_KEY", "QDRANT_URL"):
        if key not in os.environ:
            print(f"ERROR: {key} not set in environment / .env")
            return 2

    print(f"Anthropic model: {os.environ.get('LLM_MODEL_OVERRIDE', 'claude-haiku-4-5-20251001')}")
    print(f"Qdrant target:   {os.environ['QDRANT_URL']}")
    print("Loading multilingual-e5-large (first run downloads weights)...\n")

    async with aiohttp.ClientSession() as session:
        agent = ConciergeAgent(session=session)

        passed = failed = 0
        for label, utterance, region in SCENARIOS:
            print("=" * 80)
            print(f"[{label}]")
            print(f"  utterance: {utterance}")
            print(f"  region:    {region}")

            try:
                result = await agent.answer(utterance=utterance, region=region)
            except Exception as e:  # noqa: BLE001
                print(f"  EXCEPTION: {e!r}")
                failed += 1
                continue

            decomp = result.get("decomposition") or {}
            retrieval = result.get("retrieval") or {}
            print(f"  decomp.requirements: {decomp.get('requirements')}")
            print(f"  decomp.language:     {decomp.get('language')}")
            print(f"  decomp.tags:         {decomp.get('activity_tags')}")
            print(f"  decomp.category:     {decomp.get('category_hint')}")
            print(f"  retrieval.reason:    {retrieval.get('reason')}")
            print(f"  retrieval.count:     {retrieval.get('count')}")
            print(f"  retrieval.top_score: {retrieval.get('top_score')}")
            for h in (retrieval.get("hotels") or [])[:3]:
                name = (h.get("payload") or {}).get("hotel_name") or h.get("hotel_id")
                print(f"    - {name}  score={h.get('score'):.3f}")
            print(f"  ANSWER:\n    {result.get('answer')}")

            # Hard checks: decompose returned non-empty requirements, render returned
            # something non-trivial.
            ok = bool(decomp.get("requirements")) and len(result.get("answer") or "") > 10
            print(f"  Verdict: {'PASS' if ok else 'FAIL'}")
            if ok:
                passed += 1
            else:
                failed += 1

        print("=" * 80)
        print(f"\nResults: {passed} passed, {failed} failed")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
