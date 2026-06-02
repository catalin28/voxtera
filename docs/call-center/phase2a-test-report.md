# Phase 2a — Scoped Hotel KB Retriever Test Report

Story: VOX-RAG-P2A-001 (single-hotel scoped chunk retrieval)
Branch: feat/vox-kb-retrieval
Date: 2026-06-02

## 1. Scope

Validates that `HotelKBRetriever` (`src/voxtera/call_center/kb_retriever.py`)
returns hotel-scoped chunks from Qdrant under the Phase 2a decision contract,
that the shared embeddings + KB config modules are reused by both the retriever
and the ingestion-facing server endpoints, and that the admin server gains a
thin `/call_center/api/kb` surface without leaking business logic.

Decision contract (Phase 2a):
- empty query → `count=0`, `reason="empty_query"`
- missing `hotel_id` → `count=0`, `reason="no_hotel_scope"`
- no hit ≥ `min_score` (0.25) → `count=0`, `reason="no_match_above_threshold"`, `top_score=<best_below>`
- network/embedding failure → `count=0`, `reason="retriever_error"`
- otherwise → top-K (≤3) chunks sorted desc, `reason=None`

Hard invariant: every returned chunk's payload `hotel_id` equals the requested
`hotel_id` (no cross-hotel leakage).

## 2. Artifacts Under Test

- `src/voxtera/call_center/kb_retriever.py` — `HotelKBRetriever`
- `src/voxtera/call_center/kb_config.py` — `QDRANT_COLLECTION`, `EMBEDDING_DIM`, `DISTANCE`, `DEFAULT_TOP_K`, `DEFAULT_MIN_SCORE`, `CATEGORIES`
- `src/voxtera/call_center/embeddings.py` — shared `embed_query`, `embed_passages`, `embed_texts`, e5-large lazy loader
- `src/voxtera/call_center/server.py` — thin `/call_center/api/kb` handler + existing `/api/qdrant/*` now routed through the shared embeddings module
- `tests/call_center/test_hotel_kb_retriever.py` — unit suite
- `scripts/smoke_hotel_kb_retriever.py` — mock-Qdrant smoke harness over `data/seed/hotels.json`

## 3. Unit Test Results

Command:
```
.\.venv\Scripts\python.exe -m pytest tests/call_center -q
```

Result: **22 passed in 0.30s** (11 Phase 1 + 11 Phase 2a — no Phase 1 regressions from the embeddings/kb_config extraction).

Phase 2a coverage:
| Group | Cases | Result |
|-------|-------|--------|
| TestHotelKBRetrieverCore | empty query, missing hotel_id, scoped happy path, top-K cap, sort order, payload extraction, category_hint filter, no_match floor, error degradation | 9/9 pass |
| TestThresholdBoundaries (parametrized) | score=0.25 (boundary kept), score=0.24 (boundary rejected) | 2/2 pass |
| **Total** | | **11/11 pass** |

## 4. Integration Smoke Test (Mock Qdrant)

Live Qdrant credentials were unavailable, so the smoke harness loads
`data/seed/hotels.json` (11 hotels, 92 chunks), builds an in-memory `search_fn`
that mimics Qdrant point shapes (`{id, score, payload}`), applies the same
`hotel_id` + optional `category` filter the retriever sends, and scores each
candidate by lexical token overlap with the query (>2-char alphanumeric tokens).
The retriever code path exercised is identical to production — only the vector
backend is swapped. Smoke `min_score=0.20` (vs. 0.25 in unit tests) to keep the
lexical heuristic within range.

Command:
```
.\.venv\Scripts\python.exe scripts\smoke_hotel_kb_retriever.py
```

Result:
| Scenario | Hotel | Expected count | Expected reason | Got | Verdict |
|----------|-------|----------------|-----------------|-----|---------|
| scoped happy path (`water park aquapark slides`) | rixos_premium_belek | 3 | — | 3 | PASS |
| no match above threshold (`xyzzy plugh zorkmid grue`) | rixos_premium_belek | 0 | no_match_above_threshold | 0 | PASS |
| no hotel scope (empty hotel_id) | — | 0 | no_hotel_scope | 0 | PASS |
| no cross-hotel leak (`water park aquapark`) | maxx_royal_belek | ≥1, all payload hotel_id == maxx_royal_belek | — | 2 | PASS |
| category_hint food_beverage (`buffet restaurant dinner`) | rixos_premium_belek | ≥1 chunks all in {food_beverage, overview} | — | 2 | PASS |
| empty query rejected | rixos_premium_belek | 0 | empty_query | 0 | PASS |

Summary: **6/6 scenarios pass**. The cross-hotel scenario also asserts every returned chunk's payload `hotel_id` matches the request — invariant held.

## 5. Module Layout Changes (Task 1)

Moved shared retrieval primitives out of `server.py`:
- `src/voxtera/call_center/kb_config.py` — single source of truth for Qdrant collection name, vector dimension, distance, top-K / min-score defaults, and the chunk category enum.
- `src/voxtera/call_center/embeddings.py` — shared e5-large wrapper with lazy model load and `query: ` / `passage: ` prefix helpers. Both the retriever and the existing `/api/qdrant/load` ingestion path now consume the same `embed_texts` function — no duplicated model loading.

`server.py` retained: existing `/api/qdrant/load|collections|points|search` endpoints (now importing the shared helpers) plus the new `/call_center/api/kb` 8-line handler. No business logic added to the admin server.

## 6. Outstanding / Deferred

| Item | Status | Notes |
|------|--------|-------|
| Live Qdrant integration smoke | Deferred | Needs reachable `http://138.197.142.222:6333` and seeded `hotel_kb` collection. Run `POST /call_center/api/qdrant/load` then `GET /call_center/api/kb?hotel_id=...&q=...`. Tracked in `docs/call-center/phase2a-remaining-work.md`. |
| CI run for `tests/call_center/` | Pending | Local 22/22 green; CI invocation still not wired. |
| Compound-AND queries (spa + scuba) | Out of Phase 2a | Owned by Phase 2c (`feat/VOX-rag-compound`) per umbrella plan. |
| Broad / cross-hotel search | Out of Phase 2a | Phase 2b (`feat/VOX-rag-broad`). |
| Structured filter pre-pass | Out of Phase 2a | Phase 2d (`feat/VOX-rag-filters`). |

## 7. Verdict

Phase 2a acceptance criteria (decision contract, top-K cap, threshold floor, hotel-scope invariant, category hint, thin server surface, shared embeddings/config) are met under unit tests and mock-Qdrant integration. The retriever will run unchanged against live Qdrant once the collection is reachable — only the search backend swaps.
