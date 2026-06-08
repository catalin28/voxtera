# Phase 2a — Scoped Hotel KB Retrieval — Development Plan

Story: [VOX-RAG-P2A-001 (Scoped Hotel KB Retrieval)](phase2a-user-story.md)
Branch: feat/vox-kb-retrieval
Depends on: Phase 1 (`HotelResolver`) merged into `develop`
Architecture: [Voxtera_RAG_Architecture_v0.3.md](Voxtera_RAG_Architecture_v0.3.md) §6 Path 1 (Scoped Qdrant)
Umbrella plan: [Voxtera_RAG_Development_Plan.md §Phase 2](Voxtera_RAG_Development_Plan.md) — 2a is search mode #1 of 6

---

## 1. Goal

Given a resolved `hotel_id` (Phase 1 output) and a user query, return the top-K most relevant KB chunks for **that single hotel**, with a stable decision contract and zero cross-hotel leakage. This is the foundation every later Phase 2 sub-phase reuses (embeddings module, search-body builder, result contract).

**Out of scope for 2a:** broad discovery (2b), compound AND (2c), budget/geo filters (2d), dual comparison (2e), ingestion + confidence bands + Redis cache (2f). No chat-pipeline integration yet — that lands after Phase 5.

## 2. Architectural Boundary

- `src/voxtera/call_center/retriever.py` (NEW) — `HotelKBRetriever` class. All retrieval logic lives here.
- `src/voxtera/call_center/embeddings.py` (NEW) — thin wrapper around the e5-large encoder so the retriever and the existing ingest path share one place to load the model and apply the `query: ` / `passage: ` prefixes.
- `src/voxtera/call_center/kb_config.py` (NEW) — collection name, vector size, distance metric, default top_k/min_score, category enum. Mirrors the `index_config.py` pattern used in Phase 1.
- `src/voxtera/call_center/server.py` — adds **one** thin handler `GET /call_center/api/kb`. No business logic added to the server.
- `tests/call_center/test_hotel_kb_retriever.py` (NEW) — unit suite using an injectable `search_fn` (same pattern Phase 1 used for ES).
- `scripts/smoke_hotel_kb_retriever.py` (NEW) — mock-Qdrant smoke harness exercising the Gherkin scenarios.
- `docs/call-center/phase2a-test-report.md` (NEW) — output of the test runs.
- `docs/call-center/phase2a-remaining-work.md` (NEW at end of sub-phase) — anything deferred.

The existing `_embed_texts` / `QDRANT_COLLECTION` constants in `server.py` will be **moved** to the new modules. Server keeps only the imports and the existing thin endpoints.

## 3. Public API (Contract)

```python
class HotelKBRetriever:
    """Scoped semantic retrieval over hotel_kb in Qdrant.

    All parameters are dependency-injected so the retriever is testable
    without a live Qdrant or a loaded embedding model.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        collection: str = "hotel_kb",
        top_k: int = 3,
        min_score: float = 0.25,
        embed_fn: Callable[[str], Awaitable[list[float]]] | None = None,
        search_fn: Callable[[list[float], dict, int], Awaitable[list[dict]]] | None = None,
    ) -> None: ...

    async def retrieve(
        self,
        *,
        hotel_id: str,
        query: str,
        category_hint: str | None = None,
    ) -> dict[str, Any]:
        """Return a stable dict matching the contract in the user story §4."""
```

`embed_fn` defaults to `voxtera.call_center.embeddings.embed_query`. `search_fn` defaults to a Qdrant call via `voxtera.call_center.clients.qdrant_request`.

## 4. Algorithm

1. Validate inputs:
   - `hotel_id` empty → return `{count: 0, reason: "no_hotel_scope"}`.
   - normalized `query` empty → return `{count: 0, reason: "empty_query"}`.
2. Embed query with `query: ` prefix (e5-large convention).
3. Build Qdrant search body:
   ```json
   {
     "vector": [...],
     "limit": top_k * 2,                          // overshoot so threshold filter still produces top_k
     "with_payload": true,
     "filter": {
       "must": [
         {"key": "hotel_id", "match": {"value": "<hotel_id>"}}
       ]
     }
   }
   ```
   If `category_hint` is provided, append `{"key": "category", "match": {"any": [hint, "overview"]}}` to `must`.
4. POST to `/collections/{collection}/points/search`.
5. Parse hits → drop those with `score < min_score` → keep top `top_k`.
6. If filtered list empty → `{count: 0, reason: "no_match_above_threshold", top_score: <best_below>}`.
7. Otherwise → `{count: N, chunks: [...], top_score: chunks[0].score, reason: null}`.
8. Wrap steps 2–7 in a try/except. On any backend exception → `{count: 0, reason: "retriever_error"}` and log via loguru.

## 5. Test Plan

Mirrors the Gherkin scenarios in the user story. All tests use injected `embed_fn` returning a deterministic vector and an injected `search_fn` returning crafted Qdrant hit shapes.

