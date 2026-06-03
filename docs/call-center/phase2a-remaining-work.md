# Phase 2a — Remaining Work

Story: VOX-RAG-P2A-001
Branch: feat/vox-kb-retrieval
Date: 2026-06-02

Phase 2a unit + mock-Qdrant suites are green (see [phase2a-test-report.md](phase2a-test-report.md)). The items below are explicitly deferred and tracked here so they are not lost when Phase 2a is merged.

## 1. Live Qdrant Integration Smoke

**Status:** ~~Deferred~~ **DONE** (chore/VOX-rag-live-smoke, 2026-06-03).

Live harness `scripts/smoke_hotel_kb_retriever_live.py` runs the 6 scenarios end-to-end
against Qdrant at `http://138.197.142.222:6333` collection `hotel_kb` (92 points, 1024-d
cosine) with the real `multilingual-e5-large` embedder. Result: **6/6 PASS**. See
`phase2a-test-report.md` §8 for the score table.

**Original deferral notes (preserved for history):**

**Why deferred:** The mock smoke harness exercises the production retriever code path with an in-memory `search_fn`; only the network backend is swapped. A live run is needed to confirm the real Qdrant server returns identical decision shapes for the same scenarios.

**Steps to execute once Qdrant is reachable:**

1. Confirm reachability:
   ```powershell
   curl http://138.197.142.222:6333/collections
   ```
2. Start the admin server locally:
   ```powershell
   .\.venv\Scripts\python.exe -m voxtera.call_center
   ```
3. Seed the `hotel_kb` collection from `data/seed/hotels.json`:
   ```powershell
   curl -X POST http://localhost:8080/call_center/api/qdrant/load
   ```
4. Re-run the 6 mock scenarios against the live endpoint:
   | Scenario | Request |
   |----------|---------|
   | scoped happy path | `GET /call_center/api/kb?hotel_id=rixos_premium_belek&q=water+park+aquapark+slides` |
   | no match above threshold | `GET /call_center/api/kb?hotel_id=rixos_premium_belek&q=xyzzy+plugh+zorkmid+grue` |
   | no hotel scope | `GET /call_center/api/kb?hotel_id=&q=anything` |
   | no cross-hotel leak | `GET /call_center/api/kb?hotel_id=maxx_royal_belek&q=water+park+aquapark` |
   | category_hint food_beverage | `GET /call_center/api/kb?hotel_id=rixos_premium_belek&q=buffet+restaurant+dinner&category=food_beverage` |
   | empty query rejected | `GET /call_center/api/kb?hotel_id=rixos_premium_belek&q=` |
5. For each response assert: `count`, `reason`, and that every `chunks[].payload.hotel_id` equals the requested `hotel_id`.
6. Append a "Live Qdrant Smoke" section to `phase2a-test-report.md` with the results.

**Exit criterion:** all 6 live scenarios match the mock results' `count` and `reason` and the no-cross-hotel-leak invariant holds.

## 2. CI Wiring for `tests/call_center/`

**Status:** Pending.

**Action:** Add `pytest tests/call_center -q` to the CI pipeline alongside the existing test invocation. Local 22/22 green confirms suite stability; CI just needs the path included.

## 3. Threshold Calibration on Real Embeddings

**Status:** **DONE** (chore/VOX-rag-live-smoke, 2026-06-03).

**Finding:** `multilingual-e5-large` produces a highly compressed cosine range
(~0.74 [CLS] floor → ~0.82 strong match). Real relevant matches scored
0.77–0.82; pure-nonsense queries (`xyzzy plugh zorkmid grue`) still scored
0.76–0.77. **Absolute thresholding cannot separate signal from noise on its
own.** Calibrated `DEFAULT_MIN_SCORE` from 0.25 → 0.70 (catches only catastrophic
failures – out-of-domain queries returning the bare [CLS] floor or embedding
errors). Real relevance filtering happens via top-K + LLM-side check on chunk
text.

**Follow-up (folded into Phase 2c):** Relative-margin filter — keep only chunks
whose score is within `RELATIVE_MARGIN` (target 0.05) of the per-query top
score. Tracked in `Voxtera_RAG_Development_Plan.md` § Phase 2c, branch
`feat/VOX-rag-compound`. A cross-encoder reranker remains future work
(post-2f).

## 4. Items Explicitly Out of Phase 2a (owned by later sub-phases)

These are not Phase 2a remaining work — they are deliberately not in scope and are listed only to prevent accidental scope creep:

| Capability | Owner sub-phase | Branch |
|------------|-----------------|--------|
| Broad / cross-hotel search | Phase 2b | `feat/VOX-rag-broad` |
| Compound-AND queries (e.g. spa + scuba) with `partial_match_only` + `missing_requirements[]` | Phase 2c | `feat/VOX-rag-compound` |
| Structured filter pre-pass (price, stars, board type) | Phase 2d | `feat/VOX-rag-filters` |
| Dual-index (Qdrant + ES) hybrid retrieval | Phase 2e | `feat/VOX-rag-dual` |
| Incremental ingestion / re-embedding pipeline | Phase 2f | `feat/VOX-rag-ingest` |

See [Voxtera_RAG_Development_Plan.md](Voxtera_RAG_Development_Plan.md) for the umbrella plan.
