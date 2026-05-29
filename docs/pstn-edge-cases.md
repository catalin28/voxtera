# PSTN Telephony — Edge Cases & Development Status

**Phone:** +1 (226) 212-0379  
**Domain:** voxtera.daily.co  
**Branch:** `feat/dynamic-daily-rooms`  
**Last updated:** 2026-05-29

---

## Protection Layers (Implemented)

| # | Edge Case | Status | File(s) | Tested |
|---|-----------|--------|---------|--------|
| 1 | **Hard duration cap** — Kill call after N minutes (default 4 min) | ✅ Done | `serve.py` (duration enforcer) | ❌ |
| 2 | **30-sec warning before kill** — TTS "Your session is ending soon" | ✅ Done | `serve.py` (POST to /speak) | ❌ |
| 3 | **Pipeline idle timeout** — No frames flowing at all | ✅ Done | `pipeline.py` (PipelineParams.idle_timeout_secs) | ❌ |
| 4 | **Speech idle detection** — Caller silent too long → prompt → hangup | ✅ Done | `pipeline.py` (PstnIdleWatcher) | ❌ |
| 5 | **Dialin stopped/error** — Clean EndFrame on SIP disconnect | ✅ Done | `pipeline.py` (on_dialin_stopped, on_dialin_error) | ❌ |
| 6 | **Hold time measurement** — Track how long caller waits before bot answers | ✅ Done | `pipeline.py` + `serve.py` (PSTN_WEBHOOK_TS) | ❌ |

---

## Uncovered Edge Cases

### High Priority

| # | Edge Case | Description | Status | Mitigation Plan |
|---|-----------|-------------|--------|-----------------|
| 7 | **Toll fraud / webhook abuse** | No rate limiting on `/pstn/webhook`. Attacker or robocaller loop could spawn hundreds of bots, consuming Daily rooms + compute. | ✅ Done | In-memory rate limiter: max 3 calls per number per 5 min (`PSTN_RATE_LIMIT_PER_NUMBER`, `PSTN_RATE_LIMIT_WINDOW_SECS`). Returns 429. |
| 8 | **HMAC not enforced in prod** | `PSTN_WEBHOOK_HMAC` is empty for local dev. If deployed without setting it, anyone with the URL can spawn bots. | ⚠️ Partial | HMAC verification code exists but is skipped when env var is empty. Add startup warning log + refuse to start in production without it. |
| 9 | **Orphaned process on crash** | If bot segfaults/OOMs, Daily room stays open, caller hears silence until phone network times out (~60s). Duration enforcer dies with process. | ❌ TODO | External health-check from `serve.py`: if bot process exits unexpectedly, DELETE the Daily room via API so Daily drops the SIP leg. |
| 10 | **No goodbye on idle hangup** | Idle watcher says "Are you still there?" then silently ends call. Should say "Goodbye" before EndFrame. | ✅ Done | Added `TTSSpeakFrame(text="Goodbye.")` + 1.5s sleep before `EndFrame` in `PstnIdleWatcher._watch_loop`. |

### Medium Priority

| # | Edge Case | Description | Status | Mitigation Plan |
|---|-----------|-------------|--------|-----------------|
| 11 | **Hold music fools VAD** | Caller puts phone on hold → comfort tone/music triggers Silero VAD as "speech" → idle timer resets indefinitely → call stays until 4-min hard cap. | ❌ TODO | Detect repeated short VAD triggers without transcription results (music = VAD fires but STT returns empty). After N triggers with no text, treat as idle. |
| 12 | **Greeting clipped** | Bot fires greeting immediately on dialin-connected but caller's audio path may not be fully established (codec negotiation). First ~200ms lost. | ✅ Done | Added 500ms `asyncio.sleep` before greeting + greeting via `resolve_greeting()` in `on_dialin_connected`. |
| 13 | **Concurrent call limit** | No admission control. 10 simultaneous calls → 10 bots spawn → server overloaded. | ✅ Done | Already implemented: `PSTN_MAX_CONCURRENT_CALLS` (default 10) returns 503 when at capacity. |
| 14 | **Bot startup too slow** | Cold start (TTS/STT init, ONNX model load) can take 5-10s. Caller hears silence on hold. | ❌ TODO | Options: (a) pre-warm bot pool, (b) Daily comfort tone/hold music config, (c) reduce cold-start time. |
| 15 | **Caller hangs up during bot startup** | Caller abandons before bot joins room. Bot spawns, joins empty room, sits until health monitor kills it. | ❌ TODO | Check if SIP participant is still present on `on_joined`; if not, immediately EndFrame. |

