# Voxtera Website Concierge — Product Requirements Document

> **Status:** Draft v1, 2026-05-31
> **Owner:** Dan Dinu
> **Branch:** `feat/website-concierge`
> **Related:** [architecture.md](./architecture.md)
> **One-liner:** A phone line on the Voxtera website where prospects call in, hear what Voxtera does and what it costs, and end the call with a demo booked and their contact details captured.

---

## 1. Problem statement

Voxtera's go-to-market motion (the Founding Hotels pilot) relies on reaching hotel decision-makers and getting them into a demo. Today there is no instant, always-available way for an interested prospect to engage the moment their interest is highest — they have to wait for an email reply or a scheduled call, and interest cools. A simple "call us" web form or inbox introduces hours or days of latency and manual follow-up, and many prospects never convert because the friction outlasts their curiosity.

A voice concierge closes that gap: a prospect who sees the website can call a number, get their questions answered in natural conversation, and book a demo on the spot — 24/7, with no human on the Voxtera side required. It also doubles as a live proof point: the prospect experiences Voxtera's own product while being sold on it.

This is also a low-risk, high-signal way for Voxtera to dogfood its actions/booking stack (Cal.com tool calls, lead capture) on a controlled, owned use case before shipping the same capabilities to paying hotel clients.

---

## 2. Goals

This line exists to maximize **two co-primary outcomes equally**: demos booked and clean leads captured. Every call should ideally end in both.

1. **Book demos.** A prospect can complete a real Cal.com booking entirely by voice, with no human intervention. Target: ≥ 40% of *qualified* inbound calls (caller engages past the pitch) end with a booking.
2. **Capture clean leads.** Every engaged caller's name, email, and phone are captured and verified accurately. Target: ≥ 95% of captured emails are valid/deliverable (the hardest field).
3. **Zero-latency engagement.** The prospect is talking to the concierge within seconds of dialing, any time of day, with no queue or callback.
4. **Own the data.** Every inbound call — including hang-ups and wrong numbers — produces a record in Voxtera's own MySQL store, queryable without a third-party CRM.
5. **Prove the product.** The call itself is a credible live demo of Voxtera's voice quality and conversational ability.

User goals: get questions answered fast, book a time without back-and-forth.
Business goals: more booked demos per pilot-outreach dollar, a clean owned lead/call log, and a reusable actions stack.

---

## 3. Non-goals (v1)

1. **Not multilingual.** v1 is **English-only**. Multilingual capture of emails/phone numbers by voice is materially riskier; the tourism bot's 99-language path stays out of this line for now. *(Why: reliability of data capture > reach, and current prospects are Vancouver/English.)*
2. **No SMS.** No text confirmations or reminders. Cal.com's email confirmation covers it. *(Why: Daily can't send SMS; adding Twilio + A2P 10DLC registration is a separate project.)*
3. **No outbound calling.** The line only receives calls; it does not dial prospects. *(Why: different compliance and design surface.)*
4. **No CRM integration.** No HubSpot/Zoho/Salesforce sync. Leads live in Voxtera's MySQL. *(Why: pilot volume doesn't justify it; revisit if a sales team needs pipeline tooling.)*
5. **No payment / contracting on the call.** The bot books a demo; it does not quote bespoke pricing, negotiate, or take payment. *(Why: those need a human; the demo is the handoff point.)*
6. **No live human transfer.** No "press 0 for a person" in v1. *(Why: no staffed line yet; avoids transfer per-minute charges. P2 candidate.)*

---

## 4. User stories

Ordered by priority.

1. As a **hotel decision-maker who just saw the Voxtera site**, I want to call a number and ask what it does and what it costs, so that I can decide if it's worth my time — without waiting for an email.
2. As an **interested prospect**, I want to book a demo during the same call, so that I don't have to coordinate over email later.
3. As an **interested prospect**, I want the agent to read my email and phone back to me and confirm them, so that the demo invite actually reaches me.
4. As **Dan (Voxtera)**, I want every inbound call logged with the caller's number and outcome, so that I can follow up on people who called but didn't book.
5. As **Dan (Voxtera)**, I want captured leads and bookings in one place I own, so that I can work them without a CRM subscription.
6. As a **prospect calling from the number I want to be reached on**, I want the agent to offer to use my caller-ID number, so that I don't have to recite it digit by digit.
7. As a **caller who is just browsing / not interested**, I want to end the call politely without being pushed, so that the experience reflects well on the brand. *(edge case)*
8. As a **caller whose email the agent mishears**, I want a correction loop, so that a wrong address doesn't get saved. *(edge/error case)*
9. As a **caller**, I want to know up front the call may be recorded, so that I'm informed before I share contact details. *(compliance)*

---

