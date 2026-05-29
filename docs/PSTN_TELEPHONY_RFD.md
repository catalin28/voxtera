# RFD: PSTN Telephony — Inbound Calls to Voxtera

**Status:** Draft  
**Branch:** `feat/pstn-telephony`  
**Date:** 2026-05-29  
**Author:** Dan + Copilot  

---

## 1. Summary

Enable guests to call a real phone number (+1 226 212-0379, Ontario Canada) and have a full voice conversation with the Voxtera AI concierge. The system uses Daily's PSTN dial-in infrastructure to bridge phone calls into Daily rooms where the Pipecat bot handles the conversation identically to the web widget.

---

## 2. Goals

- **v1 scope:** Inbound calls only (guest → bot)
- Single hotel per number (demo hotel)
- Support both **pinless** (direct answer) and **PIN-based** routing (configurable)
- Concurrent calls — each caller gets their own bot instance
- 24/7 availability
- Max call duration: **4 minutes** (cost control)
- Auto-detect language from caller's first words (same as web widget)
- Outbound (bot → guest) deferred to v2

---

## 3. Architecture

```
┌─────────────┐         ┌──────────────┐         ┌───────────────────┐
│  Guest      │  PSTN   │  Daily PSTN  │  WebRTC │  Droplet          │
│  Phone      │────────▶│  Gateway     │────────▶│  (serve.py)       │
│             │         │              │         │                   │
│ dials +1    │         │ bridges call │         │ ┌───────────────┐ │
│ 226-212-0379│         │ into a Daily │  webhook│ │ /pstn/webhook │ │
│             │         │ room         │◀────────│ │ spawns bot    │ │
└─────────────┘         └──────────────┘         │ └───────┬───────┘ │
                                                 │         │         │
                                                 │         ▼         │
                                                 │ ┌───────────────┐ │
                                                 │ │ Pipecat Bot   │ │
                                                 │ │ (same pipeline│ │
                                                 │ │  as web)      │ │
                                                 │ └───────────────┘ │
                                                 └───────────────────┘
```

### Flow

1. Guest dials **+1 (226) 212-0379**
2. Daily's PSTN gateway answers and creates a new Daily room (or uses a pre-configured room)
3. Daily sends a **webhook** to our server: `POST /pstn/webhook`
4. Server spawns a Pipecat bot into that room
5. Bot greets the caller, auto-detects language, converses
6. After 4 min (or caller hangs up), call ends and room is cleaned up

---

## 4. Dial-in Mode: Pinless vs PIN

| Mode | How it works | When to use |
|------|-------------|-------------|
| **Pinless** | Caller connects directly to a new room → bot answers | Default for v1. Simple guest experience. |
| **PIN** | Caller hears "Enter your room code" → enters digits → routed to a specific room | Useful if hotel wants per-room or per-guest routing |

Both will be implemented and switchable via config (`PSTN_MODE=pinless` or `PSTN_MODE=pin`).

---

## 5. Server Architecture Decision

### Option A: Same server (add routes to serve.py)

| Pros | Cons |
|------|------|
| Simpler deployment — one process | If serve.py crashes, phone calls also down |
| Shares existing config, Daily client, bot spawner | Adds complexity to serve.py |
| Single port to expose (7860) | |
| Natural fit — serve.py already manages Daily rooms | |

### Option B: Separate process

| Pros | Cons |
|------|------|
| Isolated — phone service independent of web demo | Two processes to manage |
| Can scale independently | Another port/process to monitor |
| Cleaner separation of concerns | Duplicated config loading |

### Recommendation: **Option A** (same server)

Serve.py already handles Daily room creation and bot spawning. Adding a `/pstn/webhook` route is minimal additional surface. If it grows complex, we extract later. The webhook handler is ~50 lines — not worth a separate service yet.

---

## 6. Components to Build

| # | Component | Description |
|---|-----------|-------------|
| 1 | **PSTN config** | New env vars: `PSTN_ENABLED`, `PSTN_MODE`, `PSTN_PHONE_ID`, `PSTN_MAX_DURATION` |
| 2 | **Webhook handler** | `POST /pstn/webhook` in serve.py — receives Daily's call event, spawns bot |
| 3 | **Pinless dial-in setup** | Script/API call to configure the purchased number for pinless routing |
| 4 | **Call duration enforcer** | Timer that gracefully ends the call after 4 minutes with a warning |
| 5 | **Phone call logging** | Log PSTN calls to `logs/calls/` with caller number, duration, transcript |
| 6 | **Pinless/PIN config script** | `scripts/phones/configure_dialin.py` to toggle between modes |

---

## 7. Environment Variables

```env
# --- PSTN Telephony ---
PSTN_ENABLED=true
# pinless = bot answers immediately; pin = caller enters code
PSTN_MODE=pinless
# UUID of the purchased phone number (from Daily)
PSTN_PHONE_ID=29bfe005-3ce7-4caa-b2c6-fcbdbe1764f9
# Phone number (for logging/display)
PSTN_PHONE_NUMBER=+12262120379
# Max call duration in minutes (cost control). 0 = unlimited.
PSTN_MAX_DURATION_MIN=4
```

---

## 8. Webhook Payload (from Daily)

When a PSTN caller connects, Daily sends a webhook like:

