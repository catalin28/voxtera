# Phase 2a — Scoped Hotel KB Retrieval

Story ID: VOX-RAG-P2A-001
Phase: 2a of 6 sub-phases in Phase 2 — RAG Core (see [Voxtera_RAG_Development_Plan.md §Phase 2](Voxtera_RAG_Development_Plan.md))
Branch: feat/vox-kb-retrieval
Depends on: Phase 1 (`HotelResolver`) — merged into `develop`

**Scope of this sub-phase (2a only).** Single-hotel scoped retrieval against `hotel_kb` filtered by `hotel_id`. This is search mode #1 of the six in Phase 2.

**Explicitly out of scope for 2a** (delivered by later sub-phases):
- Broad cross-hotel discovery without a `hotel_id` → Phase 2b
- Multi-requirement / compound AND queries ("spa + scuba") → Phase 2c
- Budget tier / geo radius filters → Phase 2d
- Dual / comparison search (hotel A vs hotel B) → Phase 2e
- Ingestion pipeline, confidence banding, Redis cache → Phase 2f

## 1. User Story

> **As** a tourist calling the Voxtera hotel concierge,
> **when** I ask a factual question about a hotel ("does Rixos Belek have a water park?", "do they allow pets?", "what time is breakfast?"),
> **I want** the bot to answer from that specific hotel's curated knowledge base,
> **so that** I get an accurate, hotel-specific answer instead of a generic guess.

> **As** the call-center operator,
> **I want** retrieval to be scoped strictly to the resolved `hotel_id`,
> **so that** answers never leak content from a different property in the catalogue.

## 2. Business Context

Phase 1 turns a spoken hotel mention into a canonical `hotel_id`. Phase 2a uses that id to fetch the *right slice* of curated content from Qdrant and hand it to the LLM. This is the "Path 1 — Scoped Qdrant" branch in [Voxtera_RAG_Architecture_v0.3.md](Voxtera_RAG_Architecture_v0.3.md) and search mode #1 in [Voxtera_RAG_Development_Plan.md §Phase 2a](Voxtera_RAG_Development_Plan.md).

Out of scope for **this** sub-phase (see header for full list):
- All other Phase 2 search modes (2b–2e).
- Ingestion pipeline, confidence bands, Redis cache (2f).
- Live integration into the voice/chat pipeline — separate wiring story after Phase 5.

## 3. Acceptance Criteria (Gherkin)

```gherkin
Feature: Hotel KB retrieval scoped to a resolved hotel

  Background:
    Given the seed hotels and their KB chunks are loaded into Qdrant collection "hotel_kb"
    And each chunk payload carries hotel_id, category, text, text_en, region, activity_tags

  Scenario: Factual hotel-scoped question returns relevant chunks
    Given the resolver returned hotel_id "rixos_premium_belek" with decision "auto_resolve"
    When I retrieve KB for query "is there a water park"
    Then the retriever returns between 1 and RAG_TOP_K chunks
    And every returned chunk has hotel_id == "rixos_premium_belek"
    And the top chunk's score is >= RAG_MIN_SCORE
    And at least one returned chunk's category is one of {"activities","amenities","overview"}

  Scenario: Question matches no chunk above the floor returns empty
    Given the resolver returned hotel_id "rixos_premium_belek"
    When I retrieve KB for query "do they accept dogecoin as payment"
    Then the retriever returns 0 chunks
    And the result reason is "no_match_above_threshold"

  Scenario: Retrieval refuses to run without a hotel_id
    When I call the retriever with hotel_id = "" and any query
    Then the retriever returns 0 chunks
    And the result reason is "no_hotel_scope"

  Scenario: Retrieval never crosses hotel boundaries
    Given chunks exist for "rixos_premium_belek" AND "maxx_royal_belek"
    When I retrieve KB for "water park" scoped to "rixos_premium_belek"
    Then no returned chunk has hotel_id == "maxx_royal_belek"

  Scenario: Category hint narrows results
    Given the resolver returned hotel_id "rixos_premium_belek"
    When I retrieve KB for "breakfast hours" with category_hint = "food_beverage"
    Then every returned chunk has category in {"food_beverage","overview"}

  Scenario: Empty query is rejected without calling Qdrant
    When I call the retriever with hotel_id "rixos_premium_belek" and query ""
    Then the retriever returns 0 chunks
    And the result reason is "empty_query"

  Scenario: Backend error degrades gracefully
    Given Qdrant returns HTTP 500 on the next search call
    When I retrieve KB for any valid query
    Then the retriever returns 0 chunks
    And the result reason is "retriever_error"
    And no exception bubbles to the caller
```

## 4. Decision Contract (Output Schema)

```json
{
  "hotel_id":     "rixos_premium_belek",
  "query":        "is there a water park",
  "normalized_query": "is there a water park",
  "top_score":    0.873,
  "count":        3,
  "chunks": [
    {
      "chunk_id":    "rixos_premium_belek::activities::3",
      "score":       0.873,
      "category":    "activities",
      "text":        "...",
      "text_en":     "...",
      "activity_tags": ["water_park", "kids_club"]
    }
  ],
  "reason":       null
}
```

- `count == 0` always carries a non-null `reason` from:
  `empty_query`, `no_hotel_scope`, `no_match_above_threshold`, `retriever_error`.
- `top_score` is `0.0` when `count == 0`.
- `chunks` are sorted by `score` desc.

## 5. Configuration Surface

Read from environment (re-use existing `.env` keys where possible):

| Var | Default | Purpose |
|-----|---------|---------|
| `RAG_TOP_K` | 3 | Max chunks per turn (already in `.env`) |
| `RAG_MIN_SCORE` | 0.25 | Floor (already in `.env`); chunks below are dropped |
| `QDRANT_URL` | http://138.197.142.222:6333 | Qdrant base URL |
| `QDRANT_API_KEY` | "" | Optional auth |

No new env vars introduced — Phase 2 honors the existing RAG knobs.

## 6. Non-Functional Targets

- Retrieval latency p50 < 150 ms (single hotel scope, top_k=3, e5-large query already embedded by warm model).
- Embedding latency p50 < 100 ms after model warm-up (deferred-load on first call).
- Zero cross-hotel leakage — enforced by Qdrant `must` filter on `hotel_id`.

## 7. Definition of Done

Phase 2a is closed when:
1. `HotelKBRetriever` exists with the decision contract above and passes the full unit suite.
2. Mock-Qdrant smoke harness reproduces the Gherkin scenarios end-to-end.
3. A thin `GET /call_center/api/kb?hotel_id=...&q=...` endpoint exists in `server.py` (logic stays out of server).
4. `docs/call-center/phase2a-test-report.md` is written and Stage Tracker (in the dev plan) is all Done.
5. Live-Qdrant smoke is either green or explicitly deferred in the remaining-work doc.
6. Merged into `develop`.

This closes **only 2a**. Phase 2 as a whole is closed when 2b–2f also merge and the umbrella exit criteria in the canonical dev plan are met.