| # | Test | Asserts |
|---|------|---------|
| 1 | empty hotel_id | `count==0`, `reason=="no_hotel_scope"`, no embed/search call |
| 2 | empty query | `count==0`, `reason=="empty_query"`, no embed/search call |
| 3 | happy path returns top_k | `count==top_k`, chunks sorted desc, every `hotel_id` matches scope |
| 4 | all chunks below min_score | `count==0`, `reason=="no_match_above_threshold"`, `top_score` reflects best filtered-out |
| 5 | category_hint adds filter | search_fn receives filter with `category` clause including hint + "overview" |
| 6 | search backend raises | `count==0`, `reason=="retriever_error"`, exception swallowed |
| 7 | top_k=1 caps results even when 5 hits returned | `count==1` |
| 8 | hotel_id whitespace is normalized | "  rixos_premium_belek  " becomes "rixos_premium_belek" in the filter |
| 9 | min_score boundary | hit at exactly `min_score` is kept; hit at `min_score - 0.0001` is dropped |
| 10 | response payload shape | result dict contains every documented key (smoke against the contract) |

## 6. Mock-Qdrant Smoke Harness

`scripts/smoke_hotel_kb_retriever.py`:
- Loads `data/seed/hotels.json` and flattens each hotel's `chunks` into pseudo-Qdrant points with synthetic embeddings (deterministic per `(hotel_id, category, idx)`).
- For each scripted (hotel_id, query, expected_decision) tuple, runs `HotelKBRetriever.retrieve(...)` against an in-memory `search_fn` that scores by token overlap on `text` + `text_en`.
- Prints a results table identical in spirit to Phase 1's `smoke_hotel_resolver.py`.

Mention catalogue (mirrors Gherkin scenarios):

| Hotel | Query | Expected |
|-------|-------|----------|
| rixos_premium_belek | water park | chunks > 0, category in {activities, amenities, overview} |
| rixos_premium_belek | dogecoin payment | count == 0, reason == no_match_above_threshold |
| "" | water park | count == 0, reason == no_hotel_scope |
| maxx_royal_belek | water park | every chunk hotel_id == maxx_royal_belek (no rixos bleed) |
| rixos_premium_belek | breakfast hours (hint=food_beverage) | category in {food_beverage, overview} |
| rixos_premium_belek | "" | count == 0, reason == empty_query |

## 7. Stage Tracker Board

| Task | START | DEVELOP | FINISH | Status | Owner | Notes |
|------|-------|---------|--------|--------|-------|-------|
| Task 1: Extract embeddings + kb_config modules | Confirm we move `_embed_texts` and `QDRANT_COLLECTION` out of server.py | Implement `embeddings.py` (warm-on-first-call, `query`/`passage` prefixes) and `kb_config.py` (collection name, defaults, category enum) | Server.py imports both; existing `/api/qdrant/*` endpoints still work | Done | AI + Dev | Phase 1 tests stayed green after extraction (11/11) |
| Task 2: Build `HotelKBRetriever` skeleton | Confirm public API and decision contract | Implement input validation paths (`no_hotel_scope`, `empty_query`) and graceful error path | Empty-input tests pass | Done | AI + Dev | Module `kb_retriever.py` with injectable embed_fn/search_fn |
| Task 3: Implement query embedding + Qdrant search body | Confirm `must` filter + payload returns | Implement `_embed`, `_build_search_body`, `_search` | Search-body shape test passes | Done | AI + Dev | `limit = top_k * 2` overshoot |
| Task 4: Implement scoring + threshold + result assembly | Confirm overshoot/limit math and reason strings | Implement `_parse_hits`, `_apply_threshold`, `_finalize` | Threshold boundary tests pass at min_score ± 0.0001 | Done | AI + Dev | Boundary tests at 0.25 / 0.24 green |
| Task 5: Category_hint optional filter | Confirm "hint + overview" union semantics | Append second `must` clause when hint provided | Test 5 passes | Done | AI + Dev | `match.any = [hint, "overview"]` |
| Task 6: Add unit tests | Finalize fixture builders | Add all tests from §5 | All pass locally | Done | AI + Dev | 11/11 Phase 2a tests green |
| Task 7: Mock-Qdrant smoke harness | Confirm scripted catalogue | Implement `scripts/smoke_hotel_kb_retriever.py` and run | Table output matches Gherkin expectations | Done | AI + Dev | 6/6 scenarios PASS over 92 seed chunks |
| Task 8: Thin `/call_center/api/kb` endpoint | Confirm route name and required params | Add `handle_kb` calling `HotelKBRetriever.retrieve` | Endpoint registered; no business logic in server.py | Done | AI + Dev | 8-line handler |
| Task 9: Produce phase2a-test-report.md | Confirm template | Write report with both runs | Report committed | Done | AI + Dev | See [phase2a-test-report.md](phase2a-test-report.md) |
| Task 10: Live-Qdrant smoke (deferred if no creds) | Confirm `QDRANT_URL` reachable | Reload `/api/qdrant/load`, hit `/api/kb` for scripted queries | Decisions match mock | Deferred | Dev | Tracked in [phase2a-remaining-work.md](phase2a-remaining-work.md) §1 |

## 8. Task Details

