# Voxtera — Audio Quality & Multi-Region Plan

Two independent workstreams came out of the Turkey-partner debugging. Keep them separate:

- **Track A — Echo / duplex fix** (branch `fix/VOX-voice-chat-echo`): stop the bot's own voice from
  reaching STT. Mostly done.
- **Track B — Multi-region rollout** (Toronto + Frankfurt): put the bot near the guest so the
  real-time audio path stays short. Planning.

---

## Track A — Echo / duplex fix

### What actually happened (corrected root cause)

Earlier theories (pure acoustic echo, "digital loopback") were incomplete. Trace + recording
analysis showed the real causes, in order of impact:

1. **Chat + voice running at the same time.** The page (`voxtera-demo.html`) has two independent
   response paths: the voice call (Daily/WebRTC) and the text chat (`/api/chat`, which returns and
   plays its **own** TTS audio). When both are active, two bot voices play through the speakers. The
   voice pipeline's echo guard only knows when the *voice* bot speaks — it is **blind to the
   `/api/chat` TTS** — so the chat reply's audio goes into the open mic and straight to Gladia at
   full quality. This is what dominated the worst recording (`ceebf14b`).
2. **`echoCancellation: false` + speakers.** With AEC disabled in the mic constraints
   (`voxtera-demo.html`, ~line 2208) and a guest on speakers, the voice bot's own TTS is captured
   by the mic acoustically. The server-side `PlaybackLeakageGuard` only *ducks* (and was
   deliberately detuned to avoid clipping user speech), so it's threshold-fragile: quiet echo is
   suppressed, loud echo leaks through. Same code → clean in one session, chaotic in another.
3. **`allow_interruptions=true`** (set in `.env`, overriding the `false` default). The user *wants*
   barge-in, so we **cannot** hard-mute STT during bot speech — that would kill interruption. The
   fix must remove the echo at the source (AEC) rather than gating the mic.

### Changes

- **[DONE]** Pause the text chat while a voice session is active (`voxtera-demo.html`): disable
  input/send/mic + show a notice; block `sendMessage`/form submit when `inCall`; clear everything in
  `_endVoiceCall` (covers both the End button and the time-limit/server disconnect). Removes the
  two-bot-voices scenario.
- **[TODO]** Set `echoCancellation: true` (keep `noiseSuppression`/`autoGainControl` as-is) in
  `voxtera-demo.html`. Removes the bot's voice from the mic at the source, regardless of speaker
  volume, **without** disabling barge-in. This is the reliable fix for #2.
- **[OPTIONAL]** Once AEC is on, relax `PlaybackLeakageGuard` thresholds so genuine barge-in opens
  more easily (it was detuned only because AEC was off).

---

## Track B — Multi-region rollout (Toronto + Frankfurt)

### Why

Only the **bot** needs to be regional, not `voxtera.io`. The control plane (page load + the one
`/api` start call) is a tiny one-time hop; the **voice media** (browser ⇄ Daily SFU ⇄ bot) is the
latency-sensitive path. A Toronto-only bot forces a Turkey guest's audio through North America and
back — the cause of the cuts/jitter. A Frankfurt droplet keeps EU/Turkey audio in-region.

### Routing the call path

| Leg | Toronto-only (today) | With Frankfurt (fixed, for EU guest) |
|-----|----------------------|--------------------------------------|
| Voice media | Turkey ⇄ NA SFU ⇄ Toronto bot (long, jittery) | Turkey ⇄ EU SFU ⇄ Frankfurt bot (~40–60 ms) |
| STT (Gladia) | Toronto → `eu-west` (transatlantic — **misconfig**) | Frankfurt → `eu-west` (local) |
| LLM (Claude) | US | US (per-turn, not streaming — low priority) |
| TTS (ElevenLabs) | US | US (per-turn — low priority) |

### Per-region config (important — fixes a current misconfig)

`gladia_region` defaults to `eu-west` in `config.py`, so **today even the Toronto bot hits
`eu-west`** — a needless transatlantic STT hop. Set it per droplet:

- **Toronto** → `GLADIA_REGION=us-west` (Gladia's only NA region is West US; ~60–80 ms from
  Toronto — better than `eu-west`, but not truly local; there is no `us-east`).
- **Frankfurt** → `GLADIA_REGION=eu-west` (local).

### Daily rooms

Rooms are already dynamic per session (`create_room` in `daily_client.py`, no `geo` pinned), and the
SFU anchors to the **first** participant — the bot. So a Frankfurt-spawned bot auto-anchors the SFU
in the EU; no stale-region risk. Optional hardening: set `properties.geo` explicitly per session
from the guest GeoIP you already collect in `serve.py` (`ip-api.com` lookup).

### How to route guests to the right region (pick one, simplest first)

1. **Separate subdomains** — `voxtera.io` = Toronto, `eu.voxtera.io` = Frankfurt; point each hotel
   at the nearer one. Zero routing infra.
2. **Central dispatch** — keep DNS → Toronto; on `/start`, GeoIP the guest (already available) and
   have Toronto ask the Frankfurt droplet to spawn the bot for EU guests. Needs a small inter-region
   launch endpoint.
3. **GeoDNS** — `voxtera.io` resolves to the nearest region (Route 53 latency routing / Cloudflare).
   Cleanest, most infra; each droplet runs the full stack.

### Related: network telemetry (separate branch `feat/VOX-network-telemetry`)

No real packet-loss logging exists today (only coarse `navigator.connection`). Add Daily
`getNetworkStats()` sampling + `network-quality-change` events (packet loss %, jitter, RTT) written
into the per-session call record (`logs/calls/<session_id>/`). The arrived audio is already captured
(`input_raw.wav` — dropouts show as exact-zero gaps), so stats + that audio = enough to diagnose;
no need to save raw RTP packets. This signal can later drive a per-session adaptive `VAD_STOP_SECS`.

---

## Suggested order

1. **[DONE]** Chat-lock during voice (Track A).
2. `echoCancellation: true` (Track A) — makes the echo fix reliable on speakers without breaking barge-in.
3. Stand up Frankfurt; set `GLADIA_REGION` per droplet (`us-west` Toronto / `eu-west` Frankfurt).
4. Pick a routing approach (start with separate subdomains).
5. Network telemetry branch — validates the migration and feeds adaptive VAD.