```json
{
  "event": "dialin.connected",
  "callId": "abc-123",
  "roomName": "auto-generated-room",
  "from": "+14165551234",
  "to": "+12262120379"
}
```

Our handler:
1. Validates the webhook (auth header)
2. Creates a new Daily room with prefix **`VCI-`** (Voxtera Call In), e.g. `VCI-20260529-143022-a7b3`
3. Logs the incoming call
4. Spawns a Pipecat bot into the VCI room
5. Bot joins → conversation begins

### Room Naming Convention

```
VCI-{YYYYMMDD}-{HHMMSS}-{4-char-random}
```

Example: `VCI-20260529-143022-a7b3`

This prefix makes it easy to:
- Filter PSTN call rooms in the Daily dashboard
- Distinguish them from web widget rooms in logs
- Query call history via the Daily API (`GET /rooms?prefix=VCI-`)

---

## 9. Call Duration Enforcement

- At **3:30** (30 sec before limit): Bot says "Just to let you know, we have about 30 seconds left. Is there anything else I can help with?"
- At **4:00**: Bot says "Thank you for calling. Goodbye!" → disconnects
- Configurable via `PSTN_MAX_DURATION_MIN`

---

## 10. Cost Estimate

| Volume | Monthly cost |
|--------|-------------|
| 10 calls/day × 3 min avg | ~$18/month |
| 50 calls/day × 3 min avg | ~$90/month |
| 100 calls/day × 3 min avg | ~$180/month |

(At $0.02/min PSTN + audio participant minutes)

---

## 11. Security

- Webhook endpoint validates Daily's auth signature
- No PII stored beyond call logs (caller number masked after 30 days)
- Rate limiting: max 10 concurrent calls (configurable)
- Reject calls from blocked numbers (future)

---

## 12. Implementation Order

1. Configure pinless dial-in on the purchased number (API call)
2. Add webhook route to serve.py
3. Spawn bot on incoming call
4. Add duration enforcer
5. Add PSTN-specific logging
6. Test end-to-end with real phone call
7. Add PIN mode as alternative

---

## 13. Open Questions

- [ ] Does Daily send a webhook for pinless dial-in, or do we need to use their "dialin-ready" event via the client SDK?
- [ ] Should we use Daily's built-in meeting token for PSTN auth, or is the webhook sufficient?
- [ ] Do we want a warm greeting before the bot is ready (hold music / "connecting you...")?

---

## 14. Development Testing (No Phone Required)

During development, simulate calls from your PC without touching a real phone:

| Phase | Method | What it tests |
|-------|--------|---------------|
| Building webhook logic | **curl / Invoke-RestMethod** — POST to `/pstn/webhook` with a fake payload | Server routing, room creation, bot spawn |
| Testing voice interaction | **Browser** — open the VCI room URL and use your mic | Full STT → LLM → TTS pipeline |
| Phone-realistic audio | **SIP softphone** (Linphone / MicroSIP) → Daily SIP URI | Narrowband 8 kHz codec, DTMF |
| Final validation | Real phone call | End-to-end PSTN path |

### 14.1 Simulated webhook (server logic only)
```powershell
Invoke-RestMethod -Method POST -Uri "http://localhost:7860/pstn/webhook" `
  -ContentType "application/json" `
  -Body '{"type":"dialin.connected","callId":"test-123","from":"+15551234567"}'
```

### 14.2 SIP softphone → Daily SIP URI (free, full audio)
Install [Linphone](https://www.linphone.org/getting-started) or [MicroSIP](https://www.microsip.org/) and dial:
```
sip:VCI-YYYYMMDD-HHMMSS-xxxx@sip.daily.co
```
Bypasses PSTN — no per-minute charges — but exercises the full audio pipeline.

### 14.3 Browser join (easiest day-to-day)
After the webhook creates the room, open:
```
https://voxtera.daily.co/VCI-20260529-143022-a7b3
```
with a microphone. Behaves identically to a phone caller from the bot's perspective.

---

## 15. International Calling Notes

- **Inbound from overseas:** Anyone worldwide can dial +1 226 212-0379. The caller pays their own carrier's international rate; Daily charges us the same $0.018–0.03/min regardless of caller origin.
- **Outbound to international numbers (e.g., Turkey):** Daily PSTN dial-out only supports US/Canada (+1). For international dial-out, a SIP bridge (Twilio or Telnyx) is required.
- Estimated international outbound rates via Twilio SIP bridge:

| Destination | Landline | Mobile |
|-------------|----------|--------|
| Turkey | ~$0.04–0.06/min | ~$0.10–0.18/min |
| UK | ~$0.02/min | ~$0.04/min |
| Germany | ~$0.02/min | ~$0.05/min |

- **Caller-side alternatives:** Guests overseas can use Skype-to-Phone, Google Voice, or Viber Out to call the +1 number cheaply. WhatsApp **cannot** dial PSTN numbers.

---

## 16. Future (v2)

- Outbound calls (bot calls guest for notifications)
- Multiple numbers per hotel / region
- IVR menu for multi-hotel setups
- Call recording / transcription storage
- WhatsApp Business API integration alongside PSTN
- Twilio SIP bridge for international dial-out
- Analytics dashboard (call volume, avg duration, satisfaction)
