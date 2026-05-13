# Voxtera Voice Pipeline — Production Configuration and Latency Decisions

**Status:** living document, last revision May 2026
**Audience:** developers, partners, technical clients

This document captures the recommended production configuration for the
Voxtera voice pipeline, the rationale behind every choice, expected
latency under each option, and known failure modes encountered during
benchmarking sessions from Toronto and Istanbul.

---

## 1. TL;DR — recommended config

Pick these in the demo UI dropdowns; set these as `.env` defaults so a
restarted bot lands here automatically:

| Dropdown | Pick |
|---|---|
| STT model | **Whisper** (`whisper-1`) |
| LLM model | **Claude Haiku 4.5 (fast)** (`claude-haiku-4-5-20251001`) |
| TTS provider | **Google Chirp 3 HD** |
| TTS voice | **Charon (en-US)** — locale auto-switches on detected language |
| Microphone | **Real microphone** (NOT BlackHole or any virtual device) |

Server `.env` (Toronto droplet):

```
ALLOW_INTERRUPTIONS=true
RNNOISE_ENABLED=true
STT_PROVIDER=whisper
TTS_PROVIDER=google
DEFAULT_TTS_VOICE=en-US-Chirp3-HD-Charon
INPUT_MODE=hybrid
RAG_ENABLED=true
HOTEL_ID=demo
```

After editing `.env`, run `scripts/deploy-droplet.sh` to sync to the
server and restart the launcher + bot systemd units.

---

## 2. Why each component was chosen

### 2.1 STT — Whisper (OpenAI `whisper-1`)

**Chosen because:**
- 99 languages with automatic detection — Voxtera's tourism positioning
  requires multilingual STT without user-side language selection.
- Measured 712 ms STT latency from the Toronto droplet on the benchmark
  sentence "What time does breakfast start?" — fastest in the comparison.
- Clean transcripts after the leakage-guard relaxation (see §6).
- Single global endpoint, no regional configuration needed.

**Rejected alternatives:**

| Alternative | Why rejected |
|---|---|
| Google `chirp_2` | us-central1 region-locked, English-only in our config, 1577 ms in Toronto — wrong tool for multilingual tourism. |
| Google `latest_long` | Tuned for long-form audio; ~1100 ms on short conversational queries; only en/es/fr enabled in our config. |
| Google `latest_short` | Future upgrade candidate. Multilingual support requires verification with `scripts/verify_google_stt.py`. |
| Deepgram `nova-3` | 14 languages — missing Turkish, Romanian, Armenian. Too narrow. |

**Trade-off accepted:** Whisper is batch (no streaming). For very long
utterances (>3 s) a streaming STT would win; for typical concierge
queries (1-2 s) batch wins because there's no pipelining overhead.

### 2.2 LLM — Claude Haiku 4.5 (`claude-haiku-4-5-20251001`)

**Chosen because:**
- Fastest TTFT in the Claude family — measured 350-500 ms with prompt
  caching warm.
- Prompt caching enabled in `pipeline.py` — caches system prompt and
  tool schemas across turns for 5 minutes, eliminating ~150 ms of
  TTFT after turn 1.
- Excellent multilingual response quality across Turkish, Armenian,
  Romanian, French, Spanish — no instruction-tuning gap vs Sonnet for
  factual concierge Q&A.
- `max_tokens=250` cap prevents runaway generation while staying well
  above the brevity prompt's ~25-word target.
- Cost: $0.80 / $4 per million input/output tokens — 1/4 the cost of
  Sonnet 4.6.

**Rejected alternatives:**

| Alternative | Why rejected |
|---|---|
| Claude Sonnet 4.6 | +200-400 ms TTFT for marginal quality gain on factual concierge work. Right choice if responses need more empathy. |
| Claude Opus 4.7 | 800-2000 ms TTFT — disqualifying for real-time voice. |
| OpenAI GPT-4o-mini | Comparable speed but weaker non-English responses. Less mature in Pipecat. |
| Self-hosted Llama | High operational complexity, modest latency gain, lower output quality. |

### 2.3 TTS — Google Chirp 3 HD (`en-US-Chirp3-HD-Charon`)

**Chosen because:**
- Measured 230 ms first-byte latency from the Toronto droplet — 8× faster
  than OpenAI tts-1.
- gRPC streaming with `text_aggregation_mode=TOKEN` (configured in
  `tts.py`) so audio synthesis begins as soon as the LLM emits the
  first token. No sentence-aggregation wait.
