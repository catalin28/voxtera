# Pipeline Trace & Tune — Plan

- **Date:** 2026-05-07
- **Status:** Implementation in progress (branch `feat/pipeline-trace-and-tune`)
- **Author:** Claude + Catalin
- **Related:** `docs/admin-sessions-monitor-plan.md`, `docs/ON_DEMAND_BOT_SPAWN.md`, `src/voxtera/pipeline.py` (PipelineProbe, PipelineTracer, DemoEventBroadcaster)

---

## TL;DR

Add a developer-facing debug page at `/trace.html` (served by `demo-hotel/serve.py`) that lets you watch a live Voxtera turn flow through the pipeline in real time, see where time is being spent, and live-tune the knobs that control voice / sentence quality without restarting the bot.

The page has two halves:

1. **Trace view** — vital-signs strip, live pipeline graph, per-turn timeline (Gantt-style), audio scope, frame waterfall. Answers "is the flux broken, where, and how slow is it?".
2. **Knobs view** — every parameter that affects voice / sentence quality, grouped by pipeline stage, with a tier badge (live / next-restart / hardcoded). The six knobs you'd actually touch every debug session are live-editable: `vad_stop_secs`, `vad_min_volume`, `allow_interruptions`, TTS voice, STT provider, `rnnoise_enabled`. Everything else is read-only with an explanation.

Entry points: small unobtrusive trace icon in the corner of `demo.html` and `chat.html`. Opens `/trace.html` in a new tab. Same `VOXTERA_ADMIN_TOKEN` gate as the admin page.

---

## Goals

1. Watching one turn in `trace.html` answers three questions in under five seconds: *did every stage fire?*, *which stage owns the latency?*, *was anything dropped silently?*.
2. The knob panel makes every configurable parameter visible with an explanation of what it does and why the current default exists. No more grepping `audio.py` for an internal threshold.
3. Live editing the six v1 knobs takes one click per change. No restart, no reconnect, no `.env` edit. Edits do not persist (that's v2 — JSON / DB).
4. No new infrastructure: same port, same process as `serve.py`. The bot pushes events to `serve.py` over the existing `/api/bot-event` endpoint pattern (extends the launcher callback channel).
5. Page is dev-tool-grade: works in `make run` legacy mode (single bot) and on-demand-launcher mode (one bot per session). When the bot is offline, the page shows a clear "no bot connected" state instead of looking broken.

## Non-Goals

- **Persistence of tuned values.** v1 is in-memory only. v2 writes to `config/runtime_overrides.json`; v3 promotes to a DB if and when the launcher manages multiple bot configs.
- **Multi-bot dashboards.** v1 shows one bot. When the launcher lands and there are short-lived bots per session, the page shows the active session.
- **Operator-facing UI.** The admin page (`admin.html` per `admin-sessions-monitor-plan.md`) is for operators — who's in the room, kick. The trace page is for the developer — pipeline internals, latencies, knobs. They link to each other but never merge.
- **Recording / replay of a turn's audio.** v1 captures timestamps, transcripts, and RMS sparklines only. Audio recording adds storage, privacy, and consent surface area we're not solving here.
- **Editing thresholds inside `TranscriptionNoiseFilter` from the UI.** Those rules are read-only with explanations. They are baked-in for guardrail reasons; changing them belongs in code review, not a slider.

---

## Current state of the code (verified 2026-05-07)

Most of the data plane is already half-built and just needs to be reused:

- `src/voxtera/pipeline.py` already inserts `PipelineProbe` at 16+ named tap-points (`after_transport_in`, `after_rnnoise`, `after_leakage_guard`, `after_audio_monitor`, `after_vad`, `after_stt_router`, `after_parallel_stt`, `after_suppressor`, `after_stt`, `after_noise_filter`, `after_ctx_user`, `after_llm_guard`, `after_rag`, `after_llm`, `after_tts`, `after_transport_out`). Each probe currently logs interesting frames to loguru only.
- `src/voxtera/observability.py::PipelineTracer` already measures `think_ms` (LLM duration) and `total_ms` (user_stopped → bot_speaking) per turn at INFO log level.
- `src/voxtera/observability.py::DemoEventBroadcaster` already pushes `bot-thinking / bot-reply / bot-speaking / bot-done-speaking` events to the browser via Daily app-messages.
- `src/voxtera/observability.py::UserTranscriptBroadcaster` already pushes `user-started / user-stopped / user-transcript` events the same way.
- `src/voxtera/launcher_client.py::post_event` is the established bot → serve.py callback pattern (`POST /api/bot-event`). It's a no-op when `VOXTERA_LAUNCHER_URL` is unset.
- `demo-hotel/serve.py` is a `socketserver.ThreadingTCPServer` with `BaseHTTPRequestHandler`. It already maintains `BotSessionRegistry` for the on-demand launcher. Adding new routes is a one-line addition to `do_GET` / `do_POST`.

What's **missing**:

- The probes don't push to a non-loguru channel (so the browser can't see them).
- There's no per-turn correlation id, so events from one turn can't be reliably grouped.
- Per-stage durations beyond `think_ms` and `total_ms` aren't captured (no STT duration, no LLM TTFT, no TTS TTFT).
- Error frames flow through `PipelineTracer` but aren't broadcast anywhere visible.
- Live tuning of the VAD / leakage / RNNoise knobs doesn't exist; the existing app-message switchers (`ModelSwitcher`, `LanguageSwitcher`, `STTRouter`, `TTSRouter`) only cover provider / voice / language / model.
- No HTTP endpoint on the bot for tune commands.