## 5. Requirements

### P0 — Must-have (v1 cannot ship without these)

- **R0. Prompt management (table-based) — MANDATORY.** All prompts — system persona, greeting, consent line, pitch, pricing, capture, booking, closing, and error/fallback lines — **MUST** live in a single prompts **table**. Prompts must **never** be embedded inline in code or buried in prose. The table **MUST** include a **Group** column that categorizes each prompt (e.g., `greeting`, `pitch`, `pricing`, `capture`, `booking`, `closing`, `error`). The bot loads its prompts from this table. This aligns with the existing `docs/prompts-catalog.md` convention. Seed catalog in §9.
  - Given any new prompt is added, then it is added as a row in the prompts table with a Group value — not pasted into source code or a paragraph.
- **R1. Inbound call answered automatically.** A call to `WEBSITE_CONCIERGE_PHONE_NUMBER` (+12363124419) is answered by the sales-persona bot within a few seconds via Daily pinless dial-in.
  - Given a caller dials the number, when the call connects, then the bot greets them within ~3 seconds, in English.
- **R2. Consent notice.** The greeting includes a brief "this call may be recorded for quality" line before any data is collected.
- **R3. Pitch + pricing Q&A.** The bot can answer "what does Voxtera do", "what does it cost", "what languages", "how long to set up" from a maintained knowledge file.
  - Given a caller asks any of the covered FAQs, then the bot answers concisely and accurately from the knowledge file (no hallucinated pricing).
- **R4. Lead capture with confirmation.** The bot captures name, email, and phone, with a spell-back-and-confirm loop on each before accepting.
  - Given the bot captures an email, when it has a value, then it reads it back and only proceeds on caller confirmation; on "no", it re-captures.
  - The caller-ID `From` number is offered as the default phone value.
- **R5. Availability + booking via Cal.com.** The bot offers real open slots and creates a booking on the demo event type.
  - Given the caller wants to book, when they pick an offered slot, then the bot calls Cal.com `POST /v2/bookings` with name/email/timezone and confirms success by voice.
  - Given the booking API fails, then the bot apologizes, captures the lead anyway, and tells the caller they'll be followed up — it does not claim a booking that didn't happen.
- **R6. Persist lead + booking to MySQL.** On call end, the captured fields and booking result are written to the `leads_calls` table via the Leads API.
- **R7. Call logging from webhook.** Every inbound call writes a `leads_calls` row at the dial-in webhook (caller number, timestamp, status), independent of whether the bot conversation completes.
- **R8. Webhook signature verification.** The pinless dial-in webhook verifies Daily's signature (reuse the PSTN HMAC pattern) and validates `To` against the configured number.

### P1 — Should-have (fast follow)

- **R9. Transcript summary** stored in the `notes` field per call.
- **R10. Minimal admin UI** — list + detail view over `leads_calls` (sort by date, filter by status).
- **R11. Graceful "not interested" path** — polite close, lead marked accordingly, no hard sell.
- **R12. Duplicate detection** — if `caller_number`/email matches a recent row, link rather than duplicate.

### P2 — Future considerations (design for, don't build)

- **R13. SMS confirmation/reminder** (Twilio/Telnyx + A2P 10DLC).
- **R14. Live human transfer** ("connect me to someone").
- **R15. Multilingual** (re-enable auto-detect for non-English callers).
- **R16. CRM sync** (push leads to HubSpot/Zoho if a sales team adopts one).

---

## 6. Success metrics

**Leading indicators (days–weeks):**

- **Booking conversion:** booked demos ÷ qualified calls. Success ≥ 40%, stretch ≥ 55%.
- **Email capture validity:** valid emails ÷ emails captured. Success ≥ 95%.
- **Capture completion:** calls where all three fields (name/email/phone) confirmed ÷ qualified calls. Success ≥ 80%.
- **Containment:** calls handled end-to-end with no error/dead-end ÷ all answered calls. Success ≥ 90%.
- **Time-to-greeting:** median seconds from connect to first bot speech. Success ≤ 3s.

**Lagging indicators (weeks–months):**

- **Demo show-rate** of bot-booked demos vs. manually booked (are voice-booked demos as real?).
- **Pilot conversion:** pilot hotels that originated from a concierge call.
- **Cost per booked demo** (Daily + Cal.com + model cost ÷ bookings).

**Measurement:** all leading metrics are derivable from the `leads_calls` table + call logs; no extra analytics tooling needed for v1. Evaluate at 2 weeks and again at 1 month post-launch.

---

## 7. Open questions

