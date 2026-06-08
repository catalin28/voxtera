# Phase 3a — Cross-Encoder Rerank — Development Plan

## 1. Problem

`multilingual-e5-large` produces a highly compressed cosine range
(`~0.74` [CLS] floor → `~0.85` strong match). Phase 2c live calibration
showed real matches at `0.77–0.82` and pure-junk queries at
`0.76–0.77` — i.e. **absolute thresholding alone cannot separate
signal from noise**. Phase 2b shipped a relative-margin trim (`0.05`)
on top of the cosine floor (`0.70`) as a stopgap; it helps but it
still admits keyword-overlap false positives at the top of the list.

A cross-encoder re-scores the actual `(query, chunk_text)` pair instead
of the cosine of two independently-encoded vectors. The scores
separate properly (real matches > 0.7 sigmoid, junk < 0.3), so the
thresholds can do real work.

## 2. Model choice

**`BAAI/bge-reranker-v2-m3`** (~568 M params, ~1 GB on disk).

- Multilingual — matches our `multilingual-e5-large` retriever (en, tr,
  ru, fr, de, es, …).
- The English-only `bge-reranker-base` was rejected: our content and
  queries are multilingual; an English-only reranker would degrade
  non-English performance below the baseline.
- Runs on CPU at ~80 ms per 30-pair batch — acceptable for our 5-way
  fan-out in `CompoundAndDiscovery` since the batches rerank
  concurrently and wall-clock latency stays roughly that of a single
  batch.

## 3. Architecture

```
                                     ┌──────────────────────┐
  query ─► embed (e5) ─► Qdrant ───► │  rerank (bge-v2-m3)  │ ──► _finalize
                          (≤30 hits) │  sigmoid(logit)→[0,1]│     (drop <0.50,
                                     └──────────────────────┘      trim margin 0.15,
                                                                   cap @ max_hotels)
```

- **New module:** `src/voxtera/call_center/reranker.py`
  - `RerankFn = Callable[[str, list[str]], Awaitable[list[float]] | list[float]]`
  - `class CrossEncoderReranker` — lazy `sentence_transformers.CrossEncoder`
    load on first call. Mirror the `embeddings.py` lazy pattern.
  - `sigmoid(x)` — numerically stable logit → `[0,1]`.
  - `is_rerank_enabled()` — read `RAG_RERANK_ENABLED` (default true).
- **Config additions** (`kb_config.py`):
  - `RERANK_MODEL = "BAAI/bge-reranker-v2-m3"`
  - `RERANK_MIN_SCORE = 0.50`
  - `RERANK_RELATIVE_MARGIN = 0.15`
- **Integration** (`discovery.py`):
  - `BroadHotelDiscovery.__init__` accepts `rerank_fn`,
    `rerank_min_score`, `rerank_relative_margin`.
  - New private `_maybe_rerank(query, hits) → (hits, reranked: bool)`
    runs after `_search`. Replaces each `hit["score"]` with the rerank
    score, keeps the raw cosine on `hit["_cosine"]`, re-sorts desc.
  - `_finalize` is parameterised by `reranked: bool` so it picks the
    right threshold pair (rerank scale vs cosine scale).
- **Fail-open:** any exception in `_maybe_rerank` (including length
  mismatch) is logged and falls back to unmodified hits + cosine
  thresholds. Retrieval availability outranks rerank quality.

## 4. Dependency injection

Tests construct `BroadHotelDiscovery(rerank_fn=mock_fn)`; the
production wiring (Phase 3d) will pass
`CrossEncoderReranker()` as the rerank_fn. Today the default is
`rerank_fn=None` so the call_center package keeps working unchanged
in any context that hasn't opted in. This keeps the Phase 2b/2c test
suite green without a single edit.

## 5. Test scenarios

1. **Reorder** — cosine ranks A > B; rerank inverts → B > A in output.
2. **Threshold drop** — both clear cosine 0.70; only one clears rerank
   0.50. Other is removed from `hotels[]`.
3. **Env kill-switch** — `RAG_RERANK_ENABLED=false` + `rerank_fn`
   provided → rerank never called, cosine scores survive.
4. **Fail-open** — `rerank_fn` raises → cosine results returned, count
   unchanged, no exception propagates.
5. **Relative-margin trim** — 3 hits with rerank scores 0.90 / 0.80 /
   0.60 → top two survive (margin 0.15), third trimmed.
6. **Called once** — 10 input hits → exactly one batched rerank call
   with all 10 passages (not per-hit).

All scenarios use the existing `_hit` / `_make_search_fn` helpers in
`tests/call_center/test_broad_discovery.py` — no live network, no
model load.

## 6. Smoke (`scripts/smoke_rerank.py`)

Offline mock using literal keyword overlap as the rerank score.
Demonstrates before/after on a 4-hit pool where the cosine-leader is
the generic-spa hotel but the actual thalasso specialist is at
position #2. After rerank, the specialist is alone at top and the
irrelevant yoga / no-spa hotels drop below the 0.50 floor.

Network-mode toggle: `VOXTERA_SMOKE_REAL=1` reserved for future
end-to-end runs against live Qdrant + real model.

## 7. Latency budget

| Stage | Before 3a | After 3a (5-way fan-out) |
|-------|-----------|--------------------------|
| `retrieve_ms` (concierge) | ~250 ms | ~330–370 ms |
| rerank wall-clock | – | ~80–120 ms (concurrent batches) |

Within the budget. Sidecar service deferred until measured pressure
appears.

## 8. Risks & mitigations

- **Cold-start model load (~5–8 s, ~1 GB download first run).**
  Lazy load on first call; ops doc updated to warm the model at
  container start in production.
- **Rerank changes the meaning of the score field.** Mitigated by
  keeping raw cosine on `_cosine` and by parameterising `_finalize`
  with `reranked: bool` so the threshold scale is unambiguous.
- **Future plug-in rerankers (Cohere, Jina, etc.)** — already
  supported via the `RerankFn` callable contract; no module changes
  needed.
