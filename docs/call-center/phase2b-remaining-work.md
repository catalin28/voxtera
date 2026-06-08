# Phase 2b — Remaining Work

Companion to [phase2b-test-report.md](phase2b-test-report.md). Captures items
that were intentionally deferred so Phase 2b could close on a green commit and
the team could move on to Phase 2c (compound-AND).

## 1. Live Qdrant integration smoke

~~Deferred.~~ **DONE** (chore/VOX-rag-live-smoke, 2026-06-03).

Live harness `scripts/smoke_broad_discovery_live.py` runs 8 scenarios end-to-end
against `http://138.197.142.222:6333` collection `hotel_kb` with real
`multilingual-e5-large` embeddings (region = `"Turkish Riviera"` — verbatim
case, matching live payload). Result: **8/8 PASS**. See
`phase2b-test-report.md` §8 for the score table.

## 2. CI wiring

`tests/call_center/` is not yet invoked in CI. Adding a workflow step:

```yaml
- name: Call-center KB unit tests
  run: .venv/bin/python -m pytest tests/call_center -q
```

would lock in the 36-test green bar (Phase 1 + 2a + 2b) and catch regressions
in `kb_retriever.py` / `discovery.py` early.

## 3. Threshold calibration

**DONE** (chore/VOX-rag-live-smoke, 2026-06-03). `DEFAULT_MIN_SCORE` raised
from 0.25 → 0.70. See Phase 2a §3 for the full rationale — same finding
applies to Broad Discovery (real region+activity narrowed matches: 0.77–0.82;
nonsense queries: 0.77). Compressed E5 cosine range means absolute thresholding
is a weak defense.

**Follow-up (folded into Phase 2c):** Relative-margin filter lands in
`feat/VOX-rag-compound` alongside compound-AND, since intersection across N
requirements would otherwise admit near-floor false positives. See
`Voxtera_RAG_Development_Plan.md` § Phase 2c.

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