## Architecture

```
┌─────────────────┐   GET /trace.html              ┌─────────────────────┐
│ Developer       │ ─────────────────────────────► │ serve.py            │
│ browser         │   GET /api/trace/stream (SSE)  │ - TraceEventBuffer  │
│ /trace.html     │ ◄─── SSE event stream ─────── │ - SSE fan-out       │
│                 │   GET /api/trace/snapshot      │ - tune proxy        │
│                 │ ◄────── JSON snapshot ──────── │ - /trace.html       │
│                 │                                │   static            │
│                 │   POST /api/admin/tune         │                     │
│                 │ ─────────────────────────────► │ ──────POST/tune──┐  │
└─────────────────┘                                └─────────────────┘│  │
                                                                       ▼  │
                                                  ┌─────────────────────┐ │
                                                  │ Bot subprocess      │ │
                                                  │ - TraceBus          │ │
                                                  │ - TraceForwarder    │ │
                                                  │   POST /api/bot-event
                                                  │   {type:"trace",...} ◄┘
                                                  │ - TuneServer (HTTP) │
                                                  │   127.0.0.1:PORT    │
                                                  │ - Tunables registry │
                                                  └─────────────────────┘
```

Key properties:

- **Bot pushes trace events to serve.py** via the existing `/api/bot-event` endpoint pattern (new event type `trace`). Reuses `launcher_client.post_event`.
- **Bot exposes a localhost-only HTTP server** for tune commands on a port serve.py picks at spawn time (`VOXTERA_BOT_PORT` env var). This is the smallest possible RPC surface — two endpoints, never reachable from outside the host.
- **Dashboard talks only to serve.py.** It doesn't know the bot is a separate process. This is the "Option A" baking decision: same port, same auth, simpler URLs, future-proof when the launcher lands.
- **Legacy / `make run` mode**: when `VOXTERA_LAUNCHER_URL` is unset, `launcher_client.post_event` is a no-op, so the trace stream is silent. `serve.py` shows "no bot connected" but the page still loads. Same fallback shape the admin page already uses.

## Event schema

Every event the bot pushes follows this shape:

```json
{
  "schema": "voxtera.trace.v1",
  "ts_ms": 1746602811923,
  "session_id": "abc123",
  "turn_id": "turn-2026-05-07T12:34:56.789-001",
  "kind": "frame" | "stage" | "error" | "knob" | "audio" | "lifecycle",
  "source": "after_vad",
  "data": { /* kind-specific */ }
}
```

`turn_id` is stamped at every `VADUserStartedSpeakingFrame` (or at startup-greeting time as `turn_id="greeting"`) and propagates downstream. All events from one turn share the same id, which is what powers the timeline grouping in the UI.

Per-kind shapes:

- **frame**: `{frame_type: "TranscriptionFrame", direction: "downstream", text?: "..."}`. Emitted by `PipelineProbe`.
- **stage**: `{stage: "stt" | "llm_ttft" | "llm_full" | "tts_ttft" | "transport_out", duration_ms: 412}`. Emitted by stage timers.
- **error**: `{level: "error" | "fatal", message: "..."}`. Emitted from `PipelineTracer`.
- **knob**: `{knob: "vad_stop_secs", old: 0.5, new: 0.3, origin: "live"}`. Emitted on every successful tune.
- **audio**: `{position: "in" | "out", rms: 0.043, peak: 0.058}`. Emitted at ~5 Hz.
- **lifecycle**: `{event: "bot_ready" | "session_start" | "session_end"}`. Emitted at the obvious moments.

The schema field is versioned so a future v2 (richer payloads, audio chunks, etc.) can coexist with v1 dashboards.

## Endpoints — `serve.py`

All trace endpoints require `X-Admin-Token` (same gate as admin endpoints). Missing token → 401. Missing `VOXTERA_ADMIN_TOKEN` on server → 503 with `error: "admin_disabled"`.

### `GET /api/trace/stream` (SSE)

Text/event-stream of trace events. Each event is `data: <json>\n\n`. Uses `id:` field for resumption (Last-Event-ID).

Server keeps a 5000-event ring buffer. New subscribers get the most recent ~200 events as catch-up, then live tail. When the buffer wraps, the oldest events are dropped silently — UIs treat the stream as best-effort, not durable.

### `GET /api/trace/snapshot`

Returns the current state in one JSON blob:

```json
{
  "bot_connected": true,
  "session_id": "abc123",
  "current_turn_id": "turn-2026-05-07T12:34:56.789-001",
  "knobs": [
    {"name": "vad_stop_secs", "group": "vad", "value": 0.5, "origin": "default", "tier": "live", "default": 0.5, ...},
    ...
  ],
  "providers": {
    "stt": "whisper",
    "tts": "openai",
    "tts_voice": "nova",
    "llm_model": "claude-haiku-4-5-20251001"
  },
  "metrics": {
    "last_turn": {"end_to_end_ms": 1342, "stt_ms": 412, "llm_ttft_ms": 280, "tts_ttft_ms": 145},
    "last_10": {"end_to_end_p50": 1280, "end_to_end_p95": 1850}
  }
}
```

The page calls this once at load time to populate state, then relies on the SSE stream for live updates.

### `POST /api/admin/tune`

Body: `{"knob": "vad_stop_secs", "value": 0.3}`.

Forwards to bot's `POST http://127.0.0.1:{VOXTERA_BOT_PORT}/tune`. Bot validates, applies, emits a `knob` trace event. Response shape:

- `200 {"applied": true, "knob": "...", "old": 0.5, "new": 0.3}`
- `400 {"applied": false, "error": "validation_failed", "detail": "..."}`
- `502 {"applied": false, "error": "bot_unreachable"}` if the bot's tune port is down
- `404 {"applied": false, "error": "unknown_knob"}` if knob isn't in the registry
- `409 {"applied": false, "error": "not_live_tunable"}` if knob is `next_restart` or `hardcoded` tier

### `GET /trace.html`

Static file. Served by the existing `SimpleHTTPRequestHandler` — no new code.

### Bot-side: `POST /tune` and `GET /knobs` (localhost only)

Implemented in `src/voxtera/trace_server.py` as a tiny aiohttp server on `127.0.0.1:VOXTERA_BOT_PORT`. Started by `voxtera.bot.run_bot` after pipeline build. Stopped on `EndFrame` drain.

Accepts `POST /tune {knob, value}` from serve.py only. Validates against the tunables registry, calls the knob's `apply()` function, emits a `knob` trace event. Returns the same shape as `/api/admin/tune` above so serve.py can pass through.

`GET /knobs` returns the current knobs snapshot — used by serve.py to populate `/api/trace/snapshot`.

No auth on the bot side. The port is bound to `127.0.0.1` only and serve.py is the only intended caller. Trying to reach it from outside the host requires SSH tunneling — at that point you're already on the box.

## Knob taxonomy

Every knob has metadata: `name`, `group`, `label`, `explanation`, `type`, `min`/`max` (for numerics), `enum` (for choices), `default`, `current`, `origin` (`default | env | live`), `tier` (`live | next_restart | hardcoded`), `pipeline_stage` (which graph node it affects).

