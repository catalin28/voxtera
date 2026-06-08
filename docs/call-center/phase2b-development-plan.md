# Phase 2b — Broad Hotel Discovery — Development Plan

Story: [VOX-RAG-P2B-001](phase2b-user-story.md)
Branch: feat/VOX-rag-broad
Depends on: Phase 2a (`HotelKBRetriever`) merged into `develop`
Architecture: [Voxtera_RAG_Architecture_v0.3.md](Voxtera_RAG_Architecture_v0.3.md) §6 Path 2 (Broad / Cross-Hotel)
Umbrella plan: [Voxtera_RAG_Development_Plan.md §Phase 2](Voxtera_RAG_Development_Plan.md) — 2b is search mode #2 of 6

---

## 1. Goal

Given a `region` (e.g. "antalya") and a free-form intent query, return the top N **hotels** (not chunks) sorted by best-supporting-chunk score, each with one evidence chunk. No `hotel_id` is required.

**Out of scope for 2b:** compound AND (2c), price/star/board filters (2d), dual-index hybrid (2e), ingestion (2f), chat-pipeline integration (post-Phase 5).

## 2. Architectural Boundary

- `src/voxtera/call_center/discovery.py` (NEW) — `BroadHotelDiscovery` class. Sibling of `HotelKBRetriever`, reuses `kb_config` defaults and `embeddings.embed_query`.
- `src/voxtera/call_center/kb_config.py` — add `DEFAULT_MAX_HOTELS = 5` and `DISCOVERY_OVERSHOOT_MULT = 6` (we need many raw hits to aggregate into N distinct hotels).
- `src/voxtera/call_center/server.py` — add **one** thin handler `GET /call_center/api/kb/discover`. No business logic added.
- `tests/call_center/test_broad_discovery.py` (NEW) — unit suite using injectable `search_fn`.
- `scripts/smoke_broad_discovery.py` (NEW) — mock-Qdrant smoke harness.
- `docs/call-center/phase2b-test-report.md` (NEW at end of sub-phase).
- `docs/call-center/phase2b-remaining-work.md` (NEW at end of sub-phase if anything deferred).

The existing Qdrant ingestion path (`/api/qdrant/load`) already writes `region`, `activity_tags`, `price_tier`, `country`, `district` into each point's payload — no ingestion changes are required.

## 3. Public API (Contract)

```python
class BroadHotelDiscovery:
    """Cross-hotel semantic discovery scoped by region.

    All parameters are dependency-injected so the class is testable
    without a live Qdrant or a loaded embedding model.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        collection: str = "hotel_kb",
        max_hotels: int = 5,
        min_score: float = 0.25,
        overshoot_mult: int = 6,
        embed_fn: Callable[[str], Awaitable[list[float]]] | None = None,
        search_fn: Callable[[list[float], dict, int], Awaitable[list[dict]]] | None = None,
    ) -> None: ...

    async def discover(
        self,
        *,
        region: str,
        query: str,
        activity_tags: list[str] | None = None,
        category_hint: str | None = None,
    ) -> dict[str, Any]:
        """Return the contract documented in user story §3."""
```

`overshoot_mult` controls how many raw points we ask Qdrant for: `limit = max_hotels * overshoot_mult`. We need enough raw chunks to find `max_hotels` distinct hotels after aggregation even when one hotel dominates the top hits.

## 4. Algorithm

1. Validate inputs:
   - `region.strip()` empty → `{count: 0, reason: "no_region_scope"}`.
   - normalized `query` empty → `{count: 0, reason: "empty_query"}`.
2. Embed query with `query: ` prefix.
3. Build Qdrant search body:
   ```json
   {
     "vector": [...],
     "limit": max_hotels * overshoot_mult,
     "with_payload": true,
     "filter": {
       "must": [
         {"key": "region", "match": {"value": "<region>"}}
       ]
     }
   }
   ```
   - If `activity_tags` provided → append `{"key": "activity_tags", "match": {"any": activity_tags}}` to `must`.
   - If `category_hint` provided → append `{"key": "category", "match": {"any": [hint, "overview"]}}` to `must`.
4. POST to `/collections/{collection}/points/search`.
5. Aggregate hits by `payload.hotel_id`:
   - For each hotel keep the single best-scoring chunk (≥ `min_score`).
   - Drop hotels whose best chunk falls below `min_score`.
6. Sort hotels by score desc; take top `max_hotels`.
7. If empty → `{count: 0, reason: "no_match_above_threshold", top_score: <best_below_or_0>}`.
8. Otherwise → `{count: N, hotels: [...], top_score: hotels[0].score, reason: null}`.
9. Wrap steps 2–8 in try/except. On any backend exception → `{count: 0, reason: "retriever_error"}` and log via loguru.

## 5. Test Plan

All tests use injected `embed_fn` returning a deterministic vector and an injected `search_fn` returning crafted Qdrant hit shapes.

