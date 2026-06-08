# Call-Center RAG — Engineering Handoff & Next Steps

**Date:** 2026-06-05
**Working branch:** `feat/VOX-triage-decomposition`
**Test status:** `tests/call_center` → **163 passed, 3 skipped** (skips are Redis integration tests needing a live Redis)
**Owner of this doc's context:** previous session (latency tuning, conversation-state fixes, evals, progressive narrowing)

> Read this top-to-bottom once. §1 = where things stand, §2 = decisions you must
> NOT relitigate, §3 = prioritised next work, §4 = how to run everything, §5 = gotchas.
> Companion docs are linked inline; the most important are `fast-lane-design.md`
> (voice integration blueprint) and `conversation-eval.md` (the test/eval strategy).

---

## 1. Where things stand

### Built and solid (don't rebuild)
- **Pipeline:** `decompose → triage → route → retrieve → render`, orchestrated in
  `src/voxtera/call_center/pipeline.py` (`ConciergePipeline`). Each stage is
  dependency-injected and unit-tested.
- **Decompose** (`decompose.py`) — Claude Haiku 4.5, full structured extraction.
  Backend is model-name routed (`gpt-*` → OpenAI, `claude-*` → Anthropic) via
  `DECOMPOSE_MODEL`. Token usage + `stop_reason` are logged into the concierge
  jsonl. Shared (pooled) Anthropic/OpenAI clients live in `clients.py`.
- **Triage** (`triage.py`), **Router** (`router.py`), **Escalation classifier**
  (`classifier.py`, gpt-4.1-nano), **Hotel resolver** (`resolver.py`, ES BM25),
  **Compound-AND retrieval** (`compound.py` + `discovery.py`) with relative-margin
  filtering and a **cross-encoder reranker** (`reranker.py`, bge-reranker-v2-m3),
  **Session store** (`session.py`, Redis).
- **Demo surface:** `demo-hotel/serve.py` `/api/concierge` (non-streaming text).

### Recently fixed (this session — see `phase3-latency-tuning.md` §2b, §3.5)
Conversation-state correctness — all were live bugs, now fixed + guarded by tests:
1. **Scoped query returned wrong hotel** — `_run_kb` now sources `hotel_id` from
   `session.active_hotel_id`, not just the decomposition.
2. **Carry-over echo** — the active hotel is no longer injected into the decompose
   prompt as text (model echoed the slug into `hotel_mention`); instead a no-id
   follow-up *hint* is added. Plus slug/generic-reference guards in `_coerce`.
3. **New mention overriding stale session hotel** — router re-resolves a freshly
   named hotel rather than reusing `session.active_hotel_id`.
4. **`empty_requirements` on scoped follow-ups** — pipeline injects a default
   overview requirement instead of failing closed.
5. **Triage over-asking** — scoped queries no longer trigger the dietary
   non-negotiable clarification.