Groups, in pipeline order:

1. **mic** — `audio_in_sample_rate`, `audio_in_channels` (read-only, hardcoded; explain why 16 kHz is required by Silero).
2. **vad** — `vad_stop_secs`, `vad_start_secs`, `vad_min_volume`, `vad_confidence`. All four **live**.
3. **denoise** — `rnnoise_enabled` (live toggle), then read-only display of internal RNNoise mix params (`_dry_mix=0.35`, `_suppression_guard_ratio=0.18`, `_min_input_rms_for_guard=0.01`).
4. **echo** — `allow_interruptions` (live toggle), then read-only display of `PlaybackLeakageGuard` internals (`_open_ratio=3.5`, `_min_open_rms=0.045`, `_required_open_frames=8`, `_post_tts_cooldown_secs=0.25`, `_noise_floor_alpha=0.02`).
5. **stt** — `stt_provider` (live), `stt_prompt_enabled`, `stt_prompt`, `stt_thresholds_path`. Per-language thresholds rendered as a small table with a "Reload from disk" button (uses existing `STTThresholds.reload()`).
6. **transcript_filter** — read-only; one card per rule in `TranscriptionNoiseFilter`. Each card shows the threshold and a "dropped in last hour" counter pulled from the trace stream.
7. **llm** — `LLM_MODEL` (read-only; `_VALID_LLM_MODELS` set is shown), system prompt (collapsible).
8. **tts** — `tts_provider` (live), `default_tts_voice` (live; dropdown of `_VALID_OPENAI_TTS_VOICES` or `_VALID_GOOGLE_TTS_VOICES` based on active provider), `google_tts_enabled` (read-only).
9. **audio_out** — `audio_out_sample_rate`, channels (read-only with the chipmunk-bug explanation from `tts.py`).
10. **flow** — `pipeline_idle_timeout_secs`, `greeting_language`, `input_mode`, `rag_enabled`, `actions_enabled`. Mostly `next_restart`.

## How live editing works

1. User drags a slider in `/trace.html`. Optimistic UI: card flashes blue, spinner appears.
2. Browser `POST /api/admin/tune {knob, value}` to serve.py.
3. Serve.py validates the X-Admin-Token, looks up the bot's tune port from `BotSessionRegistry`, forwards `POST http://127.0.0.1:PORT/tune`.
4. Bot's `TuneServer` looks up the knob in the registry. Validates type / range. If invalid → 400 with reason.
5. If valid, bot calls the knob's `apply(new_value)` function. For example:
   - `vad_stop_secs.apply(0.3)` does `vad_processor._vad_analyzer._params.stop_secs = 0.3` and logs the change.
   - `allow_interruptions.apply(true)` flips the flag on the live `PlaybackLeakageGuard` and `BotActiveUserFrameSuppressor` instances.
   - `default_tts_voice.apply("alloy")` pushes a `TTSUpdateSettingsFrame` upstream — same machinery `ModelSwitcher` already uses.
6. Bot emits a `knob` trace event. Serve.py fans it to all connected dashboards.
7. Dashboard sees the event, removes the spinner, updates the value, flashes the card green.
8. If anything fails, the dashboard reverts the slider and shows a red toast with the error.

The full round-trip should complete in well under 200 ms on `localhost`. If it doesn't return in 1 s, the dashboard reverts the slider and shows "Bot didn't respond — restart may be required."

## File-by-file change list

