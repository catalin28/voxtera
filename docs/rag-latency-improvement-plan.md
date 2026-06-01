# RAG Latency Improvement Plan

- **Date:** 2026-05-30
- **Status:** Recommendation document
- **Scope:** PSTN / on-demand Voxtera calls with RAG enabled
- **Based on traces:** `voxtera-trace-2026-05-30T00-32-37-651Z.json`, `voxtera-trace-2026-05-30T00-49-50-725Z.json`

---

## TL;DR

RAG should stay enabled. The goal is not to remove retrieval, but to make retrieval cheaper and earlier.

The highest-value RAG improvements for the current codebase are:

1. **Make retrieval language-aware with English fallback** so the retriever prefers the caller's language when that index exists, but still searches English when the store is English-only.
2. **Improve warmup coverage** so common hotel questions hit the result cache more often.
3. **Start retrieval earlier** on stable interim transcripts so part of the retrieval cost overlaps with STT finalization.
4. **Move retrieval cache out of the bot subprocess** so repeated questions across calls stay warm.
5. **Add narrower tracing around the RAG slice** so future tuning is based on exact stage timings, not a combined `stt_to_llm` bucket.

## Important constraint: the store is English-only today

If the vector store currently contains only English chunks, then strict caller-language filtering would reduce recall, not improve it.

The good news is the current embedding model is already multilingual:

- `intfloat/multilingual-e5-small`
- queries in many languages can still match English passages semantically

That means the current broad retrieval mode is still valid for non-English callers as a baseline.

What should change is not "English-only retrieval" versus "language-filtered retrieval". The correct change is:

1. prefer the caller language when indexed chunks exist for that hotel
2. otherwise fall back to English
3. optionally fall back to all languages as a final safety net

So for the current dataset, the safest near-term behavior is still to search English content. Do not force `language=caller_language` until the fallback exists.

## Minimum multilingual ingest plan

If the store is English-only today, the fastest safe path is:

1. keep English retrieval as the baseline
2. add translated source documents only for the top 2 or 3 caller languages
3. ingest those translations offline into separate language partitions

This improves answer quality without adding per-turn latency.

### Why offline multilingual ingest is better than runtime translation

Runtime translation on every turn would add another network or model step to the visible path.

Offline translation plus ingest keeps the live path simple:

- user speaks in `fr`
- retriever prefers `fr` chunks if present
- otherwise falls back to `en`
- LLM still answers in the caller language

### Smallest rollout that is worth doing

Start with:

- `en` as the canonical source corpus
- the top 2 or 3 non-English languages from real calls
- only the highest-value hotel documents first

Recommended first categories:

- `spa`
- `menu`
- `policies`
- `welcome-guide`
- `troubleshooting`

Those categories usually drive the highest RAG value on concierge calls.

### Suggested folder shape

Keep the source docs grouped by language before ingest. For example:

```text
demo-hotel/i18n/en/
demo-hotel/i18n/fr/
demo-hotel/i18n/ro/
```

Each translated file should stay semantically aligned with the English source so re-ingest is predictable.

### Existing CLI already supports this

The current ingest CLI already accepts a language flag:

```bash
uv run voxtera ingest --hotel demo --language en demo-hotel/i18n/en/
uv run voxtera ingest --hotel demo --language fr demo-hotel/i18n/fr/
uv run voxtera ingest --hotel demo --language ro demo-hotel/i18n/ro/
```

That stores each chunk set under its own `language` value in the same SQLite store.

### What not to do first

Avoid these as the first step:

- translating every document into every language
- adding runtime translation in the retrieval path
- forcing caller-language retrieval before localized chunks exist

The best cost/benefit path is partial offline translation for the most-used languages and the most-asked hotel topics.

---

## What the traces show

### Current fast-path PSTN turn

From the newer trace, the normal turn lands around `2088ms` end-to-end:

- `stt`: `741ms`
- `stt_to_llm`: `487ms`
- `llm_ttft`: `563ms`
- `tts_ttft`: `296ms`

### What those numbers mean

#### 1. STT is still the largest consistent fixed cost

