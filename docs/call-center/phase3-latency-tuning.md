# Phase 3 — Latency Tuning + Region-Alias Fix

**Branch:** `feat/VOX-rag-foundation`
**Status:** Implemented, 148 tests pass. Pending UI verification + commit.
**Target:** voice pipeline end-to-end (user stops talking → first audio) **under 3s**.

---

## 1. Problem statement

After Phase 3 Slice C, a manual test of "spa in Antalya" through `/api/concierge` produced:

| Stage | Time |
|---|---:|
| classify | 2503 ms |
| session_load | 264 ms |
| decompose | 2952 ms |
| triage | 0 ms |
| route | 0 ms |
| **retrieve** | **4950 ms** |
| render | 2189 ms |
| **total** | **12872 ms** |

Two distinct issues:

1. **Retrieval returned 0 hotels** for valid Antalya queries — a payload-mismatch bug.
2. **End-to-end was strictly sequential**, with cold model loads, no prompt caching, and a synchronous LLM call (classifier) blocking the rest of the pipeline.

This doc covers both fixes.

---

## 2. Bug fix — region alias (was: 0 hotels for "Antalya")

### Root cause

The hotel KB Qdrant collection (`hotel_kb`, 92 chunks) was ingested with `region="Turkish Riviera"` for every point. The demo UI dropdown and the Claude decomposer both produce lowercase city slugs (`antalya`, `bodrum`, `belek`). `BroadHotelDiscovery._build_search_body` filters with a **case-sensitive exact match**:

```json
{"key": "region", "match": {"value": "antalya"}}
```

→ 0 matches → fail-closed copy.

### Fix

Added a region-alias normalisation layer in [`src/voxtera/call_center/kb_config.py`](../../src/voxtera/call_center/kb_config.py):

```python
REGION_ALIASES: dict[str, str] = {
    "antalya": "Turkish Riviera",
    "belek":   "Turkish Riviera",
    "kemer":   "Turkish Riviera",
    "side":    "Turkish Riviera",
    "alanya":  "Turkish Riviera",
    "bodrum":  "Turkish Riviera",
    "turkish riviera": "Turkish Riviera",
}

def canonical_region(region: str | None) -> str:
    if not region: return ""
    return REGION_ALIASES.get(region.strip().lower(), region.strip())
```

Applied in [`discovery.py::_build_search_body`](../../src/voxtera/call_center/discovery.py):

```python
must = [{"key": "region", "match": {"value": canonical_region(region)}}]
```

`CompoundAndDiscovery` delegates to `BroadHotelDiscovery`, so the fix covers both broad and compound paths.

### Tests updated

- `tests/call_center/test_broad_discovery.py::test_region_filter_present_in_search_body`
- `tests/call_center/test_broad_discovery.py::test_region_whitespace_is_stripped`

Both now assert the canonical label (`"Turkish Riviera"`) instead of the input slug.

### Future cleanup

When the corpus grows finer-grained (per-city payload), trim or remove the alias map and re-test.

---

## 3. Latency optimisations

### 3.1 Pre-warm e5-large at server boot

**Problem.** First `/api/concierge` call paid ~3.3s to lazy-load `sentence_transformers` and the `intfloat/multilingual-e5-large` weights.

**Fix.** [`demo-hotel/serve.py`](../../demo-hotel/serve.py) spawns a daemon thread at boot that imports `voxtera.call_center.embeddings.embed_query` and embeds the string `"warmup"`. The model is hot by the time the first request arrives.

Console line: `[warmup] call_center embed model ready in X.Xs`.

**Win:** ~3000 ms on first request.

### 3.2 Parallelise `classify ‖ (session_load + decompose)`

**Problem.** Three independent LLM calls ran strictly sequentially: classify (GPT-4.1-nano) → session_load → decompose (Claude Haiku). Classify and decompose share no inputs, so they can run concurrently.

**Fix.** [`pipeline.py::ConciergePipeline.run`](../../src/voxtera/call_center/pipeline.py) now runs two legs in parallel via `asyncio.gather`:

```python
verdict, (session, decomposition, _) = await asyncio.gather(
    _classify_leg(),
    _session_decompose_leg(),  # bundles session load + pending_slots merge + decompose
)
```

If the classifier returns `escalate=True`, the decompose result is discarded. The wasted tokens are negligible; the wall-time win is large.

Timing field `concurrent_pre_ms` is logged alongside `classify_ms` and `decompose_ms` so you can verify they're actually overlapping.

**Win:** ~2500 ms on the happy path (classify hides behind decompose).

### 3.3 Anthropic prompt caching on decompose + render

**Problem.** Every Claude Haiku call re-encoded the (large, static) system prompt.

