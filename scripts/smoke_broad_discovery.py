"""Mock-Qdrant smoke harness for BroadHotelDiscovery (Phase 2b, Task 6).

Flattens data/seed/hotels.json into pseudo-Qdrant points (with full
payload including region + activity_tags), uses a deterministic
token-overlap scorer, and exercises the 8 Gherkin scenarios from
docs/call-center/phase2b-user-story.md against a fully in-memory
backend (no live Qdrant required).

Run:
    python scripts/smoke_broad_discovery.py
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from voxtera.call_center.discovery import BroadHotelDiscovery

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
                    "hotel_name": hotel.get("name", ""),
                    "category": cat,
                    "text": chunk.get("text", ""),
                    "text_en": chunk.get("text_en", ""),
                    "region": (hotel.get("region") or "").lower(),
                    "country": (hotel.get("country") or "").lower(),
                    "district": (hotel.get("district") or "").lower(),
                    "price_tier": hotel.get("price_tier", ""),
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
        if "value" in m:
            if v != m["value"]:
                return False
        elif "any" in m:
            wanted = set(m["any"])
            if isinstance(v, list):
                if not wanted.intersection(v):
                    return False
            else:
                if v not in wanted:
                    return False
    return True


def _make_search_fn(points: list[dict[str, Any]], holder: dict[str, str]):
    async def _search(_vector, flt, limit):
        qtoks = _tokens(holder["q"])
        if not qtoks:
            return []
        scored = []
        for p in points:
            if not _matches_filter(p["payload"], flt):
                continue
            overlap = len(qtoks & p["_tokens"])
            score = overlap / max(len(qtoks), 1)
            scored.append({"id": p["id"], "score": score, "payload": p["payload"]})
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:limit]
    return _search


# (label, region, query, tags, category_hint, expected_reason, extra_check)
REGION = "turkish riviera"
SCENARIOS = [
    ("region happy path", REGION, "luxury hotel spa wellness",
     None, None, None, None),
    ("activity_tags narrows", REGION, "diving snorkel scuba",
     ["diving"], None, None, "tags_include_diving"),
    ("empty region", "  ", "anything", None, None, "no_region_scope", None),
    ("empty query", REGION, "   ", None, None, "empty_query", None),
    ("no match above threshold", REGION, "xyzzy plugh zorkmid grue",
     None, None, "no_match_above_threshold", None),
    ("dedup aggregation", REGION, "water park aquapark slides",
     None, None, None, "no_dup_hotels"),
    ("no region leakage", REGION, "beach sea",
     None, None, None, "region_all_in_scope"),
    ("category_hint food_beverage", REGION, "buffet restaurant dinner",
     None, "food_beverage", None, "evidence_in_fb_or_overview"),
]


async def main() -> None:
    points = _load_points()
    regions = {p["payload"]["region"] for p in points}
    hotel_count = len({p["payload"]["hotel_id"] for p in points})
    print(f"Loaded {len(points)} chunks from {hotel_count} hotels across regions={sorted(regions)}\n")

    holder = {"q": ""}
    search_fn = _make_search_fn(points, holder)

    async def fake_embed(_text: str) -> list[float]:
        return [0.0]

    discovery = BroadHotelDiscovery(
        max_hotels=5, min_score=0.20, embed_fn=fake_embed, search_fn=search_fn
    )

    header = f"{'Scenario':<32}{'Region':<12}{'Got':<6}{'Reason':<28}{'Verdict'}"
    print(header)
    print("-" * len(header))
    passed = failed = 0
    for label, region, query, tags, hint, expected_reason, extra in SCENARIOS:
        holder["q"] = query
        result = await discovery.discover(
            region=region.strip(), query=query,
            activity_tags=tags, category_hint=hint,
        )
        ok_reason = (result["reason"] == expected_reason)
        ok_extra = True
        if extra == "tags_include_diving":
            ok_extra = (result["count"] >= 1) and all(
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
        print(f"{label:<32}{(region.strip() or '(none)'):<12}{result['count']:<6}"
              f"{str(result['reason']):<28}{verdict}")

    print("-" * len(header))
    print(f"\nResults: {passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
