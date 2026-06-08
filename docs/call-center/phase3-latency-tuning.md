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

## 2b. Bug fix — scoped query returned the wrong hotel (added 2026-06-04)

### Symptom

A scoped, named-hotel query answered about a *different* hotel. From
`concierge-2026-06-04.jsonl`:

```
path=scoped_qdrant  hotel_mention="Crystal Tat Beach Pearl Collection"
                    → returned "Cornelia De Luxe Resort"
router.reason = "hotel_resolved"
```

This fires on the most natural flow: *"recommend a hotel"* → bot names Crystal Tat
→ *"tell me about Crystal Tat"* → answer about the wrong property.

### Root cause

`ConciergePipeline._run_kb` sourced the resolved id **only** from the
decomposition:

```python
hotel_id = decomposition.get("hotel_id") if path == PATH_SCOPED else None
```

But `decomposition["hotel_id"]` is written in exactly one place — the *inline*
resolver on the `PATH_HOTEL_RESOLVE` branch ([`pipeline.py`](../../src/voxtera/call_center/pipeline.py)).
When `session["active_hotel_id"]` is already set from a prior turn, the router
returns `PATH_SCOPED` **directly** (`router.py` → reason `"hotel_resolved"`), so
that branch never runs, `hotel_id` stays `None`, the scope filter no-ops, and
`compound.discover` degrades to a generic broad semantic search over
`requirements` (`["hotel overview", "amenities", "facilities"]`) — returning
whatever ranks top.

### Fix

Source the id from **either** location, and warn loudly if a scoped path still
has no id:

```python
hotel_id = (
    (decomposition.get("hotel_id") or session.get("active_hotel_id"))
    if path == PATH_SCOPED
    else None
)
...
if path == PATH_SCOPED and not hotel_id:
    logger.warning("scoped path with unresolved hotel_id (mention=%r) — "
                   "retrieval will not be hotel-scoped", decomposition.get("hotel_mention"))
```

### Tests

`tests/call_center/test_pipeline.py::test_scoped_with_session_hotel_runs_kb_path`
was strengthened to assert `discover` receives `hotel_id="rixos_belek"` and the
result is filtered to that hotel. Verified it **fails on the old code and passes
on the fix**. Full suite: 145 passed, 3 skipped (Redis).

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

**Win:** small — and smaller than first claimed. See the correction below.

> **Correction (2026-06-04).** This optimisation was oversold. The decompose
> system prompt (`prompts/query_decomposer.md`) is **~648 words (~860 tokens)**,
> not the "large, static" prompt assumed here. At that size the encode step is
> trivial (~tens of ms), so prompt caching saves **little to nothing** on
> decompose — the original "~300–800 ms" figure does not hold. The same applies
> to the even smaller render prompt (~135 words). The real cost in both calls is
> **output-token generation**, not prompt encode (see §3.5). Keep the cache block
> — it's harmless and trims a little token cost — but do not count on it for
> latency.

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

### 3.5 Token-usage instrumentation on decompose + render (added 2026-06-04)

**Problem.** `decompose_ms` (~2–3 s) was the single largest stage on the critical
path, but nothing logged `usage` or `stop_reason`, so we couldn't tell whether
the cost was prompt-encode, output generation, or truncation at the `max_tokens`
cap. The Phase 3 doc *assumed* a large prompt; the live data (below) disproved it.

**Fix.** A `_extract_usage()` helper in
[`decompose.py`](../../src/voxtera/call_center/decompose.py) and
[`concierge.py`](../../src/voxtera/call_center/concierge.py) pulls token counts
off the Anthropic response. Decompose threads them through the decomposition
payload, so they land directly in `concierge-YYYY-MM-DD.jsonl`:

```json
"decomposition": {
  "...": "...",
  "stop_reason": "end_turn",
  "usage": {
    "input_tokens": 880,
    "output_tokens": 310,
    "cache_read_input_tokens": 860,
    "cache_creation_input_tokens": 0
  }
}
```

Render logs the same via loguru (`concierge.render usage in=… out=… cache_read=… stop=…`)
since its contract returns a bare string.

**How to read it.**

- `stop_reason == "max_tokens"` → the answer is being truncated at the cap
  (1024 for decompose, 512 for render); raise the cap or shorten the output.
- `output_tokens` is the real latency driver. High output + slow wall-time → the
  lever is **fewer output tokens** (omit null fields / leaner schema) or a faster
  model (`gpt-4.1-nano`), *not* prompt caching.
- `cache_read_input_tokens > 0` confirms the cache is hitting — and, given the
  ~860-token prompt, confirms it's saving very little (§3.3 correction).

