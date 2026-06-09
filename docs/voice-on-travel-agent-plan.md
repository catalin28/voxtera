# Adding Voice to the Travel Agent (TRA) — Analysis & Plan

**Status:** planning only (no code yet)
**Date:** 2026-06-08
**Approach:** **Phase-2-first.** Build the real (Daily/Pipecat) voice path directly, de-risked by testing the concierge brain in the pipeline's local/text mode before adding WebRTC. The earlier "lightweight browser-mic v1" idea is **dropped** — see §7 for why.

---

## 1. What we're starting from

### `voxtera-demo.html` (the reference voice experience)
The orb is the real experience: tapping it calls `POST /api/start-session`, which spawns a `python -m voxtera.bot` subprocess (Pipecat) into a fresh Daily.co WebRTC room. The browser joins via `@daily-co/daily-js`. **STT, LLM, and TTS all run server-side in the bot**; the browser only streams mic audio up and renders Daily **app-messages** the bot emits (`user-transcript`, `bot-reply`, `bot-speaking`, `bot-done-speaking`, VAD `user-started`/`user-stopped`) to drive the orb states.

### `travel-agent.html` (the target)
Text-only. One call: `POST /api/concierge` with `{utterance, region, session_id}`, awaiting a full JSON dict: `{answer, retrieval, clarification, escalation, decomposition, router, session_id, timings}`. Renders `answer` as a bubble plus hotel/web **evidence cards** from `retrieval`, and handles `clarification` (follow-up) and `escalation` (human handoff). Persists `session_id` for multi-turn.

---

## 2. The core architectural difference (why this isn't a drop-in swap)

The two RAG systems have **different shapes**:

- **Demo bot brain = `voxtera.rag.*`.** A `RAGContextInjector` Pipecat processor sits *just before* a streaming Anthropic LLM (`pipeline.py` ~L1330–1368). It retrieves hotel chunks and **augments the user message**; the LLM then streams the answer token-by-token. Context-injection model, low latency, nothing runs before the LLM.