In the newer trace, Gladia reports:

- `user_stopped_to_final_ms = 607ms`
- `user_stopped_to_final_ms = 679ms`

The Voxtera `stt` stage is slightly larger:

- `721ms`
- `741ms`

That difference is expected. The provider metric stops when Gladia finalizes the transcript. The Voxtera `stt` stage stops when `TranscriptStageTimer` sees the `TranscriptionFrame` downstream. So the extra `~60-115ms` is local frame transit after Gladia has already finalized.

#### 2. The next material cost is transcript -> LLM start

The newer trace shows:

- normal turn: `stt_to_llm = 487ms`
- slow turn, first pass: `stt_to_llm = 509ms`

That bucket currently includes:

- `context_aggregator.user()`
- `LLMRunGuard()`
- `BrowserTextInputController()` in Daily mode
- `RAGContextInjector`
- `TimeContextInjector`

The RAG path is the most important part of this bucket because it is awaited before the LLM is allowed to start.

### Separate issue: action turns can still do two LLM passes

The slow `4403ms` turn in the newer trace is not a pure RAG problem. It shows:

1. a first LLM run ending with an empty reply
2. a second LLM run producing the spoken confirmation

That shape looks like `LLM -> action -> LLM verbalization`. It should be treated as a separate latency problem from the knowledge-retrieval path.

---

## Current RAG path in code

The critical path is:

1. transcript arrives
2. `context_aggregator.user()` folds it into LLM context
3. `LLMRunGuard()` allows the run
4. `RAGContextInjector` intercepts the downstream `LLMContextFrame`
5. `Retriever.retrieve()` embeds the query and ranks chunks
6. the injector appends excerpts to the latest user message
7. the LLM starts

### Main files involved

- `src/voxtera/pipeline.py`
- `src/voxtera/rag/injector.py`
- `src/voxtera/rag/retriever.py`
- `src/voxtera/rag/embeddings.py`
- `src/voxtera/rag/store.py`
- `demo-hotel/serve.py`
- `scripts/embedding_server.py`

---

## Recommendation 1 — Make retrieval language-aware with English fallback

### Why this helps

The chunk store already supports language filtering, and the schema already has an index on `(hotel_id, language)`. The retriever also caches by `(hotel_id, language, normalized_query)`.

But the current injector calls `retrieve(hotel_id=..., query=user_text)` without passing a language. That means retrieval currently searches all chunks for the hotel, even when the transcript already tells us the caller language.

If you later ingest multilingual chunks, searching the caller's language first should:

- reduce the candidate set size
- improve cache locality
- improve retrieval relevance
- reduce CPU work for similarity scoring

If the hotel only has English chunks, multilingual E5 can still retrieve English passages from a non-English query. In that case, strict language filtering would be a regression. The retrieval policy must therefore be language-aware, not language-exclusive.

### Where to modify

#### `src/voxtera/rag/injector.py`

Change the injector so it can pass a language into `Retriever.retrieve(...)`.

Current call shape:

```python
results = await self._retriever.retrieve(hotel_id=self._hotel_id, query=user_text)
```

Target shape:

```python
results = await self._retriever.retrieve(
    hotel_id=self._hotel_id,
    query=user_text,
    language=current_turn_language,
)
```

#### `src/voxtera/pipeline.py`

The injector currently sees `LLMContextFrame`, not `TranscriptionFrame`, so it does not directly know the latest transcript language.

Add a small state-carrying processor before `context_aggregator.user()` that records the latest `TranscriptionFrame.language` for the turn, or extend an existing nearby processor to keep that state.

Good insertion point: immediately before `context_aggregator.user()` in the same area where `TranscriptStageTimer`, `AutoTTSLanguageSwitcher`, and `InstantAckFiller` already observe transcript frames.

#### `src/voxtera/rag/store.py`

No schema change is required. The language filter and index already exist.

#### `src/voxtera/rag/retriever.py`

This is where the real fallback behavior should live. The injector should only provide the preferred language. The retriever should decide how to degrade safely when the preferred language is unavailable.

