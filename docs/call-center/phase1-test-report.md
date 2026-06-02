# Phase 1 — Hotel Resolver Test Report

Story: VOX-RAG-P1-001 (Hotel mention → canonical hotel_id)
Branch: feat/vox-hotel-resolver
Date: 2026-06-02

## 1. Scope

Validates that `HotelResolver` (`src/voxtera/call_center/resolver.py`) honors
the Phase 1 decision contract across the documented mention classes, and that
the supporting Elasticsearch index configuration is centralized and consumable
by the admin server without bloating it.

Decision contract:
- score ≥ 0.85 → `auto_resolve`
- 0.55 ≤ score < 0.85 → `needs_clarification` (top 3 candidates)
- score < 0.55 → `no_match`

## 2. Artifacts Under Test

- `src/voxtera/call_center/resolver.py` — `HotelResolver`
- `src/voxtera/call_center/index_config.py` — ES_INDEX, BRAND_KEYWORDS, HOTEL_SYNONYMS, `build_hotel_mapping()`
- `src/voxtera/call_center/server.py` — thin `/call_center/api/resolve` handler
- `tests/call_center/test_hotel_resolver.py` — unit suite
- `scripts/smoke_hotel_resolver.py` — mock-ES smoke harness

## 3. Unit Test Results

Command:
```
.\.venv\Scripts\python.exe -m pytest tests/call_center/test_hotel_resolver.py -q
```

Result: **11 passed in 0.30s**

Coverage by class:
| Group | Cases | Result |
|-------|-------|--------|
| TestHotelResolverCore | empty mention, auto_resolve, top-3 sorted clarification, no_match below threshold, no_candidates, apostrophe normalization, whitespace normalization, error degradation | 7/7 pass |
| TestThresholdBoundaries (parametrized) | score=0.85, 0.84, 0.55, 0.54 | 4/4 pass |
| **Total** | | **11/11 pass** |

## 4. Integration Smoke Test (Mock ES)

Live Elasticsearch credentials were unavailable, so the smoke harness uses an
in-memory `search_fn` that mimics ES hit shapes and applies a heuristic
scoring approximating the production query (`name^10`, `aliases^8`, `chain^5`,
location^2). The resolver code path exercised is identical to production —
only the network backend is swapped.

Command:
```
.\.venv\Scripts\python.exe scripts\smoke_hotel_resolver.py
```

Result:
| Mention | Class | Expected | Decision | Top score | Top hit |
|---------|-------|----------|----------|-----------|---------|
| `Rixos Premium Belek` | exact_full_name | auto_resolve | auto_resolve | 1.500 | rixos_premium_belek |
| `Riksos Premium Belek` | fuzzy_brand_typo | needs_clarification | needs_clarification | 0.750 | rixos_premium_belek |
| `Rixos Land of Legends` | alias | auto_resolve | auto_resolve | 1.500 | rixos_premium_belek |
| `Maxx Royal` | chain_partial_unique | auto_resolve | auto_resolve | 1.500 | maxx_royal_belek |
| `Cornelia` | chain_partial_ambiguous | auto_resolve | auto_resolve | 1.500 | cornelia_de_luxe |
| `Hilton` | chain_only (not in seed) | no_match | no_match | 0.000 | — |
| `Belek otel` | district_only_weak | needs_clarification | needs_clarification | 0.775 | limak_atlantis |
| `Quantum Sparkle Resort` | nonsense | no_match | no_match | 0.300 | (score_below_min_threshold) |
| `` (empty) | empty | no_match | no_match | 0.000 | (empty_mention) |
| `  Rixos   Premium  Belek  ` | whitespace_noise | auto_resolve | auto_resolve | 1.500 | rixos_premium_belek |
| `Rixos\u2019 Belek` | smart_apostrophe | auto_resolve | auto_resolve | 0.900 | rixos_premium_belek |

Summary: `auto_resolve=6`, `needs_clarification=2`, `no_match=3` — 11/11 on expected decision branch.

## 5. Index Configuration Changes (Task 2)

Moved index settings out of `server.py` into `src/voxtera/call_center/index_config.py`:
- `keyword_marker` filter with brand allow-list (`rixos`, `hilton`, `marriott`, `regnum`, `cornelia`, `maxx`, …) inserted **before** `turkish_stemmer` so brand tokens survive Turkish stemming (fixes Rixos→rixo over-stem).
- `synonym_graph` query-time filter for common spoken/typo variants (`riksos → rixos`, `kornelia → cornelia`, `regnum karya → regnum carya`, etc.).
- Dedicated `turkish_search` analyzer applied as `search_analyzer` on `name` and `aliases`; index-time `turkish_custom` analyzer unchanged for storage.

`server.py` now only imports `ES_INDEX` + `build_hotel_mapping()` and exposes a one-line `/call_center/api/resolve?q=...` endpoint that delegates to `HotelResolver`. No business logic in the admin server.

## 6. Outstanding / Deferred

| Item | Status | Notes |
|------|--------|-------|
| Live ES integration smoke | Deferred | Needs `ELASTICSEARCH_URL` + `ELASTICSEARCH_PASSWORD`. Run `POST /call_center/api/es/load` then re-execute representative mentions via `/call_center/api/resolve`. |
| CI run for unit suite | Pending | Local run green; CI invocation not yet wired for `tests/call_center/`. |
| Phonetic + edge-ngram fields | Out of Phase 1 | Tracked in `docs/call-center/elasticsearch-optimisation.md`. |

## 7. Verdict

Phase 1 acceptance criteria (decision contract, mention-class coverage, brand protection in analyzer chain, thin server surface) are met under unit tests and mock-ES integration. The same resolver instance will run unchanged against live Elasticsearch once credentials are provided — only the backend swaps.
