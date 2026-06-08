# Phase 1 — Remaining Work

Story: VOX-RAG-P1-001
Branch: feat/vox-hotel-resolver
Date: 2026-06-02

## TL;DR

All code for Phase 1 is written and verified (11/11 unit tests, 11/11 mock-ES smoke cases). What's left is **environment + verification work**, not code work.

## Status Snapshot

| Area | State | Notes |
|------|-------|-------|
| Resolver implementation | Done | [src/voxtera/call_center/resolver.py](../../src/voxtera/call_center/resolver.py) |
| Index/analyzer config | Done | [src/voxtera/call_center/index_config.py](../../src/voxtera/call_center/index_config.py) |
| Thin admin endpoint | Done | `GET /call_center/api/resolve` in [server.py](../../src/voxtera/call_center/server.py) |
| Unit tests | Done (local) | 11/11 via `pytest tests/call_center/test_hotel_resolver.py` |
| Mock-ES smoke test | Done | [scripts/smoke_hotel_resolver.py](../../scripts/smoke_hotel_resolver.py) |
| Test report | Done | [docs/call-center/phase1-test-report.md](phase1-test-report.md) |
| Live-ES smoke | **Pending** | Needs ES credentials |
| CI integration | **Pending** | Test suite not yet wired into CI |
| Branch merge to develop | **Pending** | After live-ES smoke passes |

## What's Left (Ordered)

### 1. Provide Elasticsearch connectivity — Owner: Dan
Add the following to `.env`:
```
ELASTICSEARCH_URL=https://<host>:9200
ELASTICSEARCH_USER=elastic
ELASTICSEARCH_PASSWORD=<password>
```
Acceptable sources:
- Existing droplet at `138.197.142.222:9200` (whoever provisioned it has the password).
- Elastic Cloud free trial (https://cloud.elastic.co).
- Local docker: `docker run -p 9200:9200 -e discovery.type=single-node -e xpack.security.enabled=false elasticsearch:8.19.16`.

**Exit criterion:** `curl -u elastic:$ELASTICSEARCH_PASSWORD $ELASTICSEARCH_URL/` returns a JSON cluster banner.

### 2. Load seed hotels into the live index — Owner: Dev
```
.\.venv\Scripts\python.exe -m voxtera.call_center.server
# in a second terminal:
curl.exe -X POST http://127.0.0.1:8100/call_center/api/es/load
```
**Exit criterion:** response shows `{"indexed": 10, "total": 10, "errors": []}`.

### 3. Run live-ES smoke test — Owner: Dev
Hit `/call_center/api/resolve` with the same mention catalogue used by the mock smoke test and capture results. Suggested invocation:
```
$mentions = @(
  "Rixos Premium Belek",
  "Riksos Premium Belek",
  "Rixos Land of Legends",
  "Maxx Royal",
  "Cornelia",
  "Hilton",
  "Belek otel",
  "Quantum Sparkle Resort"
)
foreach ($q in $mentions) {
  curl.exe -s "http://127.0.0.1:8100/call_center/api/resolve?q=$([uri]::EscapeDataString($q))"
  Write-Host ""
}
```
**Exit criterion:** decisions match the table in [phase1-test-report.md §4](phase1-test-report.md). Differences are expected only on absolute scores (BM25 vs heuristic) — the *decision branches* must match.

### 4. Append live-ES results to the test report — Owner: AI + Dev
Add a §4b table to `phase1-test-report.md` with the live scores and decisions side-by-side with the mock results.

**Exit criterion:** report shows both runs; any decision-branch mismatches are documented with root cause.

### 5. Wire the unit suite into CI — Owner: Dev
Add `tests/call_center/` to the existing pytest invocation in CI (or extend the matrix if call-center tests need different env vars — currently they need none).

**Exit criterion:** CI run on `feat/vox-hotel-resolver` shows the 11 resolver tests passing.

### 6. Merge to `develop` — Owner: Dev
Open a PR from `feat/vox-hotel-resolver` → `develop`. Checklist for the PR description:
- Link to [phase1-user-story.md](phase1-user-story.md).
- Link to [phase1-test-report.md](phase1-test-report.md) with live-ES results appended.
- Confirm CI is green.
- Confirm Stage Tracker in [phase1-development-plan.md](phase1-development-plan.md) is all Done.

**Exit criterion:** PR approved and merged; branch deleted.

## Out of Scope for Phase 1 (Deferred)

These are intentionally **not** in Phase 1's remaining work — they belong to later phases or to the optimisation backlog:

- Chat-pipeline integration (resolver wired into the live call/chat flow) — Phase 2.
- Qdrant semantic retrieval on the resolved `hotel_id` — Phase 2.
- Phonetic analyzer and edge-ngram fields — [docs/call-center/elasticsearch-optimisation.md](elasticsearch-optimisation.md).
- Resolver telemetry / metrics emission — Phase 2 observability.
- Multi-language alias expansion beyond Turkish — future.

## Rough Order-of-Operations Risk

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Live BM25 scores cross a threshold differently than the heuristic | Medium | Thresholds (0.85 / 0.55) are tunable constants on `HotelResolver`; adjust if a clear class of mentions misroutes during step 3. |
| ES 8.x rejects `synonym_graph` at index time | Low | Filter is search-time only; if a future change moves it to index time, the index reload in step 2 will fail loudly. |
| CI lacks aiohttp/loguru | Low | Both already in `pyproject.toml`; CI installs from there. |

## Definition of Done for Phase 1

Phase 1 is closed when:
1. Steps 1–6 above are complete.
2. `phase1-test-report.md` shows live-ES decisions matching the mock for all mention classes.
3. PR is merged into `develop`.
