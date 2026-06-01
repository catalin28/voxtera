# Voxtera Website Concierge — Architecture

> **Status:** Design locked, 2026-05-31. No code written yet.
> **What this is:** An inbound voice concierge for the Voxtera website. Prospects call a phone number; the agent explains what Voxtera does, quotes pricing, captures their name/email/phone, and books a demo on a calendar. This is a *subproject* of Voxtera — Voxtera selling Voxtera — running on the same Pipecat pipeline as the tourism bot but with its own persona, tools, and phone number.

---

## 1. Confirmed decisions

| Area | Decision | Notes |
|---|---|---|
| Telephony | **Daily** — a *purchased* number running **pinless dial-in** | Separate number from the tourism/demo Daily number. Pinless so callers never enter a PIN. |
| Pipeline | Reuse existing Pipecat stack | STT → Claude Sonnet 4.6 → Chirp 3 HD, Silero VAD, Daily transport. New persona + tools only. |
| Booking | **Cal.com** (free plan) | API key created (`CALCOM_API_KEY`). Demo = the "30 min meeting" event type. Cal.com emails the invite + writes to Google Calendar automatically. |
| Lead + call storage | **Own MySQL DB** on the Droplet, behind a thin internal API | Not a third-party CRM. The bot, a future web form, and the admin UI all write through one service. |
| Call log | Same MySQL table, populated from the **pinless dial-in webhook** | Every inbound call = one row, created before the bot answers. Better than Daily's dashboard, which is quality-debugging only. |
| SMS | **Not in v1** | Daily can't send SMS. Cal.com's email confirmation covers it. Twilio/Telnyx could be added later (note: US SMS needs A2P 10DLC registration). |

---

## 2. Components

- **Pipecat sales bot** — the existing pipeline with a new system prompt (the Voxtera sales concierge persona) and three function-call tools. Reads its credentials from `.env`.
- **Daily purchased number + pinless dial-in** — public phone line. Incoming call → Daily fires a webhook → your server creates the room and connects the bot, and logs the caller's number.
- **Leads API (aiohttp)** — thin service in front of MySQL: `POST /calls` (log inbound), `POST /leads` (capture/booking), `GET /leads` (admin read), `GET /health`. Holds all DB access in one place so the bot only carries an API token, never DB creds. *(Built with aiohttp + aiomysql to match the repo's existing HTTP-server convention in `trace_server.py`, rather than introducing FastAPI. Lives in `src/voxtera/concierge/`; run via `python -m voxtera.concierge`.)*
- **MySQL** — already running on the Droplet. One core table (see §4) that doubles as call log and lead store.
- **Cal.com** — booking backend. Configured, free plan, API key in hand.
- **Admin UI** — minimal list + detail view of leads/calls. Built later; v1 can just query the DB.

All of this lives in the Droplet's existing `docker-compose` alongside the bot.

---

## 3. End-to-end call flow

1. **Inbound call** to the purchased Daily number. Daily fires the **pinless dial-in webhook** to your server with the caller's `From` number and the `To` number dialed.
2. **Log + connect.** Your webhook handler writes a `calls` row (caller number, timestamp, status = `ringing`), creates a Daily room, and forwards the call to the bot (sales persona).
3. **Greeting** (+ optional consent line — see §7). Bot pitches and answers "what does it do / pricing / languages" from a small Voxtera knowledge file.
4. **Capture.** When the caller is interested: name → email → phone, each with a **spell-back-and-confirm loop** before it's accepted. The caller-ID `From` number is offered as the phone default ("Is the number you're calling from the best one?").
5. **Offer times.** Bot calls `check_availability` → Cal.com slots → offers two or three real open times.
6. **Book.** Caller picks one → bot calls `book_meeting` (`POST /v2/bookings`) with name, email, timezone → Cal.com creates the event on Google Calendar and emails the invite.
7. **Persist.** Bot calls `create_lead` → updates the row in MySQL via the Leads API (name, email, phone, booking time, status = `booked`).
8. **Confirm + close.** Bot reads back the confirmed time and ends the call warmly.

Hang-ups and wrong numbers still leave a `calls` row from step 2, so nothing is lost.

---

## 4. Data model (first pass)

One table that serves as both call log and lead store. Start simple; extend later.

```
leads_calls
-----------
id              BIGINT PK AUTO_INCREMENT
created_at      DATETIME            -- set at webhook (call start)
caller_number   VARCHAR(32)         -- From, via pinless webhook
dialed_number   VARCHAR(32)         -- To
status          VARCHAR(24)         -- ringing | answered | captured | booked | abandoned
name            VARCHAR(120)        -- captured + confirmed
email           VARCHAR(160)        -- captured + spell-back confirmed
phone           VARCHAR(32)         -- confirmed (defaults to caller_number)
timezone        VARCHAR(64)         -- needed for Cal.com booking
booking_time    DATETIME NULL       -- the booked slot
calcom_booking_id VARCHAR(64) NULL  -- returned by Cal.com
notes           TEXT NULL           -- free notes / transcript summary
```

Dedup (same person calls twice) and a follow-up `status` field can come later — resist building CRM features until the pain is real.

---

## 5. Bot tools (Claude function calling)

Three functions the bot can call mid-conversation:

- **`check_availability(date_range)`** → hits Cal.com slots endpoint, returns open times so the agent can offer real availability.
- **`book_meeting(name, email, timezone, slot)`** → `POST /v2/bookings` against the demo event type. Email is mandatory — Cal.com sends the invite there, which is why capture-and-confirm happens first.
- **`create_lead(...)`** → POSTs the captured fields + booking result to the Leads API (MySQL).

The webhook-side call logging (`POST /calls`) is not a bot tool — it happens in the dial-in webhook handler before the bot is in the loop.

---

## 6. Environment variables

```
# Cal.com
CALCOM_API_KEY=cal_live_...
CALCOM_EVENT_TYPE_ID=...        # optional; bot can look it up via the API

# Leads API (internal)
LEADS_API_URL=http://leads-api:8000
LEADS_API_TOKEN=...             # bot → leads service auth, NOT raw DB creds

# MySQL (used by the Leads API service only, not the bot)
MYSQL_HOST=...
MYSQL_DB=...
MYSQL_USER=...
MYSQL_PASSWORD=...

# Daily (purchased number / pinless dial-in) — reuses existing Daily creds
DAILY_API_KEY=...
DAILY_PINLESS_WEBHOOK_SECRET=...   # verify webhook signature (cf. PSTN HMAC pattern)
WEBSITE_CONCIERGE_PHONE_NUMBER=+12363124419   # purchased Daily number (Vancouver, BC 236). E.164. Validate webhook `To` against this.
```

---

## 7. Open decisions to confirm

1. **Language scope.** Recommendation for v1: **English-first** — more reliable email/phone capture and simpler, since this line targets your hotel-pilot prospects. The 99-language auto-detect remains available if you'd rather keep parity with the tourism bot.
2. **Recording / consent line.** Recommendation: include a short "this call may be recorded" notice at the top of the greeting, since you're collecting personal contact data by voice. Final wording depends on where most callers are based.

---

## 8. Suggested build order (no code yet)

1. **Leads API + MySQL table** — smallest standalone piece; testable on its own.
2. **Cal.com tools** — `check_availability` + `book_meeting`, tested against the real free account.
3. **Sales persona + knowledge file** — `voxtera_sales_persona.md` (pitch, pricing, FAQ, objection handling) + the spell-back capture flow.
4. **Daily purchased number + pinless dial-in webhook** — provision the number, wire the webhook to log calls and connect the bot.
5. **Admin UI** — list + detail over the leads table.