**Fix.** [`decompose.py`](../../src/voxtera/call_center/decompose.py) and [`concierge.py`](../../src/voxtera/call_center/concierge.py) now wrap the system prompt in a `cache_control: ephemeral` block:

```python
system=[{
    "type": "text",
    "text": _DECOMPOSE_SYSTEM,
    "cache_control": {"type": "ephemeral"},
}]
```

Anthropic stores the encoded prompt for ~5 min. Cache hits pay ~10% of input-token cost and skip the encode step.

**Win:** ~300–800 ms per Claude call after the first warm-up; small token-cost reduction.

### 3.4 Sub-stage timing instrumentation for retrieve

**Problem.** `retrieve_ms` was opaque — couldn't tell embed vs Qdrant vs rerank.

**Fix.** [`discovery.py`](../../src/voxtera/call_center/discovery.py) returns `timings: {embed_ms, qdrant_ms, rerank_ms}` on every `discover()` call. [`compound.py`](../../src/voxtera/call_center/compound.py) aggregates these across the fan-out:

```json
"timings": {
  "fan_out": 2,
  "embed_ms_max":  180.4,
  "qdrant_ms_max":  62.1
}
```

The two contract tests (`test_response_shape_matches_contract` in `test_broad_discovery.py` and `test_compound_discovery.py`) were updated to allow the new `timings` key.

**Win:** diagnostic only — surfaces where time actually goes.

---

## 4. Expected timings (warm path, 2nd+ request)

| Stage | Before | After | Notes |
|---|---:|---:|---|
| classify | 2500 ms | 0 ms* | runs in parallel — hides behind decompose |
| session_load | 264 ms | 264 ms | included in parallel leg |
| decompose | 2952 ms | ~2200 ms | prompt caching |
| triage + route | 0 ms | 0 ms | unchanged |
| retrieve | 4950 ms | ~250 ms | warm model (embed ~180 + qdrant ~60) |
| render | 2189 ms | ~1500 ms | prompt caching |
| **total** | **12872 ms** | **~4200 ms** | ~3× faster |

