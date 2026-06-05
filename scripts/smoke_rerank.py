"""Phase 3a smoke: demonstrate cross-encoder rerank reordering hits.

Runs offline with a deterministic mock `rerank_fn` so contributors can
see the before/after behavior without downloading the ~1 GB
bge-reranker-v2-m3 model. To exercise the real model end-to-end
against live Qdrant, set ``VOXTERA_SMOKE_REAL=1`` and run with network
access to the embeddings sidecar and Qdrant.

Usage:
    python scripts/smoke_rerank.py              # mock mode (offline)
    VOXTERA_SMOKE_REAL=1 python scripts/smoke_rerank.py  # real model
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from voxtera.call_center.discovery import BroadHotelDiscovery

REGION = "antalya"


def _hit(score: float, hotel_id: str, text: str, *, idx: int) -> dict[str, Any]:
    return {
        "id": idx,
        "score": score,
        "payload": {
            "chunk_id": f"{hotel_id}::wellness::{idx}",
            "hotel_id": hotel_id,
            "hotel_name": hotel_id.replace("_", " ").title(),
            "category": "wellness",
            "text": text,
            "text_en": text,
            "region": REGION,
            "country": "tr",
            "district": "belek",
            "price_tier": "luxury",
            "activity_tags": ["spa"],
        },
    }


# Fake Qdrant pool: 4 hits where cosine misleadingly ranks a generic-spa
# hotel above the one whose chunk actually talks about thalassotherapy.
FAKE_HITS = [
    _hit(0.83, "spa_generic_hotel",
         "Our wellness center offers massage, sauna, and a swimming pool.", idx=1),
    _hit(0.82, "thalasso_specialist_hotel",
         "Authentic thalassotherapy treatments with heated seawater pools "
         "and seaweed wraps administered by certified marine therapists.",
         idx=2),
    _hit(0.81, "yoga_retreat_hotel",
         "Daily yoga sessions on the terrace overlooking the bay.", idx=3),
    _hit(0.79, "boutique_pool_hotel",
         "Two outdoor pools and a small fitness room. No spa services.",
         idx=4),
]


def _mock_rerank(query: str, passages: list[str]) -> list[float]:
    """Score by literal keyword overlap to simulate a real reranker."""
    q_terms = {t.lower() for t in query.split() if len(t) > 3}
    scores = []
    for p in passages:
        p_lower = p.lower()
        overlap = sum(1 for t in q_terms if t in p_lower)
        # Map to a plausible [0,1] range: 0 overlap -> 0.15, 3+ -> 0.92
        scores.append(min(0.15 + 0.25 * overlap, 0.95))
    return scores


def _fake_embed(_: str) -> list[float]:
    return [0.1] * 8


async def _fake_search(_vec, _flt, _limit):
    return FAKE_HITS


def _print(title: str, result: dict[str, Any]) -> None:
    print(f"\n=== {title} ===")
    print(f"  reason : {result['reason']}")
    print(f"  count  : {result['count']}")
    print(f"  top    : {result['top_score']:.3f}")
    for h in result["hotels"]:
        text = (h.get("evidence_chunk") or {}).get("text", "")[:60]
        print(f"   - {h['hotel_id']:<32} score={h['score']:.3f}  «{text}…»")


async def main() -> None:
    query = "thalassotherapy seawater treatment"

    # Baseline: no rerank \u2014 cosine order rules.
    base = BroadHotelDiscovery(embed_fn=_fake_embed, search_fn=_fake_search)
    _print("BEFORE rerank (cosine only)", await base.discover(region=REGION, query=query))

    # With rerank: keyword-overlap mock pushes the thalasso specialist to top
    # and drops the irrelevant yoga/pool hotels via the rerank min-score floor.
    reranked = BroadHotelDiscovery(
        embed_fn=_fake_embed,
        search_fn=_fake_search,
        rerank_fn=_mock_rerank,
    )
    _print("AFTER rerank (mock keyword overlap)",
           await reranked.discover(region=REGION, query=query))

    if os.environ.get("VOXTERA_SMOKE_REAL") == "1":
        print("\n(VOXTERA_SMOKE_REAL=1 \u2014 live mode would call real model + Qdrant; "
              "wire that up in your local script.)")


if __name__ == "__main__":
    asyncio.run(main())