**Win:** diagnostic only — settles *why* decompose is slow on the next run.

---

## 4. Expected timings (warm path, 2nd+ request)

| Stage | Before | After | Notes |
|---|---:|---:|---|
| classify | 2500 ms | 0 ms* | runs in parallel — hides behind decompose |
| session_load | 264 ms | 264 ms | included in parallel leg |
| decompose | 2952 ms | ~2200 ms | output-token bound (NOT caching — see §3.3 correction) |
| triage + route | 0 ms | 0 ms | unchanged |
| retrieve | 4950 ms | ~250 ms | warm model (embed ~180 + qdrant ~60) |
| render | 2189 ms | ~1500 ms | output-token bound |
| **total** | **12872 ms** | **~4200 ms** | ~3× faster |

*classify wall-time is 0 on the critical path; it still runs (it's the longer of the parallel legs unless decompose is slower, which it usually is).

### 4.1 Measured live (2026-06-04, `concierge-2026-06-04.jsonl`)

Four real calls confirm the warm-path model and reset where the budget actually goes:

| utterance | decompose | classify | retrieve | render | total |
|---|---:|---:|---:|---:|---:|
| spa + kids club (Antalya) | 3001 | 1086 | 314 | 1972 | 5649 |
| spa to relax (paris → no match) | 1958 | 1028 | 220 | 0 | 2319 |
| spa to relax (Antalya) | 3002 | 787 | 262 | 1377 | 4802 |
| scoped: Crystal Tat Beach | 2012 | 1206 | 361 | 1789 | 4307 |

Takeaways, all verified against the code:

- **Retrieve is not the bottleneck** — 220–361 ms warm. The "~4 s retrieve" worry
  was a misread of `total`. No further tuning needed here.
- **Decompose (2–3 s) is the #1 cost** and scales with output JSON size (1 req →
  ~1958 ms; 2 reqs + vibes → ~3001 ms). It is *output-token bound*; the ~860-token
  prompt means caching barely helps. Levers: omit null fields / leaner schema, or
  `gpt-4.1-nano`. §3.5 logging will confirm `output_tokens` / `stop_reason`.
- **Classify is already free** — fully hidden inside the decompose leg
  (787–1206 ms < decompose). A fast-path classifier saves **zero** wall-time until
  decompose drops below classify. Do not build it yet.
- **Render (1.4–2.0 s)** is #2; streaming render → TTS is the time-to-first-audio win.

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

### 5.5 Verifying token usage + cache hits (now logged — §3.5)

Token usage **is now logged** (added 2026-06-04). After a run, inspect
`concierge-YYYY-MM-DD.jsonl`:

```bash
python3 -c "import json,sys; [print(d['decomposition'].get('stop_reason'), d['decomposition'].get('usage')) for d in map(json.loads, open(sys.argv[1])) if d.get('decomposition')]" logs/concierge-$(date +%F).jsonl
```

- `decomposition.usage.output_tokens` — the decompose latency driver.
- `decomposition.stop_reason` — `"max_tokens"` means truncation at the 1024 cap.
- `decomposition.usage.cache_read_input_tokens > 0` — cache is hitting (but saves
  little here; see §3.3 correction).
- Render usage is in the loguru stream: grep for `concierge.render usage`.

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

Follow-up change set (2026-06-04, same branch):

```
src/voxtera/call_center/pipeline.py      # scoped hotel_id sourced from session.active_hotel_id (§2b);
                                         #   warn on unresolved scoped path
src/voxtera/call_center/decompose.py     # _extract_usage(): thread usage + stop_reason into payload (§3.5)
src/voxtera/call_center/concierge.py     # _extract_usage(): log render usage + stop_reason (§3.5)
tests/call_center/test_pipeline.py       # regression test asserts scoped hotel_id reaches discover (§2b)
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

Follow-up commit (2026-06-04):

```
fix(call-center): scoped hotel scope + token diagnostics

- scoped query no longer returns the wrong hotel: _run_kb now sources
  hotel_id from session.active_hotel_id when the router resolves via
  session (reason "hotel_resolved"), not just from decomposition (§2b)
- warn when a scoped path reaches retrieval with no resolved hotel_id
- log Anthropic usage + stop_reason on decompose (into the jsonl) and
  render (loguru) to settle why decompose is output-bound (§3.5)
- regression test asserts the resolved id reaches discover()
- doc correction: decompose prompt is ~860 tokens, so prompt caching
  saves little; decompose latency is output-token bound (§3.3, §4.1)

Tests: 145 passed, 3 skipped (Redis).
```