*classify wall-time is 0 on the critical path; it still runs (it's the longer of the parallel legs unless decompose is slower, which it usually is).

To reach **< 3s end-to-end without streaming**, the classifier must come off the critical path entirely (Step 4 in the latency plan — regex emergency fast-path + async telemetry).

To reach **< 3s time-to-first-audio** in the voice pipeline, add streaming render → TTS (Step 5). With that, first audio plays at:

```
max(classify_ms, decompose_ms) + retrieve_ms + render_TTFT
≈ 2500 + 250 + 500 ≈ 3.25 s today
≈   0 + 250 + 500 ≈ 0.75 s once classifier is off the critical path
```

---

## 5. How to test

### 5.1 Prereqs

- `.env` populated (ANTHROPIC_API_KEY, OPENAI_API_KEY, QDRANT_URL, QDRANT_API_KEY, REDIS_URL).
- Python 3.12 venv at `.\.venv\`. **Must use venv Python — system Python lacks `sentence-transformers`.**

### 5.2 Unit tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/call_center -q
# Expect: 148 passed, 2 deselected
```

### 5.3 Probe Qdrant directly (alias sanity check)

```powershell
.\.venv\Scripts\python.exe -c "import os; from dotenv import load_dotenv; load_dotenv(); import requests; url=os.environ['QDRANT_URL'].rstrip('/'); h={'api-key':os.environ.get('QDRANT_API_KEY','')}; r=requests.post(url+'/collections/hotel_kb/points/count',headers=h,json={'filter':{'must':[{'key':'region','match':{'value':'Turkish Riviera'}}]},'exact':True},timeout=10).json(); print('canonical count:', r)"
```

Expect `count: 92`. A count of `0` means the corpus shape has changed and the alias map needs updating.

### 5.4 Demo UI test (manual)

Start the server:

```powershell
.\.venv\Scripts\python.exe demo-hotel/serve.py 8080
```

Wait for the line `[warmup] call_center embed model ready in X.Xs` before the first request.

Open <http://localhost:8080/voxtera-concierge.html>.

#### Scenarios

For each: clear `sessionStorage` (DevTools → Application → Storage → Clear Site Data) so sessions don't bleed. Paste me the debug panel for any that misbehave.

| # | Region | Utterance | Expected |
|---|---|---|---|
| 1 | Antalya | `I want a hotel with a spa` | `hotels ≥ 1`, region in decomposition aliased to `antalya`, names 1–3 properties |
| 2 | Antalya | `A hotel with a spa and scuba diving` | `requirements ≥ 2`, partial or full match, `timings.fan_out = 2` |
| 3 | (blank) | `recommend me a hotel` then `In Bodrum, near the beach, with a spa` | clarification asked first; second turn merges prior utterance; hotels ≥ 1 |
| 4 | Antalya | `family-friendly hotel with a kids club and a quiet vibe` | NO over-ask clarification; goes straight to retrieval |
| 5 | Paris | `recommend a hotel with a rooftop bar` | render_ms ≈ 0, fail-closed copy, no hallucinated names |
| 6 | Antalya | `I'd like a hotel in Belek with a golf course` | `region=belek` → aliased to `Turkish Riviera` → hotels ≥ 1 |
| 7 | Antalya | `Tell me about Rixos Premium Belek` | router path = `scoped` |
| 8 | Antalya | `Antalya'da spa ve çocuk kulübü olan bir otel arıyorum` | lang=tr, requirements ≥ 2, hotels ≥ 1, Turkish reply |

#### What to record per scenario

- `classify_ms`, `session_load_ms`, `decompose_ms`, `triage_ms`, `route_ms`, `retrieve_ms`, `render_ms`, `total`
- `concurrent_pre_ms` (should be roughly `max(classify_ms, decompose_ms + session_load_ms)`)
- `retrieval.timings.embed_ms_max`, `retrieval.timings.qdrant_ms_max`
- `retrieval.reason` (should be `null` on a good match, or one of: `partial_match_only`, `no_match_above_threshold`, `no_region_scope`, `empty_requirements`, `retriever_error`)
- `retrieval.count` (number of hotels returned)

### 5.5 Verifying prompt cache hits

After 2+ requests within ~5 min using the same prompts, Anthropic returns `usage.cache_read_input_tokens > 0` on the response. To inspect this you'd need to log `msg.usage` in the decompose/render functions (not currently logged). If you want this surfaced in the debug panel, ask and we'll add it.

### 5.6 Live evaluation suite

```powershell
.\.venv\Scripts\python.exe -m pytest tests/call_center/test_phase3_exit_criteria.py -m live_eval -s
```

Requires API keys. Costs Anthropic + OpenAI tokens.

---

## 6. What's NOT done yet

These are the next levers to pull if step 1–3 don't get you to < 3s in your voice pipeline:

1. **Fast-path classifier** — regex pre-filter on common safe utterances; only call GPT-4.1-nano on ambiguous ones, and run that as fire-and-forget telemetry. Removes ~2.5s from the critical path.
2. **Streaming render** — token-stream the Claude response via NDJSON so TTS can start within ~500ms of the first token instead of waiting for the full answer. This is the single biggest voice-UX win and almost certainly required to hit < 3s TTFA.
3. **Anthropic Sonnet → Haiku audit** — already on Haiku; double-check `LLM_MODEL_OVERRIDE` env isn't set higher in prod.
4. **Embedding sidecar for e5-large** — currently in-process. Spawning a long-lived subprocess (like the existing e5-small sidecar) would isolate model memory and allow recycling.

---

## 7. Files changed

```
src/voxtera/call_center/kb_config.py     # REGION_ALIASES + canonical_region()
src/voxtera/call_center/discovery.py     # canonical_region in filter, timings dict
src/voxtera/call_center/compound.py      # aggregate timings across fan-out
src/voxtera/call_center/pipeline.py      # asyncio.gather(classify, decompose)
src/voxtera/call_center/decompose.py     # cache_control on system prompt
src/voxtera/call_center/concierge.py     # cache_control on decompose + render system prompts
demo-hotel/serve.py                      # background pre-warm of e5-large
tests/call_center/test_broad_discovery.py     # contract test allows timings; alias-aware
tests/call_center/test_compound_discovery.py  # contract test allows timings
```

Plus three prior fixes from earlier in the session (already merged into the same uncommitted change set):

- `triage.py` — narrowed `_needs_hotel_vs_recommend` to stop the over-ask
- `pipeline.py` — pending_slots merge across clarification turns
- `pipeline.py` — render fails closed on `hotels=[]`

All on `feat/VOX-rag-foundation`. Single commit recommended:

```
fix(call-center): region alias + latency tuning

- map demo city slugs (antalya/bodrum/belek/...) to canonical
  "Turkish Riviera" payload via REGION_ALIASES + canonical_region()
- pre-warm e5-large at server boot (saves ~3s on first request)
- parallelise classify ‖ decompose (saves ~2.5s wall-time)
- Anthropic prompt caching on decompose + render system prompts
- per-stage retrieve timings (embed_ms / qdrant_ms / rerank_ms)
- prior session fixes: triage over-ask, fail-closed render,
  conversation continuity across clarification

Expected: 12.8s → ~4.2s on warm path. Streaming render still
needed to hit voice-pipeline < 3s TTFA.
```
