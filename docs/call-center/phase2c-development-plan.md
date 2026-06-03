# Phase 2c — Development Plan

**Ticket:** VOX-RAG-P2C-001
**Branch:** `feat/VOX-rag-compound`
**Status at writing:** implemented + smoked, ready for merge.

---

## 1. What this phase delivers

| Deliverable | File |
|---|---|
| `CompoundAndDiscovery` class | [src/voxtera/call_center/compound.py](../../src/voxtera/call_center/compound.py) |
| `RELATIVE_MARGIN` constant + `DEFAULT_MAX_REQUIREMENTS` | [src/voxtera/call_center/kb_config.py](../../src/voxtera/call_center/kb_config.py) |
| Margin filter inside `HotelKBRetriever._finalize` | [src/voxtera/call_center/kb_retriever.py](../../src/voxtera/call_center/kb_retriever.py) |
| Margin filter inside `BroadHotelDiscovery._finalize` | [src/voxtera/call_center/discovery.py](../../src/voxtera/call_center/discovery.py) |
| `GET /call_center/api/kb/compound` route + thin handler | [src/voxtera/call_center/server.py](../../src/voxtera/call_center/server.py) |
| 14 compound unit tests + margin tests | [tests/call_center/test_compound_discovery.py](../../tests/call_center/test_compound_discovery.py) |
| Updated 2a/2b unit tests (tightened score spreads) | [tests/call_center/test_hotel_kb_retriever.py](../../tests/call_center/test_hotel_kb_retriever.py), [tests/call_center/test_broad_discovery.py](../../tests/call_center/test_broad_discovery.py) |
| Mock smoke harness | [scripts/smoke_compound_discovery.py](../../scripts/smoke_compound_discovery.py) |
| Live smoke harness | [scripts/smoke_compound_discovery_live.py](../../scripts/smoke_compound_discovery_live.py) |
| User story | [phase2c-user-story.md](phase2c-user-story.md) |
| Test report | [phase2c-test-report.md](phase2c-test-report.md) |
| Remaining work | [phase2c-remaining-work.md](phase2c-remaining-work.md) |

## 2. Design decisions

### 2.1 Why fan out N `BroadHotelDiscovery` calls in parallel?

- Each requirement is a fully independent semantic query — there's no
  shared state to mutate.
- `asyncio.gather` makes the total wall-clock time `max(per_req_latency)`
  rather than `sum(per_req_latency)`.
- Re-using `BroadHotelDiscovery` means compound automatically inherits
  margin filtering, region scoping, category hints, activity-tag
  filters, error handling — everything Phase 2b already proved.

### 2.2 Why a set-based hotel-id intersection (vs. score aggregation)?

A guest asking *"spa AND scuba diving"* fails fast if no hotel has
both: returning a hotel that scores well on spa but has nothing for
scuba is worse than admitting partial match. The set-intersect
semantics make the answer trustworthy and easy to render in the UI.

### 2.3 Why graceful degradation by dropping the smallest-set requirement?

When the strict intersection is empty, dropping the requirement that
contributed the fewest candidate hotels is the safest heuristic: it's
the requirement most likely to be either niche or under-covered in the
corpus, and dropping it gives the largest chance of recovering a
useful answer. Each dropped requirement is logged in
`missing_requirements` so the front-end can be honest about the gap.

### 2.4 Why apply `RELATIVE_MARGIN` silently (no new reason)?

The relative-margin filter is **trim-only** and the top chunk is
always within the margin of itself. So:

- A non-empty result can never become empty because of the margin.
- The margin can never *introduce* a new failure mode.

A separate `reason: "below_relative_margin"` was on an earlier draft of
the plan but was removed once we realised it would be unreachable.

### 2.5 Why `RELATIVE_MARGIN = 0.05`?

From live Phase 2a/2b distributions:

| Query type | Top score | Tail (3rd) | Gap |
|---|---|---|---|
| Real, well-aligned (e.g. "water park") | 0.82 | 0.78 | 0.04 |
| Real, broad (e.g. "buffet restaurant dinner") | 0.80 | 0.77 | 0.03 |
| Junk (e.g. "xyzzy plugh") | 0.76 | 0.75 | 0.01 |

`0.05` keeps the legitimate top-3 cluster intact for real queries
while trimming hotels that are clearly a tier below the top, without
introducing a value that needs per-query tuning.

### 2.6 Why `DEFAULT_MAX_REQUIREMENTS = 5`?

Three reasons:

1. **Latency budget:** five parallel `BroadHotelDiscovery` calls
   complete inside the 600 ms p95 budget on the live cluster (single
   call ~80–120 ms with e5 embedding pre-warmed).
2. **Combinatorial sanity:** the probability of finding a hotel that
   strictly satisfies *N* free-form requirements drops off fast above
   5; beyond that the partial-match path dominates anyway.
3. **Surface attack:** caps the worst-case fan-out a single
   query-string can trigger.

## 3. Acceptance criteria (final, revised)

1. **3 strict compound queries** return ≥ 1 hotel with evidence for
   every requirement (live).
2. **Graceful degradation** returns `partial_match_only` with a
   populated `missing_requirements` list when the strict intersection
   is empty AND at least one requirement still matches (mock-verified;
   live e5 junk-overlap limitation documented below).
3. **`RELATIVE_MARGIN = 0.05`** is justified by Phase 2a/2b live
   score distributions (§2.5) and never zeros a non-empty result.
4. **All 50 unit tests pass** locally.
5. **Re-running Phase 2a + 2b live smokes is still 6/6 + 8/8** after
   the margin landed.

## 4. Known limitations (carried forward to Phase 3)

- **e5 junk-overlap:** absolute-threshold filtering at 0.70 cannot
  separate real hits (0.77–0.82) from junk (0.74–0.79). This means
  compound intersections across nonsense requirements may still return
  hotels with `reason: null`. See
  [phase2c-remaining-work.md](phase2c-remaining-work.md) for mitigation
  options.
- **No cross-encoder re-rank** — would significantly tighten the
  real-vs-junk gap but adds ~150 ms latency per requirement.
- **Drop heuristic is "smallest set first"** — for some failure
  patterns "lowest top-score first" would be better; deferred.
