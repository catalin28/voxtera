# Phase 3a — Cross-Encoder Rerank — User Story

## As a
hotel concierge guest typing or speaking a free-form request

## I want
the AI to return the hotel(s) whose evidence chunks actually answer my
question — not the one whose marketing brochure happens to share a few
keywords with my query.

## So that
when I ask "thalassotherapy seawater treatment" I get the property that
runs a real thalasso center, not the generic four-star with a sauna
and a swimming pool. And when I ask something nobody in the region
supports, the system tells me "no match" instead of returning the
least-irrelevant hotel.

## Acceptance criteria

- The retrieval pipeline scores hits with a **multilingual cross-encoder**
  (`BAAI/bge-reranker-v2-m3`) after Qdrant returns the overshoot pool
  and before the per-hotel best-chunk + relative-margin trim.
- Rerank scores are normalised to `[0, 1]` via sigmoid so all callers
  consume one score scale.
- Rerank thresholds (`RERANK_MIN_SCORE = 0.50`,
  `RERANK_RELATIVE_MARGIN = 0.15`) are real separators on this scale —
  not the cosine "junk floor" the e5 retriever needed.
- Rerank is **dependency-injected**: production wires a real model in,
  unit tests inject a deterministic mock and never load the model.
- A **kill-switch env var** (`RAG_RERANK_ENABLED=false`) disables rerank
  at runtime for fast rollback / A-B comparison.
- Rerank failure (model crash, timeout, mismatched output length) must
  **not** break retrieval — the pipeline falls back to cosine scores and
  logs a warning.
- All Phase 2b / 2c tests stay green. New rerank tests cover reorder,
  threshold drop, env disable, relative-margin trim, fail-open, and
  "called exactly once per discover()".

## Out of scope (deferred)

- Re-tuning the e5 cosine `min_score` (kept at 0.70 as cheap pre-filter).
- Running the reranker as a sidecar service (in-process is fine until
  latency or memory demands otherwise).
- Wiring rerank into voice pipeline call paths — that is Phase 3d.