- ~~(Blocking — Dan) Pricing message the bot may quote~~ — **RESOLVED 2026-05-31.** Approved pricing copy is in §9, matching the website's Petit / Maison / Domaine tiers, and includes the founding-hotels hook. The bot quotes only from §9 and must not improvise pricing. *(Still open: exact founding-offer terms — see §9 note.)*
- **(Blocking — Dan)** What qualifies a demo? Define "qualified call" so booking-conversion is measurable (e.g., caller is a hotel/hospitality decision-maker).
- **(Non-blocking — Dan)** Demo event length: keep the 30-min Cal.com event, or make a dedicated "Voxtera Demo" event type with custom questions?
- **(Non-blocking — legal/Dan)** Is the single recording-consent line sufficient for the caller regions you expect (BC/Canada two-party considerations)? Confirm wording.
- **(Non-blocking — Dan)** Voicemail / after-hours behavior: does anything differ outside business hours, or is the bot identical 24/7?
- **(Non-blocking — eng)** Where does the Leads API run relative to the bot in docker-compose, and what auth (static token vs. mTLS) for bot → Leads API?

---

## 8. Timeline & phasing

No hard external deadline; gated by the pilot outreach ramp. Suggested phasing (mirrors the architecture build order):

1. **Phase 1 — Data spine.** `leads_calls` table + Leads API (`POST /calls`, `POST /leads`, `GET /leads`). Independently testable. (P0: R6, R7)
2. **Phase 2 — Booking tools.** `check_availability` + `book_meeting` against the real Cal.com free account. (P0: R5)
3. **Phase 3 — Persona + capture.** Sales persona, knowledge file, spell-back capture flow, consent line. (P0: R2, R3, R4)
4. **Phase 4 — Telephony.** Pinless dial-in webhook: verify signature, log call, connect bot. Go live on +12363124419. (P0: R1, R7, R8)
5. **Phase 5 — Ops.** Transcript summaries, admin UI, dedup, not-interested path. (P1: R9–R12)

**Dependencies:** Cal.com account (done), purchased Daily number (done, +12363124419), MySQL on Droplet (exists), the bot codebase repo. Blocking open question above (qualification definition) should be resolved before Phase 3.

---

## 9. Prompt catalog (seed) — MANDATORY table format

Per **R0**, every prompt lives here as a table row with a **Group** column. Prompts are **never** embedded inline in code. This is the seed; the full catalog (capture flow, booking, closing, error handling) is completed in Phase 3 and may live in `docs/prompts-catalog.md`.

| ID | Group | When used | Prompt text | Notes |
|----|-------|-----------|-------------|-------|
| P-01 | greeting | Call connects | "Hi, thanks for calling Voxtera — you've reached our voice concierge. Quick note, this call may be recorded for quality. How can I help you today?" | Consent line bundled into greeting (R2). English only (v1). |
| P-10 | pricing | Caller asks "how much / what does it cost" | "It depends on your property size, but to give you a ballpark — independent hotels up to about 40 rooms are $290 a month, full-service hotels and small chains are $790, and for larger chains and resort groups we do custom pricing. We're also welcoming a small group of founding hotels right now at preferential rates. The best way to see exactly what fits — and whether a founding spot is open — is a quick demo. Want me to find you a time?" | Matches website tiers exactly. Founding hook included (terms TBD — see note below). Bot must NOT improvise numbers. |
| P-11 | pricing | Caller asks what's included in a tier | "**Petit, $290/month** — independent hotels up to 40 keys, up to 1,000 conversations a month, web widget plus phone line, ticketing to one channel. **Maison, $790/month**, our most popular — full-service hotels and small chains, up to 5,000 conversations, all channels, full property knowledge base, live dashboard, and human escalation. **Domaine** is custom — for chains and resort groups, with unlimited conversations, multi-property orchestration, and dedicated voice cloning." | Read only the tier(s) the caller asks about; don't dump all three unless asked. |
| P-12 | pricing | Caller pushes for an exact number | "I can't give you an exact figure on the call because it depends on your rooms and call volume — that's exactly what we'd pin down in the demo. Based on what you've told me, you'd most likely be in the **[Petit / Maison]** range. Shall I book you in?" | Bridges to booking; gives a directional tier, not a firm quote. |
| P-20 | founding | Caller asks about the founding-hotels offer | "We're onboarding a small group of founding hotels at preferential rates while we grow — founding partners help shape the product and lock in better pricing. I'd cover the specifics in the demo. Want me to grab you a slot before they fill up?" | **Founding-offer terms TBD** — confirm specifics (free months / % off / number of slots) with Dan, then update this row. Until then, keep framing non-committal. |

> **Open item — founding-offer terms:** the rows above use a deliberately vague "preferential rates" framing. Before launch, Dan to confirm the concrete offer (e.g., months free, % discount, capped number of founding slots) so P-20 (and the hook in P-10) can state something specific without over-promising.
