# WhatsApp Calls → Voxtera voice — design note

**Date:** 2026-06-08
**Goal:** A WhatsApp user taps "call" on +1 236 501-6594 → Voxtera's travel-agent voice answers.

## What we confirmed

- Calling is **enabled** on the number (`calling.status: ENABLED`), business **verified**, messaging limit **TIER_2K**. The number can ring; nothing answers yet.
- WhatsApp Calling uses a **raw WebRTC** signaling flow (NOT Daily dial-in):
  1. `calls` webhook with `event:"connect"` + an **SDP offer** + `call_id`
  2. business replies `POST /{PN}/calls action=pre_accept` with an **SDP answer**
  3. `action=accept` once the WebRTC connection is up
  4. `action=terminate` to end
- **Pipecat 1.0.0 ships a native WhatsApp transport** in the installed venv:
  `pipecat.transports.whatsapp` (`WhatsAppClient`) + `pipecat.transports.smallwebrtc`
  (`SmallWebRTCConnection`, `SmallWebRTCTransport`). It does the hard parts:
  creates the WebRTC connection, generates + SHA-256-filters the SDP answer, and
  calls pre_accept/accept on the WhatsApp Calls API for us.

## Integration surface (from the installed package)

`WhatsAppClient(whatsapp_token, phone_number_id, whatsapp_secret)`:
- `handle_verify_webhook_request(params, expected_verification_token)` → GET challenge
- `handle_webhook_request(request, raw_body, sha256_signature, connection_callback)`
  → validates signature, handles connect/terminate, and calls
  `connection_callback(connection: SmallWebRTCConnection)` when a call connects.

In the callback we build the bot pipeline over that connection:
```python
transport = SmallWebRTCTransport(
    webrtc_connection=connection,
    params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
)
# STT → TravelAgentBrain (ConciergePipeline) → TTS, same brain as text + web voice
```

## The architectural decision

The call bot's WebRTC connection is created **inside the webhook process** and must be
used by the Pipecat pipeline **in the same async process** — a live peer connection
can't be handed to the `python -m voxtera.bot` subprocess (the Daily model) nor to the
**sync** `demo-hotel/serve.py` (http.server, no event loop for WebRTC).

Meta sends **both** text messages and call events to the **same** callback URL
(`messages` + `calls` fields, one subscription). So calls force an **async** webhook
service, and it's cleanest for that one service to handle **both** text and calls.

**Decision:** consolidate the WhatsApp webhook into one **async service**
(`voxtera.whatsapp` aiohttp app, run as a systemd service on the droplet):
- `GET/POST /whatsapp/webhook` — text (existing concierge) **and** calls (Pipecat WhatsApp)
- Repoint Meta callback to this service
- This **supersedes** the text handler we added to `serve.py` (that was a stopgap)

Net: still **one** WhatsApp process — just async instead of riding inside serve.py.

## Build steps

1. Add a `transport`-injection path so a pipeline can run over a provided
   `SmallWebRTCTransport` with the `travel_agent` brain (extend `build_pipeline`
   or a thin dedicated runner reusing STT/TravelAgentBrain/TTS).
2. Build the async WhatsApp service: text branch (existing `extract_text_messages`
   → ConciergePipeline → reply) + calls branch (`WhatsAppClient.handle_webhook_request`
   with a `connection_callback` that runs the voice pipeline).
3. Env: Pipecat expects `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `WHATSAPP_APP_SECRET`, `WHATSAPP_WEBHOOK_VERIFICATION_TOKEN`. Map from our existing
   `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_WEBHOOK_VERIFY_TOKEN` (alias or rename).
4. systemd unit + reverse-proxy route on the droplet; repoint Meta callback.
5. Test: real WhatsApp call (WebRTC needs the live droplet with public networking;
   not fully testable from the sandbox).

## Constraints / risks

- Real call testing requires the deployed droplet (ICE/STUN, public IP). Can't fully
  exercise WebRTC media from the dev sandbox — only compile/import checks there.
- The call bot runs in-process; concurrency = how many simultaneous calls the box
  handles (each call = one Pipecat pipeline + STT/TTS streams).
- Region: WHATSAPP_DEFAULT_REGION empty → concierge asks region on first turn (spoken).

## What was built (2026-06-08)

- `src/voxtera/whatsapp/call_bot.py` — `run_call_bot(connection)`: self-contained
  Pipecat pipeline over `SmallWebRTCTransport`, reusing `_build_stt`, `_TTS_BUILDERS`,
  Silero VAD, LocalSmartTurnAnalyzerV3, and the shared `TravelAgentBrain`. Does NOT
  touch `build_pipeline` (keeps the hotel/Daily path safe).
- `src/voxtera/whatsapp/webhook.py` — the async aiohttp service now handles BOTH:
  text (existing concierge) and calls. `is_calls_event()` routes `field=="calls"`
  payloads to Pipecat's `WhatsAppClient.handle_webhook_request(connection_callback=run_call_bot)`.
  We validate the X-Hub signature ourselves, so the Pipecat client is built without a
  secret (skips its re-validation).
- `scripts/voxtera-whatsapp.service` — systemd unit running `python -m voxtera.whatsapp`.

Verified in dev: ruff clean, py_compile OK, all referenced `Settings` fields exist,
import paths sourced from the installed pipecat 1.0.0 + the known-good pipeline.py.
NOT runtime-tested (pipecat's macOS venv can't run in the Linux sandbox; WebRTC needs
the live droplet).

## Deploy (Option A — one async WhatsApp service)

1. Put `WHATSAPP_*` + voice env (STT_PROVIDER, TTS_PROVIDER, GLADIA/DEEPGRAM/CARTESIA/
   ELEVENLABS keys, ANTHROPIC/OPENAI) in `/etc/voxtera/voxtera.env` (the deploy script
   already scp's the local `.env`, which has them).
2. Install + start the service on the droplet:
   ```
   cp scripts/voxtera-whatsapp.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now voxtera-whatsapp
   ```
3. Reverse proxy: route `voxtera.io/whatsapp/*` → `localhost:8200` (the async service)
   instead of serve.py (8080). With Caddy, add a matcher for `/whatsapp/*` →
   `reverse_proxy localhost:8200`. Meta callback URL stays `https://voxtera.io/whatsapp/webhook`.
4. This **supersedes** the text handler in `demo-hotel/serve.py` (now dead code; can be
   removed later — harmless once the proxy routes /whatsapp/* to :8200).
5. Ensure the droplet allows **UDP** (WebRTC media) on its public IP.
6. Test: WhatsApp-call +1 236 501-6594 → Voxtera travel agent answers.

## Concurrency
Each call = one in-process Pipecat pipeline + STT/TTS streams. Capacity is bounded by
the droplet's CPU (Silero VAD + SmartTurn ONNX + STT/TTS per call).
