# Voxtera — Audio Quality Debug Guide

**Context:** Testers report bad/muffled audio in Gladia transcripts. Spectrogram analysis of a real session shows the audio reaching Gladia is already bandlimited (no energy above ~3 kHz), meaning the problem is upstream of Gladia — somewhere between the tester's microphone and what we send over the wire.

This document is what to implement to (a) stop the most common quality-killers and (b) capture enough diagnostic data to identify the cause within seconds when a report comes in.

---

## 1. Fix the `getUserMedia` constraints

The current constraints likely leave noise suppression / AEC / AGC at browser defaults (= ON). Browser DSP is tuned for video calls, not STT, and it strips high frequencies and adds artifacts.

Replace whatever we have today with this:

```js
async function getMicStream(deviceId) {
  const constraints = {
    audio: {
      deviceId:         deviceId ? { exact: deviceId } : undefined,
      channelCount:     1,
      sampleRate:       48000,        // hint only; browser may ignore
      sampleSize:       16,
      noiseSuppression: false,        // Gladia/Whisper denoise better
      echoCancellation: false,        // we are capture-only
      autoGainControl:  false,        // causes pumping artifacts
      voiceIsolation:   false,        // Safari/iOS only, very aggressive
      latency:          0.02
    },
    video: false
  };

  const stream = await navigator.mediaDevices.getUserMedia(constraints);
  return stream;
}
```

**Why each flag matters** (one-liner per flag, keep these comments in the code):

- `noiseSuppression: false` — browser NS kills high frequencies; Whisper/Gladia do this better.
- `echoCancellation: false` — no speaker playback to cancel; AEC adds subtle distortion.
- `autoGainControl: false` — causes breathing during silences and clips loud syllables.
- `voiceIsolation: false` — Safari's "studio mic mode" strips vowel formants.
- `channelCount: 1` — STT downmixes anyway; mono saves bandwidth.

---

## 2. Pin the microphone explicitly

The default device flips constantly on macOS (Bluetooth, virtual mics like BlackHole/Krisp). Never rely on it.

```js
async function listInputs() {
  await navigator.mediaDevices.getUserMedia({ audio: true }); // unlock labels
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices.filter(d => d.kind === "audioinput");
}
```

UI requirement: show a mic picker on first connect, store the chosen `deviceId` in `localStorage`, and pass it to `getMicStream(deviceId)`. If the stored ID is no longer in `enumerateDevices()`, fall back to the picker — do not silently use default.

---

## 3. Verify what the browser actually honored

`getUserMedia` constraints are *requests*. The browser can (and Chrome often does) ignore them. After acquiring the stream, log what you actually got and warn the user if it disagrees with what we asked for.

```js
function inspectTrack(track, requestedConstraints) {
  const settings     = track.getSettings();
  const capabilities = track.getCapabilities?.() ?? {};
  const applied      = track.getConstraints();

  const mismatch = {};
  for (const key of ["noiseSuppression", "echoCancellation", "autoGainControl", "channelCount"]) {
    const want = requestedConstraints.audio[key];
    const got  = settings[key];
    if (want !== undefined && want !== got) mismatch[key] = { requested: want, actual: got };
  }

  return { settings, capabilities, applied, mismatch };
}
```

If `mismatch` is non-empty, show a small toast: *"Your browser is applying audio processing we couldn't disable. Recording may be muffled."* — and log it.

---

## 4. Build the diagnostic payload

Every session must POST this object to our backend at connect time (and again if the device changes mid-session). Store it alongside the Gladia session ID so we can correlate "bad audio" reports.