### Task 1: Extract embeddings + kb_config modules

**START.** Confirm `_embed_texts`, `_get_model`, `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `PREFIX_PASSAGE`, `PREFIX_QUERY`, and `QDRANT_COLLECTION` are moved out of `server.py`. Confirm `embeddings.py` exposes `embed_query(text) -> list[float]` and `embed_passages(texts) -> list[list[float]]` as the public API.

**DEVELOP.** Create the two modules; rewrite `server.py` to import them. Verify all existing `/api/qdrant/*` endpoints behave identically (`load`, `collections`, `points`, `search`).

**FINISH.** Existing qdrant endpoints still respond with the same shapes; resolver tests still pass; no logic in `server.py` beyond imports and HTTP wiring.

### Task 2: Build HotelKBRetriever skeleton

**START.** Confirm constructor signature and that both `embed_fn` and `search_fn` are injectable (mirrors Phase 1's `search_fn` for ES). Confirm decision contract from user story §4.

**DEVELOP.** Implement `__init__`, `_normalize_inputs`, `_result(...)`, and the two early-return paths (`no_hotel_scope`, `empty_query`).

**FINISH.** Tests 1, 2, 6 from §5 pass; the retriever module imports cleanly.

### Task 3: Implement query embedding + Qdrant search body

**START.** Confirm Qdrant `must` filter semantics for `hotel_id` (`match.value`).

**DEVELOP.** Implement `_embed`, `_build_search_body`, `_search`. The body always sets `with_payload: true`, `limit: top_k * 2`, and includes the `hotel_id` filter; category hint adds a second clause only when provided.

**FINISH.** A test inspecting the captured `search_body` confirms its shape exactly.

### Task 4: Implement scoring, threshold, result assembly

**START.** Confirm "overshoot then filter" approach (we fetch `top_k * 2` and filter; this gives the threshold cut room to still produce up to `top_k`).

**DEVELOP.** Implement `_parse_hits`, `_apply_threshold`, and `_finalize`. When the filtered list is empty, set `reason = "no_match_above_threshold"` and `top_score = best_seen_below_threshold` so callers can debug.

**FINISH.** Tests 4, 7, 9 from §5 pass.

### Task 5: Category hint

**START.** Confirm hint semantics: `{hint, "overview"}` union — never exclusive — to avoid starving the LLM of context.

**DEVELOP.** Conditional append to `must` clauses.

**FINISH.** Test 5 passes.

### Task 6: Unit tests

**START.** Confirm test file layout matches Phase 1 (`tests/call_center/test_hotel_kb_retriever.py`, two TestClasses: `TestHotelKBRetrieverCore` and `TestThresholdBoundaries`).

**DEVELOP.** Implement all 10 tests using deterministic `embed_fn` and crafted `search_fn`.

**FINISH.** `pytest tests/call_center/test_hotel_kb_retriever.py -q` → all green.

### Task 7: Mock-Qdrant smoke harness

**START.** Confirm scripted catalogue from §6.

**DEVELOP.** Implement `scripts/smoke_hotel_kb_retriever.py`; flatten seed `chunks` into pseudo-points; deterministic token-overlap scorer; tabular output.

**FINISH.** Smoke run prints expected decision per row; summary counts match.

### Task 8: Thin `/call_center/api/kb` endpoint

**START.** Confirm `GET /call_center/api/kb?hotel_id=...&q=...&category=...` (`category` optional).

**DEVELOP.** Add `handle_kb` in `server.py` (≤ 10 lines): parse params, instantiate `HotelKBRetriever(session=app["http_session"])`, return its dict. Register the route.

**FINISH.** `curl` returns the documented JSON shape; no business logic in `handle_kb`.

### Task 9: phase2a-test-report.md

**START.** Confirm template (mirror Phase 1's): unit results, mock smoke results, index/config changes summary, outstanding deferrals, verdict.

**DEVELOP.** Write the report.

**FINISH.** Report committed; Stage Tracker statuses updated.

### Task 10: Live-Qdrant smoke

**START.** Confirm `QDRANT_URL` and (optional) `QDRANT_API_KEY` work via `curl $QDRANT_URL/collections`.

**DEVELOP.** Run `POST /call_center/api/qdrant/load` (existing endpoint) to embed + upsert seed chunks; then hit `GET /call_center/api/kb` for the scripted catalogue.

**FINISH.** Append a §4b live-run table to `phase2a-test-report.md`. If Qdrant unavailable, write `phase2a-remaining-work.md` with the same content as Phase 1's remaining-work doc.

---

## 9. Acceptance Criteria for This Plan

A developer can take this plan and:
1. Land Task 1 in one PR-sized commit without changing any public behavior of `/api/qdrant/*`.
2. Land Tasks 2–6 in one commit; tests prove the contract.
3. Land Tasks 7–9 in one commit; mock smoke + thin endpoint + report close Phase 2a.
4. Status in the Stage Tracker Board moves Not Started → In Progress → Done for each task as work proceeds.
5. Closing 2a does **not** close Phase 2 — sub-phases 2b–2f follow (see umbrella plan).
