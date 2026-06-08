"""Mock-Qdrant smoke harness for CompoundAndDiscovery (Phase 2c).

Reuses the in-memory token-overlap backend pattern from
smoke_broad_discovery.py, but routes each requirement through its own
embed/search call (the holder dict captures the most recent query) so a
single fan-out exercises one BroadHotelDiscovery instance per
requirement.

Run:
    python scripts/smoke_compound_discovery.py
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from voxtera.call_center.compound import CompoundAndDiscovery
from voxtera.call_center.discovery import BroadHotelDiscovery

SEED_FILE = Path(__file__).resolve().parents[1] / "data" / "seed" / "hotels.json"
REGION = "turkish riviera"


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


def _build_discovery(points: list[dict[str, Any]]) -> BroadHotelDiscovery:
    holder = {"q": ""}

    def embed(text: str) -> list[float]:
        holder["q"] = text
        return [0.0]

    async def search(_vector, flt, limit):
        qtoks = _tokens(holder["q"])
        if not qtoks:
            return []
        scored = []
        for p in points:
            if not _matches_filter(p["payload"], flt):
                continue
            overlap = len(qtoks & p["_tokens"])
            score = overlap / max(len(qtoks), 1)
            if score > 0:
                scored.append({"id": p["id"], "score": score, "payload": p["payload"]})
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:limit]

    return BroadHotelDiscovery(
        max_hotels=5, min_score=0.10, relative_margin=1.0,
        embed_fn=embed, search_fn=search,
    )


# (label, region, requirements, expected_reason, expected_missing_subset)
SCENARIOS = [
    ("strict intersection - spa+pool",
     REGION, ["spa wellness massage", "pool aquapark slides"], None, set()),
    ("partial - spa + nonsense",
     REGION, ["spa wellness", "xyzzy plugh grue"],
     "partial_match_only", {"xyzzy plugh grue"}),
    ("all-nonsense -> no_match",
     REGION, ["xyzzy plugh", "grue zorkmid"],
     "no_match_above_threshold", {"xyzzy plugh", "grue zorkmid"}),
    ("empty region -> no_region_scope",
     "  ", ["spa"], "no_region_scope", set()),
    ("empty requirements -> empty_requirements",
     REGION, ["  ", ""], "empty_requirements", set()),
    ("single requirement passthrough",
     REGION, ["beach sea water"], None, set()),
]


async def main() -> None:
    points = _load_points()
    hotel_count = len({p["payload"]["hotel_id"] for p in points})
    print(f"Loaded {len(points)} chunks from {hotel_count} hotels\n")

    discovery = _build_discovery(points)
    compound = CompoundAndDiscovery(discovery=discovery)

    header = f"{'Scenario':<42}{'Got':<6}{'Reason':<28}{'Missing':<24}{'Verdict'}"
    print(header)
    print("-" * len(header))
    passed = failed = 0

    for label, region, reqs, expected_reason, expected_missing in SCENARIOS:
        result = await compound.discover(region=region.strip(), requirements=reqs)
        ok_reason = (result["reason"] == expected_reason)
        ok_missing = expected_missing.issubset(set(result["missing_requirements"]))
        if expected_reason in (None,) or expected_reason == "partial_match_only":
            ok_count = result["count"] >= 1
        else:
            ok_count = result["count"] == 0
        ok = ok_reason and ok_missing and ok_count
        verdict = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        missing_str = ",".join(result["missing_requirements"])[:22] or "-"
        print(f"{label:<42}{result['count']:<6}{str(result['reason']):<28}{missing_str:<24}{verdict}")

    print("-" * len(header))
    print(f"\nResults: {passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