- 75+ language coverage with locale switching — `AutoTTSLanguageSwitcher`
  in `controllers.py` automatically swaps `en-US-Chirp3-HD-Charon`
  → `tr-TR-Chirp3-HD-Charon` when Whisper detects Turkish, preserving
  the same voice character.
- 1 M characters/month free, then $30/M characters.

**Rejected alternatives:**

| Alternative | Why rejected |
|---|---|
| OpenAI tts-1 (nova) | ~1900 ms TTFT from Toronto, ~1700 ms from Istanbul — the dominant latency cost when used. Voice quality marginally richer; not worth the 1.5-1.7 s delay. |
| ElevenLabs Flash v2.5 | 75 ms TTFT (excellent), but only 32 languages — missing Armenian and several others. Strong candidate for English/Turkish-only deployments. |
| ElevenLabs Eleven v3 | 70+ languages including Armenian. Higher latency than Flash, but still competitive with Chirp 3 HD. Worth evaluating for clients needing rare languages. |
| Deepgram Aura-2 | Only 7 languages. Too narrow. |
| AWS Polly Neural | No modern streaming gRPC interface; TTFT comparable to OpenAI tts-1. |

---

## 3. Expected latency by tier

The dashboard reports "perceived latency" = the time between user
silence (VAD stop) and bot audio first byte. Add ~300 ms of VAD
stop-debounce time for the actual subjective experience.

| Tier | Dashboard P50 | Subjective feel | Achievable with |
|---|---|---|---|
| Indistinguishable from human | <500 ms | "Same as another person in the room" | OpenAI Realtime, Sesame-class models — not Voxtera |
| Excellent | 500-1000 ms | Snappy, no awkwardness | ElevenLabs Flash + streaming STT, possibly self-hosted Whisper |
| Good and professional | 1000-1500 ms | Slight perceptible delay, intentional feel | **Recommended Voxtera config (warm cache)** |
| Acceptable | 1500-2500 ms | Noticeably slow but workable | Recommended config on cold-cache first turns |
| Sluggish | 2500-4000 ms | User repeats themselves | Wrong-config deployments (OpenAI tts-1 etc.) |
| Broken | >4000 ms | Conversation collapses | Failure modes / wrong mic |

**Measured:** with the recommended config, "What time does breakfast
start?" lands at **~1464 ms end-to-end from Toronto** after RAG
warm-up completes (turn 2+). First turn of a session is slower because
RAG cache and prompt cache are cold.

---

## 4. Geography considerations

The bot runs on a DigitalOcean droplet in Toronto. The bot's
location affects which AI service endpoints respond fastest:

| Bot → service | Latency from Toronto |
|---|---|
| OpenAI (US-East) | ~30 ms RTT — fast |
| Anthropic (US) | ~30 ms RTT — fast |
| Google (Global STT/TTS, picks nearest POP) | ~10-30 ms RTT — fast |

User location only affects user → Daily.co edge → bot:

| User location | Likely Daily edge | Edge → Toronto bot | Total user → bot |
|---|---|---|---|
| Canada (Toronto) | Toronto | <10 ms | ~20-30 ms |
| Western Europe (Istanbul, Frankfurt) | Frankfurt | ~80-100 ms transatlantic | ~110-130 ms |
| Eastern Europe / Caucasus (Armenia) | Frankfurt or Amsterdam | ~80-100 ms | ~130-160 ms |

For a permanent European deployment, spinning up a Frankfurt droplet
would shave 80-100 ms off every turn for EU-based users.

---

## 5. Code-level optimizations applied

Each of these is already shipped to `main` and active when the
recommended config is selected:

1. **System prompt brevity rules** (`src/voxtera/prompts/system_prompt.py`):
   1-2 sentence cap (~25 words), no padding, no re-introductions,
   no markdown leakage, anti-echo rule. Cuts bot speech duration from
   8-15 s to 2-4 s.

2. **TTS token-level streaming** (`src/voxtera/tts.py`):
   `text_aggregation_mode=TextAggregationMode.TOKEN` on Google TTS —
   LLM tokens stream to TTS immediately instead of waiting for sentence
   boundaries. Saves ~400 ms LLM→TTS gap.

3. **Anthropic prompt caching** (`src/voxtera/pipeline.py`):
   `enable_prompt_caching=True`, `max_tokens=250`. Saves ~150 ms LLM
   TTFT on turn 2+ within a 5-minute window.

