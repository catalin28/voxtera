# Phase 3 — Concierge Agent — Remaining Work

**Branch:** `feat/VOX-rag-concierge`

---

## Shipped in Phase 3

- `ConciergeAgent` orchestrator (decompose -> compound -> render).
- Default Anthropic Claude Haiku LLM backend, dependency-injectable for tests.
- `GET /call_center/api/concierge` HTTP route.
- 8 unit tests (offline) + 6 mock smoke + 4 live smoke.
- 4 markdown docs.

## Deferred / next up

### 3a — Cross-encoder rerank
Re-rank `BroadHotelDiscovery` candidates with a small cross-encoder
(e.g. `bge-reranker-base`) before the relative-margin trim. Should
sharpen the e5 junk-overlap problem so `no_match_above_threshold`
becomes reachable on nonsensical queries.

### 3b — Admin UI panel for concierge
Add a `/call_center/` UI tab that posts to `/api/concierge` and shows:
- the decomposition JSON
- per-hotel evidence chunks
- the final rendered answer
Critical debugging surface once we start tuning the prompts.

### 3c — Latency instrumentation
Wire structured timings (decompose_ms, retrieve_ms, render_ms) into
the response payload and `loguru` logs. Required before any p95 claims.

### 3d — Voice-pipeline integration
Wire `ConciergeAgent` into the live voice bot (`bot.py` / `pipeline.py`)
so a guest on a real call hits this surface instead of (or alongside)
the existing Pipecat LLM service. Likely entry point: a new
"concierge tool" exposed to the bot's LLM via the existing `actions/`
tool-calling machinery.

### 3e — Multi-turn memory
Currently each `answer()` call is stateless. Add a `history: list[Turn]`
parameter so follow-up questions ("which of those is closest to the
beach?") can refine the previous retrieval.

## Carried forward from Phase 2c

- Push `develop` to `origin/develop` (now 9 commits ahead after this branch lands).
- Calibration finding: e5-large cosine scores are highly compressed; absolute
  thresholds alone don't fully separate signal. 3a is the planned mitigation.

## Operational notes

- `ANTHROPIC_API_KEY` must be set for the default decompose/render path.
  Tests inject their own LLM functions and do NOT require the key.
- Model selection honours `LLM_MODEL_OVERRIDE` env var; default
  `claude-haiku-4-5-20251001` matches the voice pipeline default.
