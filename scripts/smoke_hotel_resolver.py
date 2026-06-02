"""Phase 1 smoke test for HotelResolver — runs without a live Elasticsearch.

Uses an in-memory mock `search_fn` that returns plausible ES hit shapes for a
catalogue of seed hotels, then drives the resolver across representative caller
mentions (exact, partial, fuzzy/Turkish spelling, alias, chain-only, ambiguous,
nonsense) and prints a results table.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_hotel_resolver.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from voxtera.call_center.resolver import HotelResolver

SEED_FILE = Path(__file__).resolve().parents[1] / "data" / "seed" / "hotels.json"


def _load_hotels() -> list[dict[str, Any]]:
    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    return [
        {
            "hotel_id": h["hotel_id"],
            "name": h["name"],
            "aliases": h.get("aliases", []),
            "chain": h.get("chain", ""),
            "city": h.get("city", ""),
            "district": h.get("district", ""),
        }
        for h in raw
    ]


def _score(hotel: dict[str, Any], query: str) -> float:
    """Heuristic score that approximates ES `name^10 + aliases^8 + chain^5 + location^2`.

    Returns a normalized score in [0, 1] suitable for the resolver's threshold policy.
    """
    q = query.lower().strip()
    if not q:
        return 0.0

    tokens = [t for t in q.split() if t]
    name = hotel["name"].lower()
    aliases = [a.lower() for a in hotel.get("aliases", [])]
    chain = (hotel.get("chain") or "").lower()
    city = (hotel.get("city") or "").lower()
    district = (hotel.get("district") or "").lower()

    score = 0.0

    # Exact / substring name match.
    if q == name:
        score += 1.0
    elif q in name or name in q:
        score += 0.75
    matched_tokens = sum(1 for t in tokens if t in name)
    if tokens:
        score += 0.55 * (matched_tokens / len(tokens))

    # Aliases.
    for alias in aliases:
        if q == alias:
            score += 0.85
            break
        if q in alias or alias in q:
            score += 0.55
            break
    alias_blob = " ".join(aliases)
    alias_token_hits = sum(1 for t in tokens if t in alias_blob)
    if tokens and alias_blob:
        score += 0.35 * (alias_token_hits / len(tokens))

    # Chain (weaker contribution).
    if chain and chain in q:
        score += 0.30

    # Location hints.
    if city and city in q:
        score += 0.10
    if district and district in q:
        score += 0.15

    return min(score, 1.5)  # raw cap; resolver compares to 0.85 / 0.55 thresholds


def make_mock_search(hotels: list[dict[str, Any]]):
    async def _search(query: str, size: int) -> list[dict[str, Any]]:
        scored = [(h, _score(h, query)) for h in hotels]
        scored = [(h, s) for h, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            {"_score": s, "_source": h} for h, s in scored[:size]
        ]

    return _search


MENTIONS: list[tuple[str, str]] = [
    ("Rixos Premium Belek",           "exact_full_name"),
    ("Riksos Premium Belek",          "fuzzy_brand_typo"),
    ("Rixos Land of Legends",         "alias"),
    ("Maxx Royal",                    "chain_partial_unique"),
    ("Cornelia",                      "chain_partial_ambiguous"),
    ("Hilton",                        "chain_only"),
    ("Belek otel",                    "district_only_weak"),
    ("Quantum Sparkle Resort",        "nonsense"),
    ("",                              "empty"),
    ("  Rixos   Premium  Belek  ",    "whitespace_noise"),
    ("Rixos\u2019 Belek",             "smart_apostrophe"),
]


async def main() -> None:
    hotels = _load_hotels()
    search_fn = make_mock_search(hotels)
    resolver = HotelResolver(search_fn=search_fn)

    print(f"Loaded {len(hotels)} seed hotels\n")
    header = f"{'mention':<32} {'kind':<26} {'decision':<22} {'score':>6}  {'hotel_id / top candidates'}"
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
        print(f"{printable_mention!s:<32} {kind:<26} {decision:<22} {top_score:>6.3f}  {detail}")

    print()
    print("Summary:", summary)


if __name__ == "__main__":
    asyncio.run(main())
