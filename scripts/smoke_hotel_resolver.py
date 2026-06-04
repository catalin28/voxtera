"""Phase 1 smoke test for HotelResolver — runs against live Elasticsearch.

Drives the resolver across representative caller mentions (exact, partial,
fuzzy/Turkish spelling, alias, chain-only, ambiguous, nonsense) using the
real Elasticsearch cluster configured in .env and prints a results table.

Requires:
    ELASTICSEARCH_URL, ELASTICSEARCH_USER, ELASTICSEARCH_PASSWORD in .env

Run:
    uv run python scripts/smoke_hotel_resolver.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voxtera.call_center.resolver import HotelResolver

# Load .env from project root.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


MENTIONS: list[tuple[str, str]] = [
    ("Rixos Premium Belek", "exact_full_name"),
    ("Riksos Premium Belek", "fuzzy_brand_typo"),
    ("Rixos Land of Legends", "alias"),
    ("Maxx Royal", "chain_partial_unique"),
    ("Cornelia", "chain_partial_ambiguous"),
    ("Hilton", "chain_only"),
    ("Belek otel", "district_only_weak"),
    ("Quantum Sparkle Resort", "nonsense"),
    ("", "empty"),
    ("  Rixos   Premium  Belek  ", "whitespace_noise"),
    ("Rixos\u2019 Belek", "smart_apostrophe"),
]


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        resolver = HotelResolver(session=session, index_name="hotels")

        header = (
            f"{'mention':<32} {'kind':<26} {'decision':<22}"
            f" {'score':>6}  {'hotel_id / top candidates'}"
        )
        print(header)
        print("-" * len(header))

        summary = {"auto_resolve": 0, "needs_clarification": 0, "no_match": 0}

        for mention, kind in MENTIONS:
            result = await resolver.resolve(mention)
            decision = result["decision"]
            summary[decision] = summary.get(decision, 0) + 1
            top_score = result.get("top_score") or 0.0
            if decision == "auto_resolve":
                detail = result["hotel_id"]
            elif decision == "needs_clarification":
                detail = ", ".join(c["hotel_id"] for c in result["candidates"])
            else:
                reason = result.get("reason") or ""
                detail = f"({reason})" if reason else ""
            printable_mention = mention if mention else "<empty>"
            print(
                f"{printable_mention!s:<32} {kind:<26} {decision:<22} {top_score:>6.3f}  {detail}"
            )

        print()
        print("Summary:", summary)


if __name__ == "__main__":
    asyncio.run(main())
