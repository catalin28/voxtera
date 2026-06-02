# VOX-RAG-P2B-001 — Broad Hotel Discovery

**Phase:** 2b — Broad Discovery Search
**Branch:** feat/VOX-rag-broad
**Depends on:** Phase 2a (`HotelKBRetriever`, shared embeddings + kb_config)
**Architecture:** [Voxtera_RAG_Architecture_v0.3.md](Voxtera_RAG_Architecture_v0.3.md) §6 Path 2 (Broad / Cross-Hotel)

## 1. Persona & Need

**As** a prospective guest who has not yet picked a hotel
**I want** to describe what I'm looking for in natural language (region + intent)
**So that** the assistant can surface a short list of candidate hotels — not chunks — for me to choose from.

Canonical query: _"luxury hotel with spa in Antalya"_ → 3–5 candidate hotels, each with one supporting evidence chunk.

## 2. Scope

**In scope (2b):**
- New `BroadHotelDiscovery` class — semantic search over `hotel_kb` with **region as the only hard filter**; optional `activity_tags` any-of filter.
- Hit aggregation: group raw chunks by `hotel_id`, score each hotel = max chunk score, keep the best chunk as evidence.
- Decision contract returning a list of `hotels`, not `chunks`.
- Optional `category_hint` carried through unchanged from 2a semantics.
- Thin `GET /call_center/api/kb/discover` endpoint.
- Unit suite + mock smoke harness over `data/seed/hotels.json`.

**Out of scope (deferred to later sub-phases):**
- Compound multi-requirement AND (Phase 2c).
- Structured budget / star / board filters beyond `region` + `activity_tags` (Phase 2d).
- Dual-index hybrid retrieval (Phase 2e).
- Ingestion / re-embedding (Phase 2f).
- Chat-pipeline integration (post-Phase 5).

## 3. Decision Contract

Return shape:

```jsonc
{
  "region": "antalya",
  "query": "luxury hotel with spa",
  "count": 3,
  "hotels": [
    {
      "hotel_id": "rixos_premium_belek",
      "score": 0.81,
      "evidence_chunk": {
        "chunk_id": "rixos_premium_belek::wellness::0",
        "category": "wellness",
        "text": "...",
        "text_en": "..."
      }
    }
  ],
  "top_score": 0.81,
  "reason": null
}
```

Reasons (non-null only when `count == 0`):

| Reason | Trigger |
|--------|---------|
| `empty_query` | Normalized query is empty |
| `no_region_scope` | `region` arg empty/whitespace |
| `no_match_above_threshold` | Every chunk scored below `min_score` (default 0.25) |
| `retriever_error` | Backend or embedding exception |

Hard invariants:
- Every entry in `hotels[]` has a payload `region` matching the request (no region leakage).
- `hotels[]` length ≤ `max_hotels` (default 5).
- `hotels[]` sorted by `score` desc; no duplicate `hotel_id`.

## 4. Acceptance Scenarios (Gherkin)

```gherkin
Feature: Broad hotel discovery across a region

Scenario: Region + intent surfaces top hotels
  Given the seed corpus is loaded
  When I call discover(region="antalya", query="luxury hotel with spa")
  Then the response count is between 1 and 5
  And every hotel in the response has region "antalya"
  And the top hotel's evidence_chunk.category is one of {wellness, amenities, overview}

Scenario: Activity tag filter narrows the candidate set
  Given the seed corpus is loaded
  When I call discover(region="antalya", query="diving", activity_tags=["scuba_diving"])
  Then every returned hotel's payload activity_tags contains "scuba_diving"

Scenario: Empty region is rejected
  When I call discover(region="  ", query="anything")
  Then the response is {count: 0, reason: "no_region_scope"}

Scenario: Empty query is rejected
  When I call discover(region="antalya", query="   ")
  Then the response is {count: 0, reason: "empty_query"}

Scenario: No match above threshold
  Given a query that lexically does not match any chunk
  When I call discover(region="antalya", query="xyzzy plugh zorkmid")
  Then the response is {count: 0, reason: "no_match_above_threshold", top_score: <best_below>}

Scenario: Hotel aggregation deduplicates chunks
  Given a hotel has 4 chunks all matching the query
  When I call discover(...)
  Then that hotel appears exactly once in hotels[]
  And its score equals the max of its 4 chunk scores

Scenario: No region leakage
  Given the seed corpus contains hotels in regions "antalya" and "bodrum"
  When I call discover(region="antalya", query="beach")
  Then no returned hotel has region "bodrum"

Scenario: Retriever error degrades gracefully
  Given the search backend raises
  When I call discover(...)
  Then the response is {count: 0, reason: "retriever_error"}
  And the exception is logged but not propagated
```

## 5. Definition of Done

- `BroadHotelDiscovery` lives in `src/voxtera/call_center/discovery.py` with injectable `embed_fn` + `search_fn` (same pattern as Phase 2a).
- ≥ 10 unit tests covering all scenarios above, all green via `pytest tests/call_center -q`.
- Mock-Qdrant smoke harness `scripts/smoke_broad_discovery.py` covers all 8 Gherkin scenarios over the 11-hotel seed corpus.
- Thin `GET /call_center/api/kb/discover?region=...&q=...&category=...&tags=tag1,tag2` handler added to `server.py` (≤ 12 lines, no business logic).
- `docs/call-center/phase2b-test-report.md` written with unit + smoke results.
- Stage Tracker in `phase2b-development-plan.md` shows all tasks Done (or deferred with notes).
- Branch `feat/VOX-rag-broad` merged into `develop` via `git merge --no-ff` (no PR — solo dev).
- Phase 2a tests stay green (no regression in `HotelKBRetriever` from any shared-module changes).