4. **Leakage guard relaxed for AirPods/headsets**
   (`src/voxtera/audio.py` — `PlaybackLeakageGuard.__init__`):
   - `_post_tts_cooldown_secs`: 0.25 → 0.10 s
   - `_required_open_frames`: 8 → 3 (~60 ms gate-open time)
   - `_min_open_rms`: 0.045 → 0.025
   - Watchdog timeouts: clears stuck `_bot_speaking` / `_bot_thinking`
     flags after 10-20 s if End frames go missing.
   - `InterruptionFrame` handler: clears all state on interruption.

5. **RAG result cache** (`src/voxtera/rag/retriever.py`):
   Per-session LRU cache of (query → results), eliminating the
   ~200-1500 ms CPU cost of `multilingual-e5-small` query embedding
   on repeat queries.

6. **RAG warm-up at session start** (`src/voxtera/pipeline.py` —
   `_on_joined`): pre-runs `DEFAULT_WARMUP_QUERIES` after the bot
   joins. Common questions (breakfast, restaurants, gym, parking,
   coffee shop, etc.) return in ~0 ms from the second turn onward
   (cache populated during the first 3-4 seconds of the session).

7. **InterruptionFrame state-machine fix** (`src/voxtera/audio.py`):
   `PlaybackLeakageGuard` and `BotActiveUserFrameSuppressor` now
   listen for `InterruptionFrame` and reset all internal state. Fixes
   the "deaf after interrupt" bug.

---

## 6. Known failure modes and diagnosis

### 6.1 Silent mic / BlackHole on Chrome (macOS)

**Symptom:** VAD fires multiple times per session (75+ events in 40 s)
but `TranscriptionFrame` count is zero. No bot replies. No errors
logged. Dashboard shows ghost turns (0 ms entries).

**Cause:** Chrome on macOS sometimes selects the BlackHole virtual
audio device as the default input. `getUserMedia` returns a stream
with no actual audio. Silero VAD twitches on the noise floor.

**Diagnosis:**
- 75 VAD events per 40 s is ~1 every 0.5 s — too regular for real speech.
- 0 transcripts AND 0 errors is the signature.

**Fix:** Explicitly select the real microphone in the demo UI mic
dropdown. Safari does NOT have this problem.

### 6.2 Ghost turns from speaking over bot reply

**Symptom:** A turn appears in the dashboard with VAD-started /
VAD-stopped events but no transcript. Bot doesn't reply.

**Cause:** User spoke while bot was still in `_bot_speaking=True` state
(reply audio still playing, or state machine slightly lagged).
Leakage-guard barge-in gate didn't open because user's voice didn't
sustain enough energy above the 0.025 RMS threshold for 3 consecutive
frames (~60 ms).

**Diagnosis:** Tooltip on `leakage_guard` shows drops with reason
`barge_in_closed` clustered in that turn's time window.

**Mitigations:**
- Wait ~1 s after the bot stops talking before speaking again (UX).
- Speak slightly louder / closer to the mic.
- If recurring, lower `_min_open_rms` further (0.025 → 0.015) and/or
  `_required_open_frames` (3 → 2).

### 6.3 `ALLOW_INTERRUPTIONS=false` on server (legacy state)

**Symptom:** Lots of silenced frames with reason `bot_active_strict`.
All user audio during bot speech is silenced unconditionally — no
barge-in possible.

**Cause:** Server `.env` doesn't have `ALLOW_INTERRUPTIONS=true`. The
default in `config.py` is False.

**Fix:** Set `ALLOW_INTERRUPTIONS=true` in the server's `.env`, then
run `scripts/deploy-droplet.sh` or `systemctl restart voxtera` on the
droplet so the value is picked up.

### 6.4 Wrong-provider regression after launcher restart

**Symptom:** Active Providers panel shows `google/latest_long` +
`openai/tts-1` instead of `whisper/whisper-1` + `google/chirp3-hd`.
End-to-end P50 is 4000-5000 ms.

**Cause:** Demo UI dropdowns reset to defaults on page reload.
The bot still spawns with whatever the env/dropdown says.

**Fix:** Always verify the Active Providers panel matches the
recommended config at the START of every benchmark session.

### 6.5 Wrong configuration deployed on server

**Symptom:** Local `.env` says `ALLOW_INTERRUPTIONS=true` but server
`.env` says `false`. Bot behavior on the server reflects the server's
config, not the laptop's.

**Cause:** `.env` deploy was not synced (or was hand-edited on the
server later).

**Fix:** `scripts/deploy-droplet.sh` copies the local `.env` to
`/etc/voxtera/voxtera.env` on the server. Run it.

---

## 7. Future work

### 7.1 Client-side telemetry (browser/mic logging)

