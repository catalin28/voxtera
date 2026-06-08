# Phase 3a — Cross-Encoder Rerank — Test Report

## Suite execution

```
$ pytest tests/call_center/ -q
............................................................. ........
66 passed in 0.40s
```

| File | Pre-3a | Post-3a | Delta |
|------|-------:|--------:|------:|
| `test_broad_discovery.py`   | 14 | **20** | +6 |
| `test_compound_discovery.py`| 14 | 14 | 0 |
| `test_concierge.py`         | 10 | 10 | 0 |
| `test_hotel_kb_retriever.py`| 11 | 11 | 0 |
| `test_hotel_resolver.py`    | 11 | 11 | 0 |
| **Total**                   | **60** | **66** | **+6** |

All Phase 2b/2c tests stay green with zero edits — the new code is
opt-in via the `rerank_fn` parameter (defaults to `None`).

## New test class: `TestRerank` (6 tests)

| # | Test | What it proves |
|---|------|----------------|
| 1 | `test_rerank_reorders_hits_by_rerank_score` | Cosine top is overridden by rerank ranking; raw cosine preserved (implicit via output ordering test). |
| 2 | `test_rerank_drops_hits_below_rerank_min_score` | Sub-0.50 rerank scores are filtered out, even when cosine ≥ 0.70. |
| 3 | `test_rerank_failure_falls_back_to_cosine` | Reranker raising `RuntimeError` does not break retrieval — cosine-scored hits still returned. |
| 4 | `test_rerank_disabled_by_env` | `RAG_RERANK_ENABLED=false` bypasses rerank even with `rerank_fn` injected; cosine scores survive. |
| 5 | `test_rerank_relative_margin_trims_weaker_hotels` | Default `RERANK_RELATIVE_MARGIN=0.15` trims hotels >0.15 below leader on the [0,1] scale. |
| 6 | `test_rerank_called_only_once_per_discover` | Reranker runs **once** per discover call with the full batch of passages — not per-hit. |

## Smoke run (`scripts/smoke_rerank.py`)

```
=== BEFORE rerank (cosine only) ===
  reason : None
  count  : 4
  top    : 0.830
   - spa_generic_hotel                score=0.830  «Our wellness center offers massage, sauna, and a swi…»
   - thalasso_specialist_hotel        score=0.820  «Authentic thalassotherapy treatments with heated sea…»
   - yoga_retreat_hotel               score=0.810  «Daily yoga sessions on the terrace overlooking the b…»
   - boutique_pool_hotel              score=0.790  «Two outdoor pools and a small fitness room. No spa s…»

=== AFTER rerank (mock keyword overlap) ===
  reason : None
  count  : 1
  top    : 0.900
   - thalasso_specialist_hotel        score=0.900  «Authentic thalassotherapy treatments with heated sea…»
```

The cosine ranking ships the wrong hotel at #1 (generic spa). The
reranker correctly elevates the thalasso specialist and drops the
three irrelevant hotels below the 0.50 floor.

## Manual checks performed

- `get_errors` on `discovery.py` and `reranker.py` — no errors.
- `pytest` full call_center suite (66 passed, 0.40s).
- Smoke script run in venv (offline mock); output above.

## Not yet covered

- **Live model end-to-end test** against a real Qdrant collection +
  loaded `bge-reranker-v2-m3`. Deferred to Phase 3d's voice-pipeline
  integration smoke, where it has real value.
- **Latency benchmark** with the real model loaded. Will be measured
  once 3d wires the production `CrossEncoderReranker` into
  `ConciergeAgent` / voice pipeline calls and the existing
  `retrieve_ms` timer captures it for free.