### Recommended behavior

Use this lookup order:

1. `language = transcript language` when that hotel has indexed chunks in that language
2. fallback to `language = en`
3. fallback to `language = None` if needed as a final safety net

If the current hotel is English-only, step 2 will be the normal path. That still preserves multilingual user support because the embedding model is multilingual.

### Expected impact

Medium. This will not remove the entire `487-509ms` bucket, but it is the cleanest immediate RAG optimization already supported by the storage model.

### Short-term practical advice

Until multilingual chunks are actually ingested, keep retrieval effectively English-backed. The first code change should be fallback-aware retrieval logic, not hard caller-language filtering.

---

## Recommendation 2 — Improve warmup query coverage

### Why this helps

The retriever already has:

- `_chunk_cache` to avoid reloading chunk matrices
- `_result_cache` to avoid recomputing query embeddings for repeated questions
- background warmup in `on_joined`

The current warmup list is generic hotel FAQ English. That is useful, but it misses:

- hotel-specific phrasings
- multilingual variants
- common PSTN-style phrasing such as short confirmations and repeated concierge intents

### Where to modify

#### `src/voxtera/rag/retriever.py`

Expand `DEFAULT_WARMUP_QUERIES` with:

- multilingual variants for the most common hotel questions
- phrasings that match actual call transcripts
- hotel-specific wording from production logs and conversation history

#### `src/voxtera/pipeline.py`

Keep the existing `warmup_queries(...)` hook, but consider making the warmup query set configurable per hotel instead of relying only on the static default tuple.

Possible follow-up shape:

- load warmup queries from hotel config
- merge them with the generic defaults

### Expected impact

Medium for first-turn and FAQ latency. Low for uncommon queries.

---

## Recommendation 3 — Start RAG earlier on interim transcripts

### Why this helps

Today retrieval starts only after the final transcript has already been produced and turned into LLM context.

For PSTN, that means retrieval begins after the longest fixed delay in the pipeline has already happened.

If retrieval starts on stable interim transcripts, part of the query embedding and retrieval work can overlap with the final STT wait.

### Where to modify

#### `src/voxtera/pipeline.py`

Insert a speculative RAG prefetch processor upstream of `context_aggregator.user()` and downstream of STT, where interim transcripts are still visible.

#### New processor file

Recommended new file:

- `src/voxtera/rag/speculative_prefetch.py`

Responsibilities:

- listen for `InterimTranscriptionFrame`
- debounce noisy updates
- normalize transcript text
- start background retrieval for stable interim text
- reuse the result when the final transcript matches or is close enough

#### `src/voxtera/rag/injector.py`

Teach the injector to consume the prefetched result instead of always starting a fresh `retrieve(...)` call.

### Expected impact

Medium to high if interim transcripts are stable. This is the best way to preserve RAG quality while reducing how much of retrieval remains on the visible latency path.

### Risks

- wasted work if the final transcript changes substantially
- more state and cancellation logic
- must avoid applying prefetched context to the wrong turn

---

## Recommendation 4 — Move retrieval cache out of the bot subprocess

### Why this helps

The bot is spawned per session. That means:

- `_chunk_cache` is warm only for the current call
- `_result_cache` is warm only for the current call
- every new call starts fresh

The embedding sidecar solves only the model-load problem. It does not preserve retrieval results or chunk matrices across calls.

If the hotel gets repeated questions across many calls, the bigger win is to share retrieval-level caches across sessions.

### Where to modify

#### `scripts/embedding_server.py`

This is the natural extension point. It already runs as a long-lived localhost sidecar and already exposes HTTP endpoints.

Two possible directions:

1. extend it into a combined embedding + retrieval sidecar
2. keep it as-is and add a separate `scripts/retrieval_server.py`

#### `demo-hotel/serve.py`

Start the retrieval sidecar at launcher boot in the same way the embedding sidecar is started, and pass its URL into bot subprocess environment variables.

#### `src/voxtera/rag/retriever.py`

Add a client mode that can call the sidecar over HTTP instead of performing retrieval fully in-process.