| # | Test | Asserts |
|---|------|---------|
| 1 | empty region | `count==0`, `reason=="no_region_scope"`, no embed/search call |
| 2 | empty query | `count==0`, `reason=="empty_query"`, no embed/search call |
| 3 | happy path returns ≤ max_hotels hotels, sorted desc | distinct hotel_ids, sorted, each carries its best chunk |
| 4 | aggregation dedupes a hotel with N matching chunks | hotel appears exactly once; score == max chunk score |
| 5 | region filter present in search body | inspected `search_fn` call args |
| 6 | activity_tags appends second `must` clause | inspected `search_fn` call args |
| 7 | category_hint appends third `must` clause with `{hint, "overview"}` union | inspected `search_fn` call args |
| 8 | all chunks below min_score | `count==0`, `reason=="no_match_above_threshold"`, `top_score==best_below` |
| 9 | search backend raises | `count==0`, `reason=="retriever_error"`, exception swallowed |
| 10 | max_hotels=2 caps results even when 5 distinct hotels in hits | `count==2`, top-2 by score |
| 11 | region whitespace normalized | "  antalya  " → "antalya" in filter |
| 12 | min_score boundary | hit at exactly `min_score` is kept; hit at `min_score - 0.0001` is dropped |

## 6. Mock-Qdrant Smoke Harness

`scripts/smoke_broad_discovery.py`:
- Loads `data/seed/hotels.json`, flattens each hotel's `chunks` into pseudo-Qdrant points (deterministic synthetic embeddings; same token-overlap scorer used in 2a).
- In-memory `search_fn` applies the production filter shape (`region`, optional `activity_tags`, optional `category`).
- Smoke `min_score = 0.20` (vs. 0.25 unit) to keep the lexical heuristic within range — same calibration as 2a.

Scenario catalogue:

| Scenario | Region | Query | Tags | Expected |
|----------|--------|-------|------|----------|
| region happy path | antalya | luxury hotel with spa | — | ≥ 1 hotel, all region == antalya, top evidence in {wellness, amenities, overview} |
| activity_tags narrows | antalya | diving | ["scuba_diving"] | every hotel's payload activity_tags contains scuba_diving |
| empty region | "" | anything | — | count=0, reason=no_region_scope |
| empty query | antalya | "" | — | count=0, reason=empty_query |
| no match above threshold | antalya | xyzzy plugh zorkmid grue | — | count=0, reason=no_match_above_threshold |
| dedup aggregation | antalya | water park aquapark | — | each hotel appears exactly once |
| no region leakage | antalya | beach | — | no hotel from a different region in results |
| category_hint food_beverage | antalya | dinner buffet | — | every evidence_chunk.category in {food_beverage, overview} |

## 7. Stage Tracker Board

| Task | START | DEVELOP | FINISH | Status | Owner | Notes |
|------|-------|---------|--------|--------|-------|-------|
| Task 1: Extend `kb_config` with discovery defaults | Confirm `DEFAULT_MAX_HOTELS=5`, `DISCOVERY_OVERSHOOT_MULT=6` | Add constants; no other module changes | Existing 22 tests stay green | Done | AI + Dev | Constants added; 36/36 unit tests green |
| Task 2: Build `BroadHotelDiscovery` skeleton | Confirm public API and decision contract | Implement `__init__`, input validation paths (`no_region_scope`, `empty_query`), and graceful error path | Empty-input tests pass | Done | AI + Dev | `src/voxtera/call_center/discovery.py` |
| Task 3: Search-body builder | Confirm `must` filter composition (region + optional tags + optional category) | Implement `_build_search_body` | Filter-shape tests pass | Done | AI + Dev | region + activity_tags any-of + category any-of[hint, overview] |
| Task 4: Hotel aggregation + top-N + threshold | Confirm "max chunk score per hotel" semantics and overshoot math | Implement `_aggregate_hits`, `_apply_threshold`, `_finalize` | Dedup + cap tests pass | Done | AI + Dev | overshoot = `max_hotels * 6` |
| Task 5: Unit tests | Finalize fixture builders | Add all 12 tests from §5 | All 12 pass locally | Done | AI + Dev | 14/14 in `tests/call_center/test_broad_discovery.py` (12 core + 2 boundary) |
| Task 6: Mock-Qdrant smoke harness | Confirm scripted catalogue | Implement `scripts/smoke_broad_discovery.py` and run | Table output matches scenarios | Done | AI + Dev | 8/8 PASS against seed |
| Task 7: Thin `/call_center/api/kb/discover` endpoint | Confirm route name and required params | Add `handle_kb_discover` calling `BroadHotelDiscovery.discover` | Endpoint registered; ≤ 12 lines; no business logic | Done | AI + Dev | 11-line handler in `server.py` |
| Task 8: phase2b-test-report.md | Confirm template (mirror 2a) | Write report with both runs | Report committed | Done | AI + Dev | `docs/call-center/phase2b-test-report.md` |
| Task 9: Live-Qdrant smoke (deferred if no creds) | Confirm `QDRANT_URL` reachable | Reload `/api/qdrant/load`, hit `/api/kb/discover` for catalogue | Decisions match mock | Deferred | Dev | Tracked in `phase2b-remaining-work.md` §1 |
| Task 10: Merge into develop | All tasks 1–9 green or deferred | `git merge --no-ff feat/VOX-rag-broad` | develop ahead by one merge commit | In Progress | Dev | No PR (solo dev) |

