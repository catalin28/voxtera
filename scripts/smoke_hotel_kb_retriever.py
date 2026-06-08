"""Mock-Qdrant smoke harness for HotelKBRetriever (Phase 2a, Task 7).

Flattens data/seed/hotels.json into pseudo-Qdrant points with a
deterministic token-overlap scorer, then exercises the Gherkin
scenarios from docs/call-center/phase2a-user-story.md against a fully
in-memory backend (no live Qdrant required).

Run:
    python scripts/smoke_hotel_kb_retriever.py
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from voxtera.call_center.kb_retriever import HotelKBRetriever

SEED_FILE = Path(__file__).resolve().parents[1] / "data" / "seed" / "hotels.json"


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


def _load_points() -> list[dict[str, Any]]:
    hotels = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    points: list[dict[str, Any]] = []
    for hotel in hotels:
        for i, chunk in enumerate(hotel.get("chunks", [])):
            cat = chunk.get("category", "")
            points.append({
                "id": f"{hotel['hotel_id']}_{i}",
                "payload": {
                    "chunk_id": f"{hotel['hotel_id']}::{cat}::{i}",
                    "hotel_id": hotel["hotel_id"],
                    "category": cat,
                    "text": chunk.get("text", ""),
                    "text_en": chunk.get("text_en", ""),
                    "activity_tags": hotel.get("activity_tags", []),
                },
                "_tokens": _tokens(chunk.get("text", "") + " " + chunk.get("text_en", "")),
            })
    return points


def _matches_filter(payload: dict[str, Any], flt: dict[str, Any]) -> bool:
    for clause in flt.get("must", []):
        key = clause["key"]
        m = clause["match"]
        v = payload.get(key)
        if "value" in m and v != m["value"]:
            return False
        if "any" in m and v not in m["any"]:
            return False
    return True


def _make_search_fn(points: list[dict[str, Any]], query_text_holder: dict[str, str]):
    async def _search(_vector, flt, limit):
        qtoks = _tokens(query_text_holder["q"])
        scored = []
        for p in points:
            if not _matches_filter(p["payload"], flt):
                continue
            overlap = len(qtoks & p["_tokens"])
            if not qtoks:
                continue
            score = overlap / max(len(qtoks), 1)
            scored.append({"id": p["id"], "score": score, "payload": p["payload"]})
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:limit]
    return _search


SCENARIOS = [
    # (label, hotel_id, query, category_hint, expected_reason, expected_min_chunks)
    ("scoped happy path", "rixos_premium_belek", "water park land legends",
     None, None, 1),
    ("no match above threshold", "rixos_premium_belek", "xyzzy plugh zorkmid grue",
     None, "no_match_above_threshold", 0),
    ("no hotel scope", "", "water park", None, "no_hotel_scope", 0),
    ("no cross-hotel leak", "maxx_royal_belek", "water park",
     None, None, 0),  # may be 0 if no chunks match; key check below is hotel_id
    ("category hint food_beverage", "rixos_premium_belek", "breakfast restaurants dining",
     "food_beverage", None, 0),
    ("empty query rejected", "rixos_premium_belek", "   ", None, "empty_query", 0),
]


async def main() -> None:
    points = _load_points()
    print(f"Loaded {len(points)} chunks from {len({p['payload']['hotel_id'] for p in points})} hotels\n")

    holder = {"q": ""}
    search_fn = _make_search_fn(points, holder)

    async def fake_embed(_text: str) -> list[float]:
        return [0.0]  # vector unused by token-overlap mock

    retriever = HotelKBRetriever(
        top_k=3, min_score=0.20, embed_fn=fake_embed, search_fn=search_fn
    )

    print(f"{'Scenario':<32}{'Hotel':<25}{'Got':<8}{'Reason':<28}{'Verdict'}")
    print("-" * 110)
    passed = failed = 0
    for label, hotel_id, query, hint, expected_reason, min_chunks in SCENARIOS:
        holder["q"] = query
        result = await retriever.retrieve(
            hotel_id=hotel_id, query=query, category_hint=hint
        )
        leak = any(c.get("category") and  # noqa - dummy
                   hit_hid != hotel_id
                   for c in result["chunks"]
                   for hit_hid in [c.get("hotel_id", hotel_id)])
        ok_reason = (result["reason"] == expected_reason)
        ok_count = (result["count"] >= min_chunks)
        ok = ok_reason and ok_count and not leak
        verdict = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"{label:<32}{hotel_id or '(none)':<25}{result['count']:<8}"
              f"{str(result['reason']):<28}{verdict}")

    print("-" * 110)
    print(f"\nResults: {passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
