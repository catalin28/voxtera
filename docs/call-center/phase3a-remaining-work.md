# Phase 3a — Cross-Encoder Rerank — Remaining Work

## Done in 3a

- `src/voxtera/call_center/reranker.py` — `CrossEncoderReranker`,
  `RerankFn`, `sigmoid`, `is_rerank_enabled`.
- `kb_config.py` — `RERANK_MODEL`, `RERANK_MIN_SCORE`,
  `RERANK_RELATIVE_MARGIN`.
- `BroadHotelDiscovery` integration — opt-in via `rerank_fn` DI,
  fail-open on rerank errors, env kill-switch honored, separate
  threshold scales for cosine vs rerank.
- 6 new unit tests covering reorder / drop / fail-open / env-disable /
  margin / single-batch-call. All 66 tests green.
- `scripts/smoke_rerank.py` offline demo.
- 4-file phase doc set (user-story, plan, test-report, this file).

## Carry-over to Phase 3d (voice pipeline integration)

1. **Wire production rerank_fn.** Phase 3d will construct
   `BroadHotelDiscovery(rerank_fn=CrossEncoderReranker())` (or pass it
   through `CompoundAndDiscovery → BroadHotelDiscovery`) at the
   call-site that creates `ConciergeAgent` from the voice bot.
2. **Pre-warm the model.** Add a startup hook (likely in `bot.py` or
   `pipeline.py`) that calls `CrossEncoderReranker()._get_model()`
   once, so the first guest request doesn't pay the ~5–8 s cold start.
3. **`retrieve_ms` budget check.** Phase 3bc's `answer()` timings
   already report `retrieve_ms`. Confirm in 3d that the live number
   stays within budget (~330–370 ms target for 5-way fan-out) with the
   real model loaded.
4. **Concierge UI debug drawer.** Optional: surface the rerank vs
   cosine score per evidence chunk in `voxtera-concierge.html`'s debug
   drawer so we can visually validate thresholds during tuning.

## Deferred (not blocking 3d)

- **Threshold re-tuning** once we have ≥50 live rerank scores. Today's
  values (`RERANK_MIN_SCORE=0.50`, `RERANK_RELATIVE_MARGIN=0.15`) are
  bootstrapped from the model's published ranking ranges, not from our
  data. Re-calibrate after a week of production traffic.
- **Sidecar reranker service** (mirroring `embedding_server.py`) —
  only if in-process load time or memory becomes a constraint on the
  bot host.
- **Drop the cosine `min_score` pre-filter** (currently 0.70) once we
  trust the rerank floor to handle junk on its own. Today the cosine
  floor stays as cheap insurance and as a fast-path skip for obvious
  junk.
- **Cohere / Jina rerank backends** — the `RerankFn` callable contract
  already supports them, but no concrete need yet.

## Push state

- Local branch `feat/VOX-rerank` based on local `develop` at `e70acee`.
- **Not pushed.** Awaiting user review + merge approval.
- Local `develop` is 2 commits ahead of `origin/develop` (3bc merge +
  this branch's eventual merge will make it 3+).