## 8. Task Details

### Task 1: Extend kb_config

**START.** Confirm we add `DEFAULT_MAX_HOTELS = 5` and `DISCOVERY_OVERSHOOT_MULT = 6` to `src/voxtera/call_center/kb_config.py`.

**DEVELOP.** Add the two constants. Nothing else changes.

**FINISH.** `pytest tests/call_center -q` → still 22/22 green.

### Task 2: BroadHotelDiscovery skeleton

**START.** Confirm constructor signature and that both `embed_fn` and `search_fn` are injectable (same DI pattern as `HotelKBRetriever`).

**DEVELOP.** Implement `__init__`, `_normalize_inputs`, `_empty`, and the two early-return paths (`no_region_scope`, `empty_query`). Wrap the rest in a try/except returning `retriever_error`.

**FINISH.** Tests 1, 2, 9 from §5 pass.

### Task 3: Search-body builder

**START.** Confirm Qdrant `must` filter semantics — `match.value` for single values, `match.any` for lists.

**DEVELOP.** Implement `_build_search_body`. Body always sets `with_payload: true`, `limit: max_hotels * overshoot_mult`, and includes the `region` filter; appends `activity_tags` and `category` clauses only when provided.

**FINISH.** Tests 5, 6, 7 pass via captured `search_fn` arguments.

### Task 4: Aggregation + threshold

**START.** Confirm "max chunk score per hotel" aggregation rule and that we keep the best chunk as `evidence_chunk`.

**DEVELOP.** Implement `_aggregate_hits` (dict keyed by `hotel_id`, keep highest-scoring hit), `_apply_threshold`, `_finalize` (sort desc, cap at `max_hotels`, build response dict).

**FINISH.** Tests 3, 4, 8, 10, 12 pass.

### Task 5: Unit tests

**START.** Confirm test file layout matches 2a (`tests/call_center/test_broad_discovery.py`, two TestClasses: `TestBroadDiscoveryCore` and `TestThresholdBoundaries`).

**DEVELOP.** Implement all 12 tests using deterministic `embed_fn` and a `_make_search_fn(hits)` helper recording calls (mirrors `_make_search_fn` in `test_hotel_kb_retriever.py`).

**FINISH.** `pytest tests/call_center -q` → all 34 tests green (11 Phase 1 + 11 Phase 2a + 12 Phase 2b).

### Task 6: Mock-Qdrant smoke harness

**START.** Confirm scripted catalogue from §6.

**DEVELOP.** Implement `scripts/smoke_broad_discovery.py`. Reuse the seed-loading helper pattern from `scripts/smoke_hotel_kb_retriever.py`. Filter logic must support `must.value`, `must.any` (tags), and category any-of.

**FINISH.** Smoke run prints expected results per row; summary counts match.

### Task 7: Thin `/call_center/api/kb/discover` endpoint

**START.** Confirm `GET /call_center/api/kb/discover?region=...&q=...&category=...&tags=tag1,tag2` (all but `region` + `q` optional).

**DEVELOP.** Add `handle_kb_discover` in `server.py` (≤ 12 lines): parse params, split `tags` on `,` into a list, instantiate `BroadHotelDiscovery(session=app["http_session"])`, return its dict.

**FINISH.** `curl` returns the documented JSON shape; no business logic in `handle_kb_discover`.

### Task 8: phase2b-test-report.md

**START.** Confirm template mirrors `phase2a-test-report.md`: unit results, mock smoke results, config changes, outstanding deferrals, verdict.

**DEVELOP.** Write the report.

**FINISH.** Report committed; Stage Tracker statuses updated.

### Task 9: Live-Qdrant smoke

**START.** Confirm `QDRANT_URL` reachable.

**DEVELOP.** Run `POST /call_center/api/qdrant/load` then hit `GET /call_center/api/kb/discover` for the scripted catalogue.

**FINISH.** Append §4b live-run table to `phase2b-test-report.md`. If Qdrant unavailable, write `phase2b-remaining-work.md` mirroring `phase2a-remaining-work.md`.

### Task 10: Merge

**START.** Tasks 1–9 done or deferred; tests green.

**DEVELOP.** `git checkout develop && git merge --no-ff feat/VOX-rag-broad`.

**FINISH.** develop ahead by one merge commit; no PR.

---

## 9. Acceptance Criteria for This Plan

A developer can take this plan and:
1. Land Tasks 1–5 in one commit; tests prove the contract (34/34 green).
2. Land Tasks 6–8 in one commit; mock smoke + thin endpoint + report close Phase 2b's local validation.
3. Land Task 9 either as a live-run section or a deferral doc.
4. Stage Tracker moves Not Started → In Progress → Done for each task as work proceeds.
5. Closing 2b does **not** close Phase 2 — sub-phases 2c–2f follow.