| File | Change |
| ---- | ------ |
| `src/voxtera/trace.py` (new) | `TraceEvent` dataclass, `TraceBus` (in-process queue + ring buffer), `TraceForwarder` async task that POSTs events to launcher, `TurnTracker` for `turn_id` correlation. |
| `src/voxtera/tunables.py` (new) | `Tunable` dataclass, `Registry`, validators, apply functions for the v1 live knobs, metadata for read-only knobs. |
| `src/voxtera/trace_server.py` (new) | aiohttp server bound to `127.0.0.1:VOXTERA_BOT_PORT`. Routes: `POST /tune`, `GET /knobs`, `GET /health`. |
| `src/voxtera/pipeline.py` | `PipelineProbe` also pushes events to `TraceBus`. `PipelineTracer` emits `stage` events for STT/LLM/TTS/transport durations. New stage timer processors for `stt_ms`, `llm_ttft_ms`, `tts_ttft_ms`. Tune-server start/stop wiring. |
| `src/voxtera/audio.py` | `RNNoiseDenoiser`, `PlaybackLeakageGuard`, `BotActiveUserFrameSuppressor`, `TranscriptionNoiseFilter` register references with the tunables registry so live edits flow to them. `TranscriptionNoiseFilter` emits a `frame` event with the rule that dropped the transcript when one fires. |
| `src/voxtera/bot.py` | Start `TraceBus`, `TraceForwarder`, `TuneServer` as background tasks alongside the pipeline. Stop them cleanly in the `finally` block. |
| `src/voxtera/config.py` | Add `voxtera_bot_port`, `voxtera_trace_enabled` to `Settings`. |
| `demo-hotel/serve.py` | Add `_handle_trace_stream` (SSE), `_handle_trace_snapshot`, `_handle_admin_tune`. Add `_TraceEventBuffer` global. Extend `_handle_bot_event` to route `type=trace` events to the buffer. Track per-session bot tune port in `BotSessionRegistry`. Pass `VOXTERA_BOT_PORT` env to spawned bot. |
| `demo-hotel/trace.html` (new) | The dashboard. Vital-signs strip, pipeline graph, turn timeline, audio scope, frame waterfall, knobs panel. ~800 LOC of vanilla JS + CSS, no build step. |
| `demo-hotel/demo.html` | Small trace icon-button in the header (next to the logo). `target="_blank"` to `/trace.html`. |
| `demo-hotel/chat.html` | Same trace icon-button. |
| `.env.example` | Add `VOXTERA_TRACE_ENABLED=true` (comment that this is for dev). |
| `docs/setup.md` | One paragraph: "Open `http://localhost:8080/trace.html` to see the live pipeline view; needs `VOXTERA_ADMIN_TOKEN` set." |
| `tests/test_tunables.py` (new) | Validator + apply round-trip tests for each v1 knob. Mocked pipeline. |
| `tests/test_trace_bus.py` (new) | Ring buffer wrap, fan-out to multiple subscribers, schema versioning. |

## Implementation order

1. **Plan doc** (this file) — ensure design is recorded before writing 1500+ LOC.
2. **trace.py** — foundation. Standalone, easy to test.
3. **tunables.py** — registry + validators. Standalone, easy to test.
4. **trace_server.py** — small aiohttp server, depends on tunables.
5. **Wire into pipeline.py + audio.py + bot.py** — the bot side is now end-to-end testable in isolation by hitting `127.0.0.1:VOXTERA_BOT_PORT` with curl.
6. **serve.py extensions** — SSE + snapshot + tune proxy. Now end-to-end testable from a browser tab pointed at `serve.py`.
7. **trace.html** — the UI. Built last because the data plane needs to be working first; otherwise we fight ghosts.
8. **Buttons on demo.html / chat.html** — trivially small.
9. **Tests** — registry round-trips, ring buffer, SSE basics.
10. **README / setup.md** — one-paragraph docs update.

Stop at step 6 if priorities change — at that point you have a working `curl`-able trace, just no GUI yet.

## Open questions

1. **Persistence of live edits.** v1 is in-memory only. The user has signaled v2 will write to a JSON file. Decision: when implementing the JSON layer, the `Settings` precedence becomes `defaults < .env < runtime_overrides.json < live`. Live edits write to the JSON synchronously so on-demand bots inherit them.
2. **Dropping the bot's tune port for legacy mode.** When `make run` is used (no launcher), serve.py doesn't know the bot's tune port. Either (a) bot announces it via the existing launcher callback even outside launcher mode, or (b) bot uses a fixed default port (`9091`) configurable via env. Option (b) is simpler for v1 — pick that, document it in `.env.example`.
3. **Cross-tab broadcast.** When two `/trace.html` tabs are open, both should see live edits made in either. The SSE fan-out gives this for free for `knob` trace events. No additional plumbing required.
4. **Audit log of edits.** v1 keeps tune edits only in the trace stream and logs. v2 should write a structured `logs/tuning.jsonl` line per edit so post-hoc forensics don't depend on log scrollback.