### Lower Priority

| # | Edge Case | Description | Status | Mitigation Plan |
|---|-----------|-------------|--------|-----------------|
| 16 | **DTMF tones not handled** | Caller presses keypad — ignored. Some callers expect IVR navigation. | ❌ Backlog | Future: listen for DTMF events from Daily, map to actions (e.g., "0" = transfer to human). |
| 17 | **STT service outage** | If STT is down, bot can't hear caller. Responds with confused LLM output or silence. | ❌ Backlog | Detect N consecutive empty transcriptions → speak "I'm having trouble hearing you, please try again later" → EndFrame. |
| 18 | **TTS service outage** | If TTS is down, bot can't speak. Caller hears silence. | ❌ Backlog | Detect TTS error frame → fallback to pre-recorded WAV announcement → EndFrame. |
| 19 | **LLM infinite response** | LLM generates extremely long text → TTS plays for minutes. | ❌ Backlog | Per-utterance token limit on LLM (max_tokens already set). Also: TTS timeout — if single utterance > 30s, interrupt and move on. |
| 20 | **Webhook replay attack** | Without HMAC, old webhook payloads can be replayed. | ⚠️ Partial | HMAC handles freshness if Daily includes timestamp. Also: check `call_id` hasn't been seen before (dedup set with TTL). |
| 21 | **SIP reconnect / ouble-join** | PSTN call drops and reconnects (rare). Second `on_dialin_connected` fires. | ❌ Backlog | Guard: if watchdog already started, don't start a second. Reset idle timer on reconnect. |
| 22 | **Audio quality / STT accuracy at 8kHz** | Narrowband audio degrades STT word error rate. LLM may hallucinate from bad transcriptions. | ❌ Backlog | Tune STT confidence thresholds for PSTN. Consider Deepgram's `telephony` model variant. |

---

## Environment Variables (PSTN)

| Variable | Default | Description |
|----------|---------|-------------|
| `PSTN_ENABLED` | `false` | Enable PSTN webhook endpoint |
| `PSTN_MODE` | `pinless` | Dial-in mode (only `pinless` supported) |
| `PSTN_PHONE_NUMBER` | — | The phone number (for logging/display) |
| `PSTN_MAX_DURATION_MIN` | `4` | Hard call duration cap in minutes |
| `PSTN_WEBHOOK_HMAC` | — | HMAC secret for webhook verification (required in prod) |
| `PSTN_WEBHOOK_TS` | — | Internal: timestamp passed to bot subprocess |
| `PSTN_IDLE_TIMEOUT_SECS` | `45` | Seconds of silence before "Are you still there?" |
| `PSTN_IDLE_FOLLOWUP_SECS` | `15` | Seconds after prompt before auto-hangup |
| `PSTN_MAX_CONCURRENT_CALLS` | `10` | Max simultaneous PSTN calls (returns 503) |
| `PSTN_RATE_LIMIT_PER_NUMBER` | `3` | Max calls per phone number within window |
| `PSTN_RATE_LIMIT_WINDOW_SECS` | `300` | Sliding window for per-number rate limit (5 min) |

---

## Testing Checklist

- [ ] Call the number, speak normally, verify bot responds
- [ ] Call and stay silent for 45s → verify "Are you still there?" plays
- [ ] Stay silent 15s more → verify call ends
- [ ] Call and talk for >4 minutes → verify 30s warning + disconnect
- [ ] Call and hang up mid-conversation → verify bot process exits cleanly
- [ ] Verify HMAC rejection with wrong/missing signature
- [ ] Verify hold time is logged correctly
- [ ] Verify bot startup time (dialin-ready → dialin-connected)
- [ ] Simulate concurrent calls at rate limit boundary
- [ ] Test with poor audio quality / background noise
