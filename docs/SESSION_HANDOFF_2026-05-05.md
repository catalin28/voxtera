# Voxtera — Session Handoff (2026-05-05 evening)

This document captures the state at the end of a long debugging session so a
fresh conversation can pick up without losing context. It covers what was
shipped today, what was tried and reverted, the active open issue (Chrome +
AirPods Pro publishing silent audio to Daily), and the proposed engineering
plan for getting the demo to a reliably-working state across all browsers.

## TL;DR

A demo is coming up. The user wants the bot to work reliably for any user on
any browser — "perfect, not patched". Today we landed several real fixes,
discovered one real launcher bug, and uncovered one platform-level audio
quirk that's currently blocking Chrome on the demo machine. Three more
changes need to be built next session.

The user's constraints worth knowing:

- Demo languages: **English, French, Romanian, Russian, Turkish, Azerbaijani, Arabic**.
- The user wants to keep using **AirPods Pro for audio output** (and ideally
  for input too, but input is broken — see open issue #1).
- The user does **not** want to switch to MacBook built-in mic.
- Testing platform: **macOS, Safari 26.1, Chrome 147**.
- The user prefers **step-by-step guidance** with confirmation between
  steps, not large multi-step plans.

## What was shipped today (all in `voxtera/` repo)

### 1. LLMRunGuard interrupt fix
**File:** `src/voxtera/controllers.py`
**What:** The refractory window (`min_run_interval_secs=2.5`) used to silently
drop a post-interruption `LLMRunFrame`, causing the bot to go permanently
silent after a barge-in. Fixed by clearing `_last_run_sent_at` on
`BotStoppedSpeakingFrame` or `InterruptionFrame`.
**Status:** Committed and verified by static syntax + import check.
Note: pipecat 1.0.0 uses `InterruptionFrame`, NOT `StartInterruptionFrame`
(my first attempt had the wrong name; corrected mid-session).

### 2. Whisper avg_logprob confidence filter (per-language thresholds)
**Files:**
- `src/voxtera/stt_thresholds.py` — new module: `STTThresholds` loader
  with four-step lookup chain (canonical resolution → JSON entry →
  JSON `default` → hardcoded fallback `-1.0 / 0.7`)
- `src/voxtera/stt.py` — `_MultilingualWhisperSTT._transcribe` now reads
  `result.segments`, drops transcriptions where worst `avg_logprob` <
  threshold OR worst `no_speech_prob` > threshold
- `config/stt_thresholds.json` — pre-tuned for the seven demo languages
- `config/stt_thresholds.README.md` — schema docs and tuning guide
- `src/voxtera/config.py` — added `stt_thresholds_path` setting
- `tests/test_stt_thresholds.py` — 18 unit tests covering every fallback
**Behavior:** When dropping a low-confidence transcription, we do NOT
update `last_detected_language` (so `AutoTTSLanguageSwitcher` doesn't
flick TTS to a misdetected language on noise).
**Status:** All 18 tests pass. Verified working in production log:
`[stt] detected language: english (avg_logprob ok)` line confirms accept
path; no false-positive drops observed.

### 3. VAD_STOP_SECS reduced from 0.5 → 0.2
**File:** `.env` (local — not yet pushed to Droplet)
**Why:** Pipecat's `TurnAnalyzerUserTurnStopStrategy` was warning that
0.5 differs from the recommended default. Verified by static analysis
that `.env → settings.vad_stop_secs → VADParams.stop_secs → frame →
TurnAnalyzer comparison` chain is the only path; no shadow override.
Saves ~300ms per turn.
**Status:** Local only. Not deployed to the Droplet yet.

### 4. `?audio=element` URL override (diagnostic)
**File:** `demo-hotel/demo.html`
**What:** URL query param to force the audio path. Mostly a diagnostic
tool now since the default became `<audio>` element for all browsers
(see #5).

### 5. Default audio path flipped to `<audio>` element for all browsers
**File:** `demo-hotel/demo.html` (line ~256)
**Why:** Safari 26.1's WebAudio path (MediaStreamAudioSourceNode + muted
primer) silently produces zero samples — `attach OK, ctx.state=running`
but no audio out. The `<audio>` element path works in all browsers
including Safari.
**Trade-off:** User confirmed Safari audio is now "a bit robotic" via
the `<audio>` path — the WebRTC jitter buffer issue the original code
comment warned about. Acceptable for the demo. The WebAudio path
remains available via `?audio=webaudio` for future regression triage.

## What was tried and reverted

### Reverted: `STT_MODEL_GOOGLE = "chirp_2"` → back to `latest_long`
**File:** `src/voxtera/stt.py`
**Why:** When Chrome was set to use Google STT, `chirp_2` produced no
transcripts at all. Most likely causes: region restriction (chirp_2 is
only available in us-central1, europe-west4, asia-southeast1 as of
mid-2025), or it doesn't accept the multi-language config flags this
builder passes. Reverted to `latest_long` (broadly available, slow
~1000ms but works).
**Documented:** The current comment in `stt.py` notes `latest_short`
as the safe streaming alternative if we want to retry latency
optimization later.

### Reverted: Default STT/TTS providers in `demo.html` → back to Whisper / OpenAI tts-1
**File:** `demo-hotel/demo.html`
**Why:** Initial change flipped defaults to Google STT + Google Chirp 3 HD TTS
to gain latency. STT default change exposed the `chirp_2` failure (above);
TTS default change broke Safari audio output (Chirp 3 HD's 24 kHz output
through Safari's WebAudio MediaStreamAudioSourceNode produced silence).
The user can still manually pick Google providers from the comboboxes for
testing.

## Performance baseline (from server log analysis earlier in session)

- STT (Whisper batch): P50 ~1000ms, P95 ~2500ms
- LLM (Haiku 4.5): P50 ~1066ms, P95 ~2648ms
- TTS warmup (OpenAI tts-1): P50 ~1000ms, P95 ~2500ms
- Total user-perceived latency: P50 ~3420ms, P95 ~6921ms

The VAD_STOP_SECS=0.2 change (once deployed) should cut ~300ms off P50.
Further latency wins are possible via `latest_short` STT and Google TTS but
were not pursued today after the failures above.

## OPEN ISSUE #1 — Chrome + AirPods Pro publishes silent audio to Daily

**Symptom:** When user speaks in Chrome with AirPods Pro selected as macOS
input device, the bot's pipeline shows audio frames flowing
(`[probe:after_leakage_guard] audio_in: 250 frames/5s`) but
`RMS avg=0.0000 peak=0.0000` for the entire utterance. Compare to working
Safari session same machine same mic: `RMS avg=0.0193 peak=0.1873`.

**Diagnostic facts established:**
- This happens in **Chrome incognito** with all other Chrome windows closed,
  no extensions, fresh profile → ruled out tab/extension interference.
- macOS System Settings → Sound → Input → AirPods Pro selected, input level
  meter shows weak activity (~3-4 dots out of 16 lit) when user speaks → so
  macOS itself IS receiving audio from AirPods, just very low.
- Mic permission is granted: macOS Privacy & Security → Microphone → Google
  Chrome toggled ON. Chrome's lock-icon shows mic permission ON.
- The bot's launcher had a stuck zombie session earlier — that was a
  separate bug (see Open Issue #3) and we confirmed it doesn't explain the
  current Chrome silence.

**Hypothesis:** The known Chrome + macOS + AirPods Pro Bluetooth WebRTC bug.
Chrome's `getUserMedia` with WebRTC audio constraints (echo cancellation,
auto-gain control, noise suppression) interacts poorly with the AirPods
Bluetooth audio profile. macOS native apps see the mic, browser WebRTC
gets silence (or near-silence below VAD floor of 0.02).

**The user explicitly does NOT want to switch to MacBook built-in mic** for
the demo. So the simple workaround "use built-in mic for input, AirPods for
output" is off the table.

**What needs investigating next session:**

1. Test whether Chrome WebRTC works with AirPods on a different machine /
   different version of macOS — is this universal or specific to this setup?
2. Try Chrome flags for WebRTC audio processing:
   `chrome://flags/#enable-experimental-web-platform-features`,
   `chrome://flags/#disable-features=MediaStreamTrackUseConfigMaxBitrate`.
   May need to disable Chrome's auto-gain/AEC entirely for AirPods.
3. Try Daily's `audioSource` override with custom constraints —
   `audioSource: { autoGainControl: false, echoCancellation: false,
   noiseSuppression: false }` — might bypass Chrome's processing pipeline.
4. Try a different mic profile in macOS: switch AirPods to "AirPods Pro"
   (always-on stereo) vs the auto-selected SCO profile that activates the
   mic. macOS Bluetooth menu → AirPods → check codec.
5. Check whether Brave (Chromium) has the same problem. If Brave works
   with AirPods but Chrome doesn't, it's a Chrome-specific WebRTC quirk.

**The "perfect, not patched" answer:** build the in-UI mic energy meter
(see Plan, Phase 1) so the user can SEE the silent stream before the
demo and switch input devices proactively. Right now this issue is
invisible until the bot doesn't respond.

## OPEN ISSUE #2 — Safari `<audio>` element audio is "a bit robotic"

**Symptom:** With the new default `<audio>` element audio path, Safari plays
the bot's voice but it's "a bit robotic" / glitchy due to Safari's WebRTC
jitter buffer.

**Workaround in place:** Acceptable for the demo (better than silent).
**Proper fix:** AudioWorklet output path (Phase 2, see Plan below).

## OPEN ISSUE #3 — Launcher single-slot lock can leak

**File:** `demo-hotel/serve.py` — `BotSessionRegistry`
**Symptom:** Today the launcher held a slot for ~10+ minutes after a bot
subprocess wedged. New `POST /api/start-session` requests returned 409
"another session is active". Daily dashboard showed 0 active sessions.

**Root cause:** The reaper thread blocks on `proc.wait()`. If a bot
subprocess is alive but unresponsive (Daily disconnected, Telegram listener
deadlock, etc.), `proc.wait()` never returns, slot stays locked forever.

**Workaround used today:** `kill <pid>` of the wedged bot subprocess
(SIGTERM caused `rc=-15` reap, slot freed).

**Proper fix:** Phase 3 below — timeout watchdog + Daily presence health
check + admin force-end endpoint + UI 409 recovery button. Sketched in
detail in earlier conversation. Existing admin endpoints (`/api/admin/{health,sessions,eject,end-session}`)
act on Daily participants, not on the launcher subprocess — the new endpoint
needs to kill the subprocess and call `REGISTRY.reap()`.

## RECOMMENDED PLAN — what next session should build

### Phase 1: in-UI diagnostics (estimate: 1-2 hours)
**Why first:** Most of today's debugging time was burned digging server
logs to find issues that should have been visible to the user immediately.
This is the highest-leverage fix.

Build into `demo-hotel/demo.html`:

1. **Live mic energy meter on the call page.** The codebase already has
   `micAudioCtx` and analyzer logic for the test-mic page; reuse it during
   the active call so the user sees their own mic level in real time.
2. **"Mic appears silent" warning.** If meter stays flat for >5s after
   `bot-done-speaking`, show a visible banner: "Microphone may not be
   capturing audio. Check your input device in System Settings → Sound."
3. **Pre-flight permission + device check on Start.** Before joining
   Daily, explicitly call `navigator.mediaDevices.getUserMedia({audio: true})`
   and surface clear errors: permission denied / no device / device
   in use.
4. **Bot output meter.** Mirror the local mic meter for the bot's audio
   stream so the user can see if the bot is actually emitting audio when
   `bot-speaking` fires.

This phase makes Open Issue #1 (Chrome + AirPods silence) visible to the
user instead of mysterious. They'll see "my mic isn't being heard" and
self-correct.

### Phase 2: AudioWorklet output (estimate: half day)
**Why:** Solves Open Issue #2 (Safari robotic audio) AND removes the
fragile WebAudio-vs-`<audio>` branching.

**What:** Replace `MediaStreamAudioSourceNode` + `<audio>` element with an
`AudioWorkletNode` that processes the WebRTC remote audio track with
explicit buffering. Works smoothly on Safari 14.1+, Chrome, Firefox,
Edge. Single audio path, no per-browser code.

### Phase 3: launcher hardening (estimate: 1 hour)
**Why:** Solves Open Issue #3 — prevents zombie-bot lockouts.

Four additions to `demo-hotel/serve.py`:

1. **Timeout watchdog thread:** auto-reap any session active for >30 min.
2. **Daily-presence health check:** every 60s, if launcher thinks a
   session is active but Daily presence shows no participants for 90s,
   reap. Reuses existing Daily REST helper from `_handle_admin_sessions`.
3. **`POST /api/admin/force-end-bot`:** auth via existing `_admin_auth()`,
   kills subprocess, calls `REGISTRY.reap()`. Distinct from existing
   `/api/admin/end-session` which only ejects from the Daily room and
   doesn't touch the launcher subprocess.
4. **UI 409 recovery:** when `demo.html` sees the 409 busy error, show
   a "Force end stuck session" button that calls the new endpoint and
   retries Start.

### Phase 4 (optional, post-demo): browser compatibility matrix + smoke test
Document tested browser+version combinations end-to-end. Spin up a tiny
Playwright test that records expected event sequences and asserts they
appear. Catches Safari/Chrome regressions on day zero, not during demos.

## Code-state pointers

For the next session to be productive, here are the "where to look" hooks:

- **VAD/audio probe pipeline:** `src/voxtera/pipeline.py:495-511`
  (`VADProcessor` + `SileroVADAnalyzer` setup using `settings.vad_*`)
- **Audio probe lines in logs:** `[probe:after_leakage_guard] audio_in: N
  frames/5s | RMS avg=X peak=Y` — RMS is the truth signal; if avg=0
  during user speech, the browser is publishing silence regardless of
  what the browser's local mic test shows.
- **Mic test code (reusable for live meter):** `demo-hotel/demo.html`,
  search for `micAudioCtx` and `setMicBars`.
- **Launcher session registry:** `demo-hotel/serve.py:118-185`
  (`BotSessionRegistry`).
- **Existing admin endpoints:** `demo-hotel/serve.py:533-802`
  (`/api/admin/health`, `sessions`, `eject`, `end-session`).
- **Threshold loader (today's work, fully tested):**
  `src/voxtera/stt_thresholds.py` and `tests/test_stt_thresholds.py`.

## A note on user collaboration style

The user prefers:
- **Step-by-step guidance with confirmation between steps**, not multi-step
  plans. Give one step, wait for "done" or feedback, then continue.
- **Concrete commands** rather than placeholders — never put `<your-log-path>`
  in a shell command, just give a runnable command or ask for the actual
  path first.
- **Honest correction over deflection** — when a previous suggestion
  caused breakage, lead the next message with the correction and the
  recovery, don't paper over.
- **Real engineering ("perfect") over patches** — when the user has time,
  invest in foundations (in-UI diagnostics, AudioWorklet, launcher
  hardening) rather than per-incident workarounds.

---

End of handoff. Next session should start by reading this file, confirming
which Phase to tackle first, and verifying the open Chrome + AirPods issue
is still reproducible (or if it's been worked around in the meantime).