- **TRA brain = `voxtera.call_center.ConciergePipeline`** (`/api/concierge`, `serve.py` ~L5485). Not a chunk-injector — a **multi-stage agent**: `classify → decompose → triage → route → retrieve (Elasticsearch hotel resolver + Qdrant chunks + web) → render`. It returns the **finished answer itself**. Backing stores are ES + Qdrant (not the demo's SQLite `ChunksStore`).

So "same voice, different RAG" means **wrapping the concierge as a turn-level answer source inside the voice pipeline**, not swapping a processor. Good news: `ConciergePipeline` was **already written for a voice channel** — its render functions produce spoken-style answers and omit citation lists ("read aloud in a voice system" — `pipeline.py` L758, L798). The brain is voice-ready; only the **transport** is missing.

**Latency caveat that shapes everything:** the concierge runs several sequential LLM calls + retrieval per turn (~3–8s typical). Fine behind a chat "Thinking…"; awkward as dead air in a live call. §4 addresses it head-on.

---

## 3. The build sequence (each step feeds the next)

The bot already supports **`TRANSPORT_MODE=local`** (the default, `config.py:75`) with **`INPUT_MODE=text`** (`config.py:73`) — you can run the *real* Pipecat pipeline and **type** to it in the terminal, no Daily/browser/STT. That makes a true incremental path possible.

### Step 1 — Stream the concierge render *(latency, standalone)*
Switch the concierge's `render_fn` from non-streaming `messages.create` to `messages.stream` (`concierge.py:295`, ≤512 tokens). The hotel agent already streams this exact way (`serve.py:5030`). Lets TTS start on the first sentence instead of waiting for the whole reply. Testable through `/api/concierge` today — no Daily required. This is Phase 2's #1 latency fix, done first and in isolation.

### Step 2 — Build the concierge-brain processor *(the core new piece)* — IMPLEMENTED
`src/voxtera/travel_agent_brain.py` — `TravelAgentBrain(FrameProcessor)` consumes the `LLMContextFrame` the context aggregator emits (the same trigger the LLM uses), reads the latest user utterance, runs `ConciergePipeline.run(utterance, session_id, region)`, and emits `LLMFullResponseStartFrame → LLMTextFrame → LLMFullResponseEndFrame` — the exact frames the LLM service emits. So TTS sentence-aggregation, `DemoEventBroadcaster` (orb `bot-thinking`/`bot-reply`, derived from those frames at `observability.py:749`), and the assistant context aggregator all work unchanged. Wired into `pipeline.py` **behind `if settings.bot_brain == "travel_agent"`** in place of `llm`; `TimeContextInjector` skipped for this brain (it would pollute the utterance). Region/session from `CONCIERGE_REGION`/`VOXTERA_SESSION_ID` env for now (Daily bridge = Step 4); session carried forward for multi-turn. First cut emits the full answer as one `LLMTextFrame` (TTS still splits sentences); streaming the render through the processor is a follow-up. *Unit-verified in sandbox: utterance extraction (string + list content), frame sequence, context-frame swallow, passthrough, session carry-forward.*

**`BOT_BRAIN` switch (decided + implemented):**
- Values: **`hotel`** (default — current `RAGContextInjector` + streaming LLM over the hotel KB), **`travel_agent`** (the `ConciergePipeline` brain that backs `/api/concierge`; named for the channel it serves, since the hotel bot also self-describes as a "concierge"), **`none`** (bare LLM, no RAG — for isolating brain vs. transport issues). The backend class stays `ConciergePipeline`; only the `BOT_BRAIN` value is `travel_agent`.
- **It supersedes `rag_enabled`.** That boolean becomes *derived* in `config.from_env`: `rag_enabled = (bot_brain == "hotel" AND RAG_ENABLED set)`; `travel_agent`/`none ⟹` the hotel injector is off. One knob, no contradictory states. Invalid values raise at load. *(Implemented in `config.py`; verified the `hotel` default preserves current behavior.)*

### Step 3 — Test it in local/text mode *(the real de-risking step)*
Run `TRANSPORT_MODE=local INPUT_MODE=text BOT_BRAIN=travel_agent` and type queries in the terminal. Validate the brain swap, streamed answer, per-stage `timings`, and fillers — all in the actual pipeline, with **zero Daily/audio complexity**. This is the "try it fast" win the dropped browser-mic v1 was only pretending to be, but on the architecture we're actually shipping.

### Step 4 — Flip to Daily (the orb) *(transport)* — IMPLEMENTED
- **`serve.py` (4a):** `_spawn_bot` gained `bot_brain`/`region`/`channel` params → `BOT_BRAIN`/`CONCIERGE_REGION`/`VOXTERA_CHANNEL` env. `/api/start-session` reads `brain` + `region` from the body (only `hotel`/`travel_agent`/`none` honoured), sets `channel="tra"` for travel_agent. Daily room gets a **`tra-` prefix** (vs `vox-`) for travel_agent. The public **demo-token gate is exempted** for `brain=travel_agent` (internal/B2B tool — clearly commented, easy to re-gate).
- **`travel-agent.html` (4b):** Daily SDK + a compact **orb** (shares the page's gold/rust palette) + `#remote-audio`. Tapping the orb calls `/api/start-session` with `{brain:'travel_agent', region: regionPick.value, language:'multi'}`, joins the Daily room, and renders `user-transcript`/`bot-reply` app-messages into the existing thread via `addBubble`; `bot-speaking`/`bot-done-speaking` drive orb states. Clean hang-up + `pagehide` beacon to `/api/end-session`. *Validated: JS parses (node --check), tags balanced, ruff/AST clean. Needs a live run (Daily creds + ES/Qdrant) to confirm end-to-end.*
- STT, TTS, **recording, and trace ride the existing pipeline** — mostly free; channel tagging + sub-timings are Step 5.

### Step 5 — Polish
Fillers on retrieval turns (§4), region/session bridge, emit concierge sub-timings as trace events (§5b), `channel=tra` tagging across trace + call records, evidence-card handling in call mode (§6).

---

## 4. Latency strategy — streaming **and** fillers

These fix **different halves** of the delay; use both.

**Why the hotel agent feels instant:** `/api/chat` is one injector+LLM call and already streams (`serve.py:5030`), chunked into sentence-level TTS. Nothing runs before the LLM.

**Why the concierge is slower:** before it speaks a word it runs `classify → decompose → retrieve (ES + Qdrant + sometimes web) → render`. Render is the spoken answer.

1. **Stream render (Step 1)** — speeds the **back half** (the answer generation).
2. **Fillers cover the front half.** Streamed render can't emit token one until retrieval finishes; that gap (classify+decompose+retrieve, web search being the slow part) is silent regardless. Speak an instant "Let me check that for you…" the moment the user stops. The pipeline already parallelizes `classify ∥ decompose` (`pipeline.py:892`), so the irreducible cost is mostly retrieval.
3. **Don't filler the fast path.** The conversational/recall path (`converse_fn`) has no retrieval and is already quick — fire fillers only on retrieval turns.

---

## 5. Latency logging + a TRA trace page (`admin/trace.html` equivalent)

**Big finding: the per-stage timing data already exists.** `ConciergePipeline.run()` builds and returns a full `timings` dict every turn:
`classify_ms, session_load_ms, decompose_ms, concurrent_pre_ms, hotel_detect_ms, triage_ms, route_ms, resolve_ms, retrieve_ms, fallback_ms, render_ms, web_ms, total_ms`.
Today it's returned in the `/api/concierge` JSON and discarded. We **persist and visualize** it, not instrument from scratch.

### 5a. How the hotel trace works (to mirror)
The voice bot emits `TraceEvent`s (kinds `frame, stage, error, audio, lifecycle, frame_drop`) onto a bus → `TraceForwarder` POSTs to the launcher → a **trace store** writes `{session_id}.ndjson` + a meta file per session (`serve.py:596`) → `admin/trace.html` reads `/api/trace/sessions`, `/api/trace/sessions/{id}/events`, `/api/trace/snapshot`, and live `/api/trace/stream` (SSE). Vital signs (STT, LLM TTFT, TTS TTFT, cache) and the per-turn waterfall derive from those events.

### 5b. What TRA needs
Because Phase 2 runs through the same pipeline, **most of the trace surface is automatic** — STT/TTS/VAD timing, frame drops, and the audio waterfall all appear. The one piece of work:
- The "LLM" stage is now the multi-stage concierge, so **emit the concierge `timings` sub-stages as `stage` `TraceEvent`s** into the bus, so the waterfall shows `classify → decompose → resolve → retrieve → web → render` between STT and TTS (today that segment is a single `rag`+`llm` block).
- **Tag sessions `channel=tra`** (the tracer already tags by `hotel_id`; add a channel field) so TRA and hotel sessions filter apart — either a query param on the existing page or a separate `admin/tra-trace.html`.

This gives you the exact `admin/trace.html` experience — vital signs, per-turn waterfall, frame drops — but with the concierge stage breakdown visible inline.

---

## 6. Saving dialog voice records (like the hotel agent)

How the hotel agent does it: `src/voxtera/call_record.py` writes **one directory per call** under `logs/`, with `record.json` (metadata + every user/bot turn, token usage, interruptions) and `recording.wav` (stereo 16 kHz call audio). `CallAudioRecorder` taps audio after `transport.output()`; `record_user_turn`/`record_bot_turn` are called from the pipeline (`bot.py:140`). `admin/calls.html` lists via `/api/admin/calls` and plays/downloads via `/api/admin/call/{id}`.

Because Phase 2 is a real bot through the same pipeline, **recording is essentially free**: a TRA call captures `recording.wav` + the full turn transcript automatically, landing in the same calls store and `admin/calls.html`. Additions:
- Tag the record `channel=tra` so TRA calls are filterable in `calls.html`.
- Optionally store the concierge `retrieval`/evidence alongside the turns, so a recorded TRA call shows which hotels were cited.

(No browser-side `MediaRecorder` gymnastics — that was only a problem for the dropped browser-mic idea, which had no server-side audio stream.)

---

## 6a. TTS configuration (`tts_config.json`)

A separate, admin-editable JSON file owns the **voice + tuning** for whichever bot is running (orthogonal to `BOT_BRAIN` — same file serves hotel and concierge). An admin voice page lets an operator select a voice/provider, test it, and save; the saved file is read at bot startup. This also resolves the plan's open "concierge voice persona" decision — it becomes a saved `active_voice`, not a hardcode.

### Schema (single active provider, flat params)
```json
{
  "_meta": { "schema_version": "1.0", "description": "...", "updated_at": "...", "updated_by": "..." },
  "active_voice": { "voice_key": "elevenlabs:21m00Tcm4TlvDq8ikWAM", "display_name": "Rachel (ElevenLabs)", "provider": "elevenlabs", "model": "eleven_flash_v2_5" },
  "parameters": { "...provider-specific keys..." },
  "fallback_chain": ["cartesia", "google"]
}
```
- **One flat `parameters` block** whose valid keys depend on `active_voice.provider` (ElevenLabs: `stability, similarity_boost, style, use_speaker_boost, speed, apply_text_normalization, pronunciation_dictionary_locators`; Cartesia: `speed, volume, emotion, pronunciation_dict_id`; Google: `speaking_rate, volume_gain_db, effects_profile, pause_style, custom_pronunciations`). Loader reads `parameters` directly (not `parameters[provider]`).
- **No `language` field.** Removed deliberately. Voxtera detects language per utterance and the runtime `LanguageSwitcher` (`controllers.py:720`, covers google/cartesia/elevenlabs) sets it. A static language here would either be dead config or pin the bot to one language. Language is owned by the session/STT layer, not the voice file.
- `voice_key` stores the provider-native voice **id/character** only; locale is resolved at runtime (`_chirp3_voice_for_lang`). Never bake a locale into `voice_key`.

### Validation (provider-conditional, fail fast)
Read `active_voice.provider`, then validate `parameters` against *that* provider's allowed keys + ranges (e.g. Cartesia `speed` 0.6–1.5, ElevenLabs `stability` 0.0–1.0). Reject mismatched/out-of-range blocks with a clear error — never silently default. `active_voice.provider` must not appear in its own `fallback_chain`.

### Fallback
`fallback_chain` lists providers to try if the active one's API is unreachable. With a single param block, **fallback providers have no tuned params in the file — they run on safe hardcoded defaults** (a fallback just needs to talk). NB: runtime TTS failover does **not** exist in the codebase yet — `fallback_chain` is a *new feature* (catch TTS error → reinstantiate next provider with its default params + a resolved voice), not just a config field.

### File location (decided)
- Env var **`VOXTERA_TTS_CONFIG_PATH`** (mirrors the existing `STT_THRESHOLDS_PATH` convention), default **`~/.voxtera/tts_config.json`** — alongside the runtime DBs (`VOXTERA_DB_PATH` → `~/.voxtera/`).
- **Not** in `config/` — that dir is git-committed, shipped, read-only in Docker; the admin panel must not write there.
- Docker/droplet: mount `~/.voxtera` (or point the env at a `/app/config` volume) as a **writable volume** so saves persist across restarts.
- **Per-hotel ready:** the path resolver tries `~/.voxtera/tts/<hotel_id>.json` first, falls back to the global `~/.voxtera/tts_config.json`. Ships as one global file today; becomes per-hotel with no loader change.

### Hot-reload — mostly a non-issue
The voice bot is spawned **fresh per call** (`_spawn_bot`), so each call reads the latest config at startup — an admin save is picked up on the **next call**, no restart or file-watcher needed. Only the persistent HTTP TTS paths (`/api/tts-test`, any concierge TTS) need a deliberate per-request re-read.

### Admin test surface
The existing `/api/tts-test` only synthesizes a **fixed greeting with no params** (`serve.py:4787`). Extend it to accept **arbitrary text + provider + voice + full param set** and return audio — that's what the page calls on every "Test voice" click. "Save" writes the JSON atomically (temp-write → rename). Self-contained build: one admin page + extended test endpoint + config writer + the startup loader/validator.

---

## 7. Why the lightweight browser-mic "v1" was dropped
It would have used the browser's `SpeechRecognition` (the small `#mic-btn` in the demo's input bar) → `/api/concierge` → a one-off `/api/tts`. The problem: it shares **no architecture** with the orb. Browser STT is client-side, Chrome-only, Google-hosted; Phase 2 STT is server-side (gladia/whisper) over Daily. Nothing built for it carries over — not the transport, not the STT, not the recording path (it has no server audio stream). Step 3 (local/text mode) delivers the same "try it fast" benefit on the architecture we're actually shipping, so the browser mic is pure detour.

---

## 8. Open decisions to confirm during build
- **Filler copy/voice** — one generic line or rotate a few; spoken in the detected language?
- **TRA trace page** — extend `admin/trace.html` with a `channel=tra` filter, or a separate `admin/tra-trace.html`?
- **Evidence cards in call mode** — drop them (voice-only), or push `retrieval` to the browser via app-message and render cards beside the live transcript?
- **Region in voice** — carry the page's region picker into `start-session`, or ask for region by voice as a triage slot?
- **Clarification/escalation by voice** — speak the triage clarification and feed the spoken reply back as the next turn; speak + visually flag escalations.
- **Record retention** — same `logs/` store + retention as hotel calls, or a separate TRA store?

---

## 9. File map (where the work lands)
- `src/voxtera/call_center/concierge.py` — `render_fn` → `messages.stream` (Step 1).
- `src/voxtera/pipeline.py` — concierge-brain processor + `BOT_BRAIN` switch replacing `RAGContextInjector → LLM`; forward concierge sub-timings to `PipelineTracer` (Steps 2, 5b).
- `src/voxtera/call_center/pipeline.py` — filler hook on retrieval paths; expose sub-stage timings for trace emission (Steps 4–5).
- `demo-hotel/serve.py` — `start-session`/`_spawn_bot` forward `BOT_BRAIN`, region, `channel=tra` (Step 4); persist concierge timings; `channel` tag in trace store + call records.
- `demo-hotel/travel-agent.html` — orb + Daily join logic (lifted from `voxtera-demo.html`), region→start-session (Step 4).
- `demo-hotel/admin/trace.html` (or new `tra-trace.html`) — concierge stage-breakdown waterfall + `channel=tra` filter (§5).
- `demo-hotel/admin/calls.html` — `channel=tra` filter to browse TRA dialog records (§6).
- `src/voxtera/trace.py` / trace store — add `channel` tag.
- `src/voxtera/call_record.py` — reuse as-is; add `channel=tra` tag.
- `src/voxtera/tts.py` / a new `tts_config.py` loader — read+validate `tts_config.json` at startup, build the TTS service (§6a). Path resolver: `~/.voxtera/tts/<hotel_id>.json` → `~/.voxtera/tts_config.json`, env `VOXTERA_TTS_CONFIG_PATH`.
- `demo-hotel/serve.py` — extend `/api/tts-test` to take arbitrary text + provider + voice + params; add config-writer endpoint (atomic temp→rename).
- `demo-hotel/admin/` — new voice-config/test page (select voice, test, save).
- **Unchanged:** `ConciergePipeline` logic, ES/Qdrant, all concierge prompts. The `timings` dict it already returns is reused, not rebuilt.
