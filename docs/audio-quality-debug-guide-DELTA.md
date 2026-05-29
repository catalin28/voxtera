# Audio Quality Debug Guide — Modifications

Read alongside `audio-quality-debug-guide.md`. Only the changes below; the rest of the guide stands.

---

## CHANGE 1 — Section 5 "Resampler audit": **DELETE the entire section**

Reason: we are moving resampling off the client. The client should not resample at all.

Replace with the new section below.

---

## NEW Section 5 — Server-side audio normalization (replaces "Resampler audit")

The client sends the audio in whatever native rate the device provides. All resampling, decoding (µ-law from Twilio), and rate-matching to Gladia happens server-side in the Pipecat pipeline.

**Why server-side:**
- Same pipeline handles web widget, mobile SDK, and Twilio phone — clients differ, server does not.
- One known-good resampler (`soxr_hq`) instead of N browser/JS implementations.
- Easy to dump raw incoming audio for debugging.
- Vendor swap (Gladia → Deepgram → local Whisper) becomes a server config change, not a client redeploy.

**Implementation:**

- In Pipecat, add an `AudioResampler` (or equivalent) stage after the transport decoder and before the STT service.
- Target output: **16 kHz, 16-bit, mono PCM** — Gladia's standard (`encoding: "wav/pcm"`, `sample_rate: 16000`, `bit_depth: 16`, `channels: 1`).
- Use `soxr_hq` quality. If using ffmpeg directly: `-af aresample=resampler=soxr:precision=28`. If using `librosa`: `res_type="soxr_hq"`.
- Never decimate by sample-dropping. The resampler must apply a proper lowpass before downsampling, otherwise we get aliasing that looks identical to a bandlimited mic on the spectrogram.

**What the client now sends:**

- Browser/widget: native rate from `getUserMedia` (usually 48 kHz, 16-bit, mono PCM) — do not resample on the client.
- Preferred for production: Opus over WebRTC via Daily.co (smaller payload, wideband). Pipecat decodes Opus server-side.
- Twilio: 8 kHz µ-law, raw — Pipecat decodes µ-law server-side.

**Bandwidth note:**
Raw 48 kHz PCM ≈ 768 kbps per call. Fine for demo traffic on the droplet (~2 MB/s extra inbound at 20 concurrent calls). For production, prefer Opus.

---

## CHANGE 2 — Section 1 "Fix getUserMedia constraints": **add Bluetooth note at the end**

Append this paragraph:

> **About Bluetooth / AirPods users:** these constraints disable browser-level DSP, but they cannot disable the noise suppression / beamforming that runs *inside* the AirPods firmware, nor the Bluetooth HFP narrowband cap (8–16 kHz). Bluetooth audio will always arrive lower-bandwidth than wired. That's reality — we adapt downstream, not by rejecting these users. See Section 10.

---

## CHANGE 3 — Section 4 "Build the diagnostic payload": **add isBluetooth detection**

Inside `buildAudioDiagnostics`, add to the returned object:

```js
const label = track.getSettings().label || track.label || "";
const isBluetooth = /airpods|bluetooth|bt|hfp|hands.?free|wireless/i.test(label);

return {
  // ... existing fields ...
  device_kind: isBluetooth ? "bluetooth" : "wired_or_builtin",
};
```

Backend: bucket session-level metrics (WER, latency, drop rate) by `device_kind`. We need to know how much accuracy gap exists between Bluetooth and wired before we can decide what to do about it.

---

## CHANGE 4 — Section 6 "OS-level enhancements": **soften the framing**

Replace the section header sentence with:

> Some processing happens *before* the browser and constraints can't disable it. We cannot make users change OS settings, but if **internal testers** report bad audio, these are the first things to check on their machine.

Keep the bullet list as-is. The point is that this advice is for QA, not for end users.

---

## CHANGE 5 — Section 7 "Acceptance criteria": **replace item 5**

Old:
> 5. Resampler verified to apply a lowpass at Nyquist of the target rate.

New:
> 5. Pipecat server-side resampler verified: feed a 48 kHz sine sweep into the pipeline, confirm the 16 kHz output has no aliased frequencies above 8 kHz Nyquist.

---

## CHANGE 6 — Section 8 "Quick test procedure": **keep, but note where to capture audio**

Add a line at the top:

> Capture the raw audio **after server-side resampling** (i.e., what Pipecat actually forwards to Gladia), not what leaves the browser. That's what matters.

---

## NEW Section 10 — Bluetooth / AirPods reality and adaptive pipeline

Bluetooth headsets (AirPods, Jabra, Bose, etc.) are used by most of our real users. We cannot tell them to switch hardware. Instead, the pipeline detects them and adapts.

**Detection (client → server):**

The `device_kind: "bluetooth"` flag from Section 4 is propagated to Pipecat at session start.

**Pipeline adaptations when `device_kind == "bluetooth"`:**

1. **Spectral pre-emphasis** before STT. Apply a high-shelf boost of +4–6 dB above 2 kHz to compensate for AirPods' tendency to clamp high frequencies during quieter passages. Pipecat filter or one-line ffmpeg `highshelf=f=2000:g=4`.
2. **Gladia config:** keep `sample_rate: 16000`. Consider enabling Gladia's `audio_enhancer` if available on our plan — confirm with Gladia docs / support.
3. **Lower partial-transcript confidence threshold** if we surface partials in the UI.
4. **Stronger Claude post-correction.** Add a system-prompt hint to the LLM stage: "Transcript may have errors from narrowband Bluetooth input. Common confusions: …". Use tourism vocabulary as priors.

**Observability:**

Add a Grafana / loguru dashboard panel that splits these metrics by `device_kind`:
- WER (sample 50 sessions/week with manual transcripts)
- Average final-transcript latency
- Mid-session quality drops (the "1:14 muffling" event we saw — detect via rolling high-band energy)

Target: close the WER gap between Bluetooth and wired to <2 percentage points.

**Do not do:**

- Reject Bluetooth devices.
- Show users a "switch device" prompt as a hard block.
- A soft one-time tip ("for best results, find a quiet spot — Bluetooth mics work harder in noise") is fine, shown once per device.

---

## CHANGE 7 — Section 9 "Open questions": **add two items**

Append:

- Do we want Pipecat to dump the first 10 seconds of every session's raw + resampled audio to disk for debugging (auto-deleted after 24h)? Useful for triaging "bad audio" reports without re-running the session.
- Should Bluetooth-detected sessions go through a separate Pipecat pipeline variant (with pre-emphasis + stricter Claude correction), or a single pipeline with conditional stages? My recommendation: single pipeline, conditional. Engineer's call.