6. **Time-sensitive intent hijacking a scoped query** to web — router fixed.
7. **Progressive narrowing** (NEW feature) — a broad query with **4+ matches**
   asks one differentiating question (budget → kids' ages → beachfront-vs-city),
   once per session, within the shared 2-turn clarification budget.

### Build status vs the original plan
See `Voxtera_RAG_Development_Plan.md` → **Build Status** table. Quick version:
- ✅ Phases 0, 1, 2a, 2b, 2c, 3, 3a (rerank)
- ❌ Not built: 2d (budget/geo filters), 2e (dual/comparison retriever), 4 (web search)
- ⚠️ Partial: 2f (4-band confidence → replaced by margin+rerank), 5 (narrowing now in; no result-set re-query loop yet), 6 (voice — concierge is text-only)

---

## 2. Decisions already made — do NOT relitigate

These were settled with data this session. Changing them needs a new reason, not a hunch.

1. **Model = Claude Haiku 4.5 for decompose + render.** A live eval
   (`scripts/eval_decompose.py`, results in `conversation-eval.md` §7) showed
   Haiku **1.000** vs nano **0.870** on `query_type`, nano breaks on Turkish and
   sometimes emits invalid JSON, and nano is **not faster** (both ~2s).
   `.env` is set to Haiku. Leave it.
2. **Decompose latency is a ~2s floor.** It survived a model swap, connection
   reuse (shared client), and token trimming. It is TTFT/round-trip bound, not
   token bound. **Do not keep trying to make the decompose call faster** — the
   path to a responsive voice agent is streaming + the fast-lane split (§3), not
   a faster decompose.
3. **Confidence handling = relative-margin + reranker, not the 4-band scheme** in
   the old plan. e5-large cosine scores are too compressed for absolute
   thresholds (documented in `kb_config.py`).
4. **Voice integration = swap the RAG, reuse the pipeline.** The existing
   single-hotel voice bot's RAG is a Pipecat `FrameProcessor` context injector
   (`src/voxtera/rag/injector.py`). The plan is to make the call-center retrieval
   a drop-in for its `Retriever` and **drop the call-center `render` step**
   (the voice pipeline's streaming LLM produces the answer). Full rationale in
   `fast-lane-design.md` §… and the integration discussion. Do not port the whole
   `ConciergePipeline` into the voice loop.

---

## 3. Next steps — prioritised

### P0 — Validate retrieval quality (the gate before anything else)
**Why:** everything so far proves routing/state/decompose; nothing has measured
whether retrieval returns the **right hotels with grounded answers**. Don't build
voice or persona on unproven retrieval.

**Do:**
1. Run the Tier-3 eval against live Qdrant + a warm Anthropic key:
   ```
   python scripts/eval_retrieval.py --out output/retrieval_report.txt
   ```
   (cases in `tests/call_center/eval_data/retrieval_cases.jsonl`)
2. Read the report. For the 10 **report-only** cases, judge relevance by hand and
   fill in `expect_hotels` / `expect_top` so they become auto-graded. Grow the set.
3. Triage failures by class: wrong region scope, missing requirement match,
   reranker dropping good hits, hallucinated amenities (grounding smell-test flags
   answers that name a hotel not in the retrieved set).
4. Fix the worst class first. Likely suspects if quality is low: the
   `RELATIVE_MARGIN` / `RERANK_MIN_SCORE` thresholds in `kb_config.py`, or KB chunk
   quality/coverage.

**Done when:** ≥ ~80% of graded cases pass hotel-recall and spot-checked answers
are grounded. Record the baseline in `conversation-eval.md`.

### P1 — Voice integration (the product milestone)
**Why:** none of this is real on a phone call yet — the concierge is a text demo.

**Do (per `fast-lane-design.md`):**
1. **Factor retrieval out of `ConciergePipeline`** so it can return *chunks/evidence*
   (decompose → route → `compound.discover` → hotels+evidence) **without** the
   render step.
2. **Wrap it as a `FrameProcessor`** matching `src/voxtera/rag/injector.py`'s
   `RAGContextInjector` interface (intercept `LLMContextFrame`, append retrieved
   excerpts to the latest user message). Swap it into the voice pipeline assembly
   (`src/voxtera/pipeline.py`) in place of the single-hotel injector's retriever.
3. **Drop the call-center `render` call** — the voice pipeline's existing streaming
   Claude + TTS produces the spoken answer (this also removes ~1.5s and gives
   streaming for free). Reuse the streaming-TTS path already proven in
   `serve.py` `/api/chat`.
4. Keep grounding via the injector preamble + the voice LLM system prompt.

**Watch:** the call-center collection uses **e5-large + reranker** on its own
Qdrant collection; the single-hotel RAG uses **e5-small**. Don't mix embedding
spaces — point the new injector at the call-center collection.

**Done when:** a transcript (or real call) runs STT → call-center retrieval →
streaming answer → TTS, end-to-end, scoped + broad + follow-up working.

### P2 — Fast-lane intake (voice latency / agent feel)
Only meaningful once voice (P1) is in. `fast-lane-design.md` §3–§9: one cheap
intake classifier handles escalate / "which region?" / chit-chat in ~0.8s; the
full decompose+retrieve runs only on search turns, masked by a filler line +
streaming. Ship incrementally (shadow-mode the intake classifier first).

### P3 — Backlog (when needed, not before)
- **Web search (Phase 4)** — router has `PATH_WEB` but pipeline returns a
  placeholder. Needed for events/weather/operators; probably v2.
- **Budget + geo filters (2d)** and **comparison retriever (2e)** — not built.
- **Per-visitor rate limit on `/api/concierge`** — currently ungated
  (`phase3bc-remaining-work.md`).
- **Streaming render for the text concierge** — only needed if the text demo
  itself must feel fast; voice gets streaming via P1.

---

## 4. How to run things

```bash
# Unit + conversation + eval-scorer tests (offline, no keys)
PYTHONPATH=src python -m pytest tests/call_center -q          # expect 163 passed, 3 skipped

# Tier 2 — live decomposer quality (needs ANTHROPIC/OPENAI keys; costs tokens)
python scripts/eval_decompose.py --models claude-haiku-4-5-20251001,gpt-4.1-nano
python scripts/eval_decompose.py --selftest                  # offline scorer check

# Tier 3 — live retrieval quality (needs Qdrant/ES/Redis + Anthropic key)
python scripts/eval_retrieval.py --out output/retrieval_report.txt
python scripts/eval_retrieval.py --selftest                  # offline grader check

# A/B decompose latency/fields across two models
python scripts/ab_decompose.py
```

**Test tiers (see `conversation-eval.md`):** Tier 1 = `test_conversation_flows.py`
(offline, scripts the decomposition, tests routing/state/triage/guards/narrowing).
Tier 2 = decomposer quality (live LLM). Tier 3 = retrieval quality (live KB).
Add a case whenever you fix a bug — every fix this session has a guarding test.

---

## 5. Gotchas

- **Python version:** `pipeline.py` uses `from datetime import UTC` (3.11+). Fine
  on the project's 3.12 venv; will break on ≤3.10. Run tests on the venv.
- **Tests write to the concierge log** unless `CONCIERGE_LOG_DIR` is set — they use
  the default `logs/`. Set `CONCIERGE_LOG_DIR=/tmp/cc_test_logs` when running the
  suite so you don't pollute `logs/concierge-YYYY-MM-DD.jsonl`.
- **`.env` vs `.env.fra`** — both exist; `.env.fra` is the Frankfurt variant and
  was NOT updated when `DECOMPOSE_MODEL` was set to Haiku. Mirror env changes if
  you deploy with `.env.fra`.
- **Doc dates drift** — the original `Voxtera_RAG_Development_Plan.md` checklists
  are NOT ticked; use its **Build Status** table (added 2026-06-05) as truth.
- **Reranker minimum / prompt cache** — Anthropic prompt caching is effectively a
  no-op on the decompose prompt (too short for Haiku's min cacheable size;
  `cache_read` is always 0). Don't rely on it for latency.

---

## 6. Key files index

| Area | File |
|---|---|
| Orchestration | `src/voxtera/call_center/pipeline.py` |
| Decompose (+ model routing, usage logging) | `src/voxtera/call_center/decompose.py` |
| Triage / narrowing question | `triage.py` / `pipeline.py::_narrowing_question` |
| Router (5-path) | `router.py` |
| Compound retrieval + rerank | `compound.py`, `discovery.py`, `reranker.py` |
| Resolver (ES) | `resolver.py` |
| Shared LLM clients | `clients.py` |
| Config / thresholds | `kb_config.py` |
| Live demo | `demo-hotel/serve.py` (`/api/concierge`, `/api/chat`) |
| Voice RAG seam (for P1) | `src/voxtera/rag/injector.py`, `src/voxtera/pipeline.py` |
| Evals | `scripts/eval_decompose.py`, `scripts/eval_retrieval.py`, `scripts/ab_decompose.py` |
| Eval data | `tests/call_center/eval_data/*.jsonl` |
| Design docs | `fast-lane-design.md`, `conversation-eval.md`, `phase3-latency-tuning.md` |
