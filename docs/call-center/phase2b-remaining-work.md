# Phase 2b — Remaining Work

Companion to [phase2b-test-report.md](phase2b-test-report.md). Captures items
that were intentionally deferred so Phase 2b could close on a green commit and
the team could move on to Phase 2c (compound-AND).

## 1. Live Qdrant integration smoke

The mock smoke harness covers the production code path but swaps the
vector backend. Before declaring "ship", a one-shot run against
`http://138.197.142.222:6333` collection `hotel_kb` should be performed.

Repro (when credentials and a populated collection are available):

```powershell
# 1. Make sure data/seed/hotels.json has been ingested into the collection
.\.venv\Scripts\python.exe -m scripts.seed_qdrant_hotels  # (or POST /api/qdrant/load)

# 2. Run a real round-trip against the same 8 scenarios via the HTTP surface
$env:VOXTERA_KB_SMOKE_LIVE = "1"
.\.venv\Scripts\python.exe scripts\smoke_broad_discovery_live.py  # (to be written)
```

The live harness still needs to be written; it should reuse the scenario
table from `scripts/smoke_broad_discovery.py` and call
`http://localhost:8085/call_center/api/kb/discover?...` instead of the
in-process `search_fn`.

## 2. CI wiring

`tests/call_center/` is not yet invoked in CI. Adding a workflow step:

```yaml
- name: Call-center KB unit tests
  run: .venv/bin/python -m pytest tests/call_center -q
```

would lock in the 36-test green bar (Phase 1 + 2a + 2b) and catch regressions
in `kb_retriever.py` / `discovery.py` early.

## 3. Threshold calibration

`DEFAULT_MIN_SCORE = 0.25` is inherited from Phase 2a and re-used by Phase 2b.
Once we have live embedding scores from `multilingual-e5-large` over the real
corpus we should re-tune separately for `HotelKBRetriever` (per-hotel chunks)
vs. `BroadHotelDiscovery` (cross-hotel hot ones tend to score lower because
of region noise). Suggested workflow: dump top-50 scores for ~20 representative
queries, plot, set threshold at the elbow.

## 4. Multi-region seed corpus

All 11 hotels in `data/seed/hotels.json` carry `region = "turkish riviera"`,
so the "no region leakage" smoke assertion is vacuously satisfied. Phase 2b
will be considered fully exercised when:

- ≥2 distinct regions exist in the seed (e.g. add 3-5 hotels for `aegean`,
  `bodrum`, or `cappadocia`)
- The "no region leakage" scenario produces ≥1 cross-region candidate that
  the filter must reject

This is a corpus-data task and not a code task — it does not change Phase 2b
semantics, only test strength.

## 5. Out of scope (covered elsewhere)

| Capability | Owning phase | Branch |
|-----------|--------------|--------|
| Compound AND across category + activity_tags + price | Phase 2c | `feat/VOX-rag-compound` |
| Structured filters (price_tier ranges, star ratings, distances) | Phase 2d | `feat/VOX-rag-filters` |
| Dual-index (Qdrant + ES BM25) hybrid retrieval | Phase 2e | `feat/VOX-rag-dual` |
| Re-ingestion + payload schema migration | Phase 2f | `feat/VOX-rag-ingest` |