### Expected impact

High across repeated calls. Medium within a single call.

This is the best structural optimization if the deployment pattern remains one subprocess per call.

---

## Recommendation 5 — Split the current `stt_to_llm` bucket into smaller trace stages

### Why this helps

Right now `stt_to_llm` is a combined timing bucket. It tells us there is `~487-509ms` between transcript and LLM start, but it does not tell us how much belongs to:

- context aggregation
- `LLMRunGuard`
- RAG retrieval
- time-context injection
- any action-trigger or controller work

Without that split, tuning becomes guesswork.

### Where to modify

#### `src/voxtera/pipeline.py`

Insert small stage timers or probes around:

- `context_aggregator.user()`
- `LLMRunGuard()`
- `RAGContextInjector`
- `TimeContextInjector()`

#### `src/voxtera/observability.py`

Add named stage emits for those new spans.

Suggested new stage names:

- `ctx_user`
- `llm_run_guard`
- `rag_retrieve`
- `time_context`

### Expected impact

Low direct latency impact. High debugging value.

This should be done before or alongside deeper RAG changes so later traces clearly prove what improved.

---

## Recommendation 6 — Keep RAG on for knowledge turns, but do not force it on purely transactional turns

### Why this matters

Some turns are knowledge lookups. Those need RAG.

Some turns are not knowledge lookups at all:

- `yes, send it`
- `that's all`
- `book it`
- `okay`
- `yes tomorrow at 11`

For those turns, RAG often adds latency without adding useful context.

This is especially relevant because the slower new trace shows an action-like turn doing two LLM passes.

### Where to modify

This is not a pure RAG-file change. It likely belongs in the action / intent path rather than in the retriever.

Candidate surfaces to review:

- action routing / listener code under `src/voxtera/actions/`
- the stage before `RAGContextInjector` in `src/voxtera/pipeline.py`

### Expected impact

High on action turns. None on knowledge turns.

### Important note

This is not the same as disabling RAG globally. It is selective gating based on turn type.

---

## What is unlikely to help much

These are lower-value changes compared with the items above:

- lowering `rag_top_k` from `3` to `2`
- changing `rag_min_score` as a latency tactic
- adding more embedding model warmup in the bot subprocess

Reason:

- `top_k` mostly affects how many results are returned, not the expensive part of query embedding
- `min_score` mainly affects quality filtering
- model warmup is already handled by the embedding sidecar and background warmup path

---

## Suggested implementation order

1. **Language-aware retrieval**
2. **Trace split for the `stt_to_llm` bucket**
3. **Better warmup query coverage**
4. **Speculative retrieval on interim transcripts**
5. **Shared retrieval sidecar / cross-session cache**
6. **Separate action-turn optimization path**

This order gives the best ratio of engineering cost to likely latency gain.

---

## Concrete file map

### Immediate changes

- `src/voxtera/pipeline.py`
  - carry transcript language forward
  - insert speculative prefetch if implemented
  - add finer stage timing around the pre-LLM slice

- `src/voxtera/rag/injector.py`
  - pass language into retrieval
  - consume prefetched retrieval results if added

- `src/voxtera/rag/retriever.py`
  - support language-first fallback behavior
  - improve default warmup query set
  - optionally add HTTP client mode for retrieval sidecar

### Existing support already in place

- `src/voxtera/rag/store.py`
  - already supports language filtering and already has the right index

- `src/voxtera/rag/embeddings.py`
  - already supports HTTP sidecar mode

- `demo-hotel/serve.py`
  - already starts the embedding sidecar and is the right place to start a retrieval sidecar too

- `scripts/embedding_server.py`
  - existing long-lived sidecar that can be extended or mirrored

---

## Success criteria

After implementing the first two recommendations, the next trace should prove:

1. `stt_to_llm` is lower than the current `487-509ms` on normal PSTN turns
2. the new trace explicitly shows how much of the remaining cost belongs to RAG versus non-RAG plumbing
3. retrieval stays relevant and accurate for multilingual hotel questions
4. no regression in action / booking / room-service flows