```js
async function buildAudioDiagnostics(track, requestedConstraints) {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const conn    = navigator.connection || {};

  return {
    timestamp:   new Date().toISOString(),
    session_id:  currentSessionId,
    user_agent:  navigator.userAgent,
    platform:    navigator.platform,

    requested_constraints: requestedConstraints,

    audio_track_settings:     track.getSettings(),
    audio_track_capabilities: track.getCapabilities?.() ?? null,
    audio_track_applied:      track.getConstraints(),

    available_devices: devices
      .filter(d => d.kind === "audioinput")
      .map(d => ({ deviceId: d.deviceId, label: d.label, groupId: d.groupId })),

    transport: {
      protocol: "websocket",                 // or "webrtc"
      codec:    "pcm_s16le",
      sample_rate_sent_to_gladia: 16000,
      resampled_from:             track.getSettings().sampleRate
    },

    network: {
      downlink_mbps:  conn.downlink ?? null,
      rtt_ms:         conn.rtt ?? null,
      effective_type: conn.effectiveType ?? null
    }
  };
}
```

Backend: add an `audio_diagnostics` JSON column on the session table; index by `session_id`.

---

## 5. Resampler audit

If we go 48 kHz → 16 kHz on the client before sending to Gladia, the resampler **must** have a proper lowpass filter. A naive decimation (just dropping samples) causes aliasing that looks identical on a spectrogram to "bandlimited audio" — i.e. we would blame the mic when the bug is in our pipeline.

Action: confirm we use one of the following on the client:
- `OfflineAudioContext` resampling (built-in, correct), or
- `libsamplerate-js` / `wavefile` with a documented filter, or
- Server-side resampling using `librosa.resample(res_type='soxr_hq')` or `ffmpeg -af aresample=resampler=soxr`.

If we currently do `for (let i = 0; i < src.length; i += 3) dst[i/3] = src[i];` — that's the bug. Replace immediately.

---

## 6. OS-level enhancements we cannot override

Some processing happens *before* the browser and constraints can't disable it. Detect platform and show a one-time warning telling the tester to turn these off:

- **macOS:** System Settings → Microphone → disable "Voice Isolation" (Control Center → Mic Mode → Standard).
- **Windows:** Sound Settings → Microphone properties → Advanced → uncheck "Enable audio enhancements".
- **Krisp / Nvidia Broadcast / Zoom Audio:** quit these apps entirely; they intercept at OS level.
- **Bluetooth headsets (AirPods, etc.):** when used as a mic, force HFP profile = 8 kHz mono. Tell testers to use wired or laptop mic for QA.

A short doc page linked from the warning is enough — don't try to detect each tool.

---

## 7. Acceptance criteria for "this is fixed"

1. `getSettings()` after `getUserMedia` returns `noiseSuppression: false`, `echoCancellation: false`, `autoGainControl: false` — or shows a warning if not.
2. Mic picker is shown on first session; selection persists.
3. Every session writes an `audio_diagnostics` record we can query.
4. Spectrogram of a clean test recording (laptop mic, quiet room, "the quick brown fox") shows energy up to ~7 kHz, not capped at 3 kHz.
5. Resampler verified to apply a lowpass at Nyquist of the target rate.

---

## 8. Quick test procedure for the developer

After deploying the above:

```bash
# Record 10 seconds via the widget, save the raw audio Gladia received.
# Then run:
ffmpeg -i sample.wav -lavfi showspectrumpic=s=1200x400:mode=combined:legend=1 spectrum.png
ffmpeg -i sample.wav -af "highpass=f=6000,volumedetect" -vn -f null /dev/null 2>&1 | grep mean_volume
```

Pass criteria: `mean_volume` of the >6 kHz band should be roughly **-30 to -50 dB**. If it's below -60 dB, the audio is still bandlimited and we have more work to do (probably the resampler or an OS-level enhancement).

---

## 9. Open questions / decisions

- Do we want a client-side VU meter so testers can see if their mic is even producing signal before they start a session? (Recommended — 10 lines of code with `AudioContext.createAnalyser`.)
- Should we reject sessions where `mismatch` is non-empty, or just warn? (Recommend: warn for now, reject later once we know how common it is.)
- Where in the backend should `audio_diagnostics` live — alongside session metadata, or in a separate `qa_diagnostics` table? (Engineer's call.)