When a session starts, log the browser user-agent, selected mic
device name, OS platform, language preference, and timezone. Helps
diagnose:
- Chrome-vs-Safari behavior differences (BlackHole, mic permissions).
- Which mic device was selected (catches BlackHole at a glance).
- Whether the user's locale matches the language they're speaking
  (e.g., Istanbul user with `tr-TR` locale).

Implementation: ~40 LOC across `demo.html` (send `voxtera-client-info`
app-message on connect) and `controllers.py` (handle the message,
emit a lifecycle trace event, log via loguru).

### 7.2 STT swap to streaming

`scripts/verify_google_stt.py` is built to validate that
`STT_MODEL_GOOGLE = "latest_short"` produces clean multilingual
transcripts before flipping the constant in `stt.py`. The latency
saving is ~200-300 ms if it works.

### 7.3 ElevenLabs as alternative TTS

Two relevant models:
- **Flash v2.5** (75 ms TTFT, 32 languages including Turkish/Romanian):
  fastest option, suitable when Armenian/Georgian/etc. aren't needed.
- **Eleven v3** (70+ languages including Armenian): broader coverage
  at the cost of higher latency. Worth A/B testing.

Integration is ~30 minutes (Pipecat has `ElevenLabsTTSService`).
Add to `_TTS_BUILDERS` dict and the demo UI's TTS provider dropdown.

### 7.4 Frankfurt droplet for European clients

For deployments serving Istanbul/Armenia clients regularly, a
Frankfurt droplet would save 80-100 ms per turn on the
user → Daily-edge → bot leg. Operational complexity: same as Toronto,
just `git pull && docker-compose up -d` on a second host.

### 7.5 Phrase-level interrupt UI

Right now interrupting the bot requires the user's voice to cross
the barge-in threshold. For demos where the partner wants reliable
interrupt without overcoming audio thresholds, a "tap to interrupt"
button in the demo UI would help. ~50 LOC change.

---

## 8. Configuration verification checklist

Before every benchmark/demo session:

1. ✅ Server `.env` contains `ALLOW_INTERRUPTIONS=true`
2. ✅ Demo UI dropdowns set to: Whisper / Haiku 4.5 / Google Chirp 3 HD / Charon (en-US)
3. ✅ Microphone dropdown shows the real device (not BlackHole)
4. ✅ Active Providers panel reads `whisper/whisper-1` and `google/chirp3-hd`
5. ✅ Trace dashboard "Clear waterfall" clicked to wipe stale stats
6. ✅ For benchmarking: first turn is discarded (cache-warming overhead)
7. ✅ For real conversation: bot reply finished playing through speakers
   before asking the next question (avoid the `_bot_speaking` overlap)

If any of those fail, the latency will be 2-3× worse than the measured
optimal and the demo will feel sluggish.

---

## Appendix A — benchmark sentences

Use these exact phrases for repeatable latency measurement. Same
phrase × multiple trials reduces variance from STT load and reply
length.

| Sentence | Tests |
|---|---|
| "What time does breakfast start?" | Baseline factual lookup, simple RAG hit |
| "What restaurants do you have?" | List/enumeration response (~35-word budget) |
| "Where is the gym?" | Short factual (~10 words back) |
| "Do you have parking?" | Yes/no + redirect ("Don't know — call the desk") |
| "Can you recommend a coffee shop nearby?" | RAG retrieval, longer reply |
| "How do I get to the airport?" | Multi-step instruction (first-step rule) |
| "Otelinizde hangi restoranlar var?" | Turkish — auto language switch test |

Avoid sentences that trigger `create_ticket` (e.g., "My AC isn't
working") unless you're specifically testing the actions/Telegram
flow — those add multi-step LLM cycles and aren't representative of
typical concierge latency.

---

## Appendix B — trace event taxonomy

The trace dashboard reports these stage durations per turn:

| Stage | What it measures |
|---|---|
| `stt` | VAD-stopped → final transcript event |
| `stt_to_llm` | Transcript → LLM call dispatched (includes RAG retrieval) |
| `llm_ttft` | LLM call dispatched → first LLM token |
| `llm_full` | LLM call dispatched → LLM end (background overlay) |
| `llm_ttft_to_tts` | First LLM token → first text frame to TTS |
| `tts_ttft` | TTS request → first audio byte arriving at transport_out |
| `end_to_end` | VAD-stopped → first audio byte (== sum of the perceived-latency stages above) |

The `bg-stage` overlay (LLM full duration, LLM end → TTS) shows
background work that overlaps with the perceived-latency path —
useful for spotting when LLM streaming runs in parallel with TTS
synthesis vs serially.
