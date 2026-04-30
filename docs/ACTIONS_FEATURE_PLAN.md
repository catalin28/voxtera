# Voxtera — Action-Taking Feature: Development Plan

**Audience:** Engineering — planning document, not an implementation spec.
**Project:** Voxtera — multilingual real-time voice agent for tourism.
**Status:** Pre-implementation. No code yet.

---

## 1. What we're building

Today the bot only *talks*. We are adding the ability for the bot to *do things* during a call — specifically, to file tickets when a guest reports a problem or makes a request (maintenance, reservation, concierge, etc.). The ticket appears in real time in a hotel-staff channel, who then act on it.

For the demo and early stage, tickets are sent to a single Telegram channel. The architecture is built so this destination can be swapped for a real ticketing system (Freshdesk first, Zendesk later) without touching the bot's conversation logic.

---

## 2. Goals

1. The bot recognizes when an action is needed (complaint, request, booking) versus when a plain answer is enough.
2. The bot gathers any missing information (e.g. room number) before filing.
3. The bot **always** confirms with the guest before filing a ticket.
4. The ticket is posted to a Telegram channel with a category prefix — `[Maintenance]`, `[Reservation]`, etc.
5. The ticket message is written in the **hotel's official language**, even when the guest spoke in a different language. The original-language quote is preserved in the message for context.
6. Within a single call, the bot remembers guest info (room number, name) and does not re-ask.
7. The destination ("sink") is swappable: Telegram today, Freshdesk later, Zendesk eventually.

---

## 3. Locked-in design decisions

These were agreed during planning and should not be re-debated unless we hit a blocker.

### 3.1 Categories (the prefix tag)

A finite set Claude must choose from. No free-form categories.

`[Maintenance]`, `[Reservation]`, `[Concierge]`, `[Restaurant]`, `[Housekeeping]`, `[Lost & Found]`, `[Complaint]`, `[Emergency]`, `[Feedback]`, `[Other]`

Per-hotel config can restrict this list to the categories that hotel cares about.

### 3.2 Confirmation flow

The bot **always asks before filing**. Standard sequence:

1. Guest describes the issue.
2. Bot gathers any missing info (e.g. asks for the room number if not already known).
3. Bot summarizes the request back to the guest in the guest's language.
4. Bot asks: *"Shall I send this to the [team] team?"*
5. On **yes**: bot files the ticket and confirms aloud.
6. On **no** or hesitation: bot drops the ticket gracefully and continues the conversation.

The summary-back step is the safety net against wrong tickets. It is not optional.

### 3.3 Languages: guest vs. hotel

The bot speaks to the guest in the **guest's** language (auto-detected by Whisper).
The Telegram message is written in the **hotel's official language** (per-hotel config).
The original-language quote from the guest is included in the Telegram message verbatim, so staff can see exactly what was said in case translation lost nuance.

### 3.4 Session memory

A per-call session state object holds guest-level info that should persist across multiple tickets in the same call:

- Room number
- Guest name (if given)
- Detected guest language
- Hotel config (loaded once at call start)

Once a value is captured, the bot reuses it for subsequent tickets without re-asking. The summary-back step naturally exposes cached values so the guest can correct them if wrong (e.g. a guest filing on behalf of someone in a different room).

### 3.5 Ticket message format (Telegram)

```
[Maintenance]
Room 412 — AC not cooling since last night.
Guest spoke in: French
Original: "La climatisation ne fonctionne pas depuis hier soir."
Session: vox-2026-04-29-1432
```

No severity field. No priority. Just the prefix, the summary in hotel language, the original quote, and the session ID for traceability.

### 3.6 Sink architecture

A `TicketSink` abstraction defines the contract: *given a structured ticket, deliver it somewhere*. Concrete implementations:

- **TelegramSink** — built first, used for the demo.
- **FreshdeskSink** — built second, before first paid client demo.
- **ZendeskSink** — built on demand when a client uses Zendesk.

The bot's conversation logic and tool definitions stay identical across sinks. Only the transport changes.

---

## 4. Architecture overview

```
Guest speaks
   ↓
Whisper STT (detects language, transcribes)
   ↓
Pipecat context (with session state: room#, name, language, hotel config)
   ↓
Claude Sonnet 4.6 (with create_ticket tool registered)
   ↓
   ├─→ Direct response → Chirp 3 HD TTS → Guest          (most turns)
   │
   └─→ Tool call: create_ticket(category, summary, ...)   (action turns)
         ↓
       Confirmation gathered from guest first
         ↓
       TicketSink.send(ticket)
         ↓
       Telegram Bot API → Hotel staff channel
         ↓
       Bot confirms aloud to guest in guest's language
```

Session state lives in Pipecat's context, augmented with a Voxtera-specific `guest_info` and `hotel_config` block that the `create_ticket` tool reads from.

---

## 5. Development phases

The work is broken into seven phases. Each phase is independently testable and produces something demoable.

### Phase 1 — Telegram plumbing

Goal: prove we can post a ticket to Telegram from a Python script, end-to-end.

Steps:

1. Create a Telegram bot via BotFather and obtain the bot token.
2. Create a private Telegram channel for the demo. Add the bot as an admin.
3. Obtain the channel ID (numeric, starts with `-100...`).
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHANNEL_ID` to `.env`.
5. Define a `Ticket` data class with the fields needed for the message format (category, summary, original_quote, language_detected, room_number, session_id, timestamp).
6. Define an abstract `TicketSink` interface with a single `async send(ticket)` method.
7. Implement `TelegramSink` — uses `aiohttp` to call Telegram's `sendMessage` endpoint. Format the message per section 3.5.
8. Write a one-off test script that builds a fake ticket and sends it. Confirm it appears in the channel.

**Done when:** running the test script causes a correctly formatted message to appear in the Telegram channel.

### Phase 2 — Hotel config

Goal: introduce the multi-tenant config layer so the bot knows which hotel it's serving.

Steps:

1. Define a `HotelConfig` data class: hotel name, official language (BCP-47), telegram channel ID, list of allowed categories, optional system-prompt addendum (hotel-specific facts).
2. Pick a config storage format — YAML files in `/config/hotels/` keyed by hotel ID.
3. Create one config file for the demo hotel.
4. Load the active hotel config at call start. For now, hotel ID is hardcoded or passed as a CLI arg; later it'll come from the call's metadata.

**Done when:** the bot prints the loaded hotel config at startup and uses the official language as the target language for ticket messages.

### Phase 3 — Tool definition and prompt update

Goal: register `create_ticket` as a Pipecat function, and update the system prompt so Claude uses it correctly.

Steps:

1. Define the `create_ticket` function schema for Pipecat's function-calling layer. Required parameters:
   - `category` — enum of allowed categories
   - `summary` — string, in the hotel's official language
   - `original_quote` — string, in the guest's language
   - `room_number` — string
2. Register the function with the LLM service in `bot.py`.
3. Update the system prompt to instruct Claude on:
   - When to use the tool (complaints, requests, bookings — not for plain Q&A).
   - The confirmation rule: always summarize and ask "shall I send this?" before calling the tool.
   - Translation responsibility: `summary` must be in the hotel's language; `original_quote` is the guest's verbatim words.
   - Reuse rule: if room number is already known from earlier in the call, do not ask again.
4. Wire the tool's execution path: when Claude calls `create_ticket`, the bot constructs a `Ticket` object (adding session_id and timestamp) and passes it to the active `TicketSink`.

**Done when:** in a manual test call, the bot can recognize a maintenance complaint, ask for room number, summarize, ask for confirmation, and on yes, post a correctly formatted Telegram message.

### Phase 4 — Session state for guest info

Goal: make the bot remember room number and guest name across multiple tickets in one call.

Steps:

1. Add a `guest_info` dict to the bot's session context: `room_number`, `guest_name`, `detected_language`.
2. Update the system prompt: tell Claude that this state exists, that it's auto-populated as info is gathered, and that the tool may read from it as defaults.
3. Make `create_ticket`'s `room_number` parameter optional in the schema, so Claude can call the tool relying on cached state.
4. Update the bot's tool-execution wrapper to pull `room_number` from `guest_info` if Claude omits it.

**Done when:** in a single call, a guest opens a maintenance ticket (bot asks room number once), then opens a reservation ticket — bot does not re-ask the room number, but the summary-back utterance still mentions it so the guest can correct it.

### Phase 5 — UX polish: latency-fillers and confirmation copy

Goal: make the action turns feel natural, not robotic.

Steps:

1. Tool calls take 1–2 seconds. Add a "while-filing" utterance the bot says before the action runs: e.g. *"One moment, I'm sending this to the team."* Localized in each language.
2. After the tool succeeds, add a confirmation utterance: *"Done — the maintenance team has been notified. They'll be with you shortly."*
3. On tool failure (Telegram API error), add a fallback utterance: *"I'm having trouble reaching the team. Please call the front desk and ask for maintenance — I apologize for the inconvenience."*
4. Test these utterances in the top 5 demo languages (English, French, Spanish, German, Japanese) — Chirp 3 HD pronunciation check.

**Done when:** action turns sound conversational in all five demo languages.

### Phase 6 — Testing

Goal: verify the feature behaves correctly across realistic scenarios.

Manual test scenarios:

1. **Cross-language ticket.** French-speaking guest reports broken AC. Telegram message arrives in English, summary correct, original French quote intact.
2. **Multi-ticket session.** Same call: one maintenance ticket, then one reservation ticket. Room number asked once.
3. **Cancellation.** Guest says "no, never mind" after the summary-back. No ticket filed. Conversation continues normally.
4. **Wrong-room correction.** Bot summarizes with cached room 412. Guest says "no, this is for room 413, my friend's room." Bot updates and re-confirms before filing.
5. **Telegram down.** Simulate by using a bad token. Bot delivers the fallback utterance, no ticket lost silently.
6. **Rare language.** Guest speaks in Tagalog or Vietnamese. Verify Claude still triggers the tool correctly and the English summary is accurate.
7. **Multiple complaints in one breath.** Guest says "the AC is broken, the towels are dirty, and I want to extend my stay." Bot files three separate tickets sequentially or batches them — observe Claude's behavior, document it, decide if we want to constrain it.

**Done when:** all seven scenarios pass without code changes, just by talking to the bot.

### Phase 7 — Demo readiness

Goal: make it look good when shown to clients.

Steps:

1. Set up a dedicated demo Telegram channel separate from the dev channel.
2. Pre-write a short demo script for sales: 4–5 turns, two filed tickets, guest in French, hotel staff seeing English.
3. Ensure the demo Telegram channel can be screen-shared during a sales call (web client works well for this).
4. Document how to onboard a new "demo hotel" config quickly (5-minute setup) so the same bot can be rebranded mid-call if a client wants to see their own name.

**Done when:** sales team can run the demo end-to-end without engineering support.

### Phase 8 — Interactive staff actions (Telegram inline buttons)

Goal: turn the staff channel from a passive feed into an interactive workspace. Staff click buttons under each ticket to acknowledge, claim, escalate, or resolve, and the bot reflects the new state in real time. This is the demo moment that turns "the AI files a ticket" into "the AI runs an operations loop".

#### 8.1 Why this matters

A ticket arriving in Telegram is good. A ticket arriving with `[🔍 Find available maintenance]` `[✅ I'm on it]` `[❌ Pass to next shift]` underneath is *visibly* a workflow — the kind of thing a hotel ops manager understands without an explanation. Adding this raises the demo's perceived value disproportionately to its build cost.

#### 8.2 Architecture additions

The bot is currently fire-and-forget — it sends, it does not listen. This phase adds an inbound channel from Telegram back into Voxtera.

```
Voxtera bot → Telegram API: sendMessage with inline_keyboard
                                   ↓
                              Telegram channel
                                   ↓ (staff clicks button)
                              Telegram API
                                   ↓
Voxtera bot ← long polling getUpdates ← TelegramListener
                                   ↓
                              ButtonAction registry → handler
                                   ↓
                              State update + editMessageText
                                   ↓
                              (optional) push event back into voice loop
```

Two channels of inbound events to consider:

- **Long polling** — a background task polls Telegram's `getUpdates` endpoint. Simplest to set up, no public URL needed, fine for demo.
- **Webhook** — Telegram posts to our public URL. More elegant, requires SSL endpoint (we already have nginx on the Droplet). Switch to this when we go to production.

Phase 8 builds long polling first; webhook is a swap-in later.

#### 8.3 New components

1. **`TelegramListener`** — async background task running `getUpdates` with a 30s long-poll timeout. Emits a `ButtonClicked` event for each `callback_query`. Survives Telegram brief outages by retrying with exponential backoff.

2. **`ButtonAction` interface** — abstract async callable: takes a `ButtonClicked` event, returns an `ActionResult` (new message text, optional new buttons, optional notification to push back to the voice bot).

3. **`ActionRegistry`** — maps `action_id` → `ButtonAction`. Registered at startup, hotel-specific subset configurable via `HotelConfig`.

4. **`TicketRecord`** — persistent state per ticket: `session_id`, `telegram_message_id`, `status` (one of `open`, `claimed`, `assigned`, `resolved`), `claimed_by`, `assigned_to`, action history. Stored in SQLite (small table, easy to inspect during demo).

5. **`InteractiveTelegramSink`** — extends/replaces `TelegramSink`. On `send`, attaches an `inline_keyboard` derived from the ticket's category. Returns the Telegram `message_id` so we can later call `editMessageText` on the same post. Persists a `TicketRecord`.

#### 8.4 Button catalogue (initial)

Wired per category. Each button stores a compact `action_id|session_id` in `callback_data` (Telegram limit: 64 bytes — short IDs matter).

| Category | Buttons |
|---|---|
| Maintenance | `🔍 Find available technician` · `✅ I'm on it` · `❌ Pass to next shift` · `✓ Resolved` |
| Concierge | `📞 Call guest` · `✅ Handling` · `✓ Resolved` |
| Restaurant | `📅 Add to reservations` · `✅ Confirmed` · `❌ Fully booked — apologise` |
| Housekeeping | `✅ Sending now` · `✓ Done` |
| Lost & Found | `🔍 Search inventory` · `✅ Found` · `❌ Not found yet` |
| Emergency | `📞 Call guest` · `🚨 Notify manager` · `✓ Resolved` |
| Complaint | `📞 Call guest` · `✅ Handling` · `📝 Request manager` |
| Reservation | `✅ Booked` · `❌ Unavailable` |
| Feedback | `👁 Acknowledged` |
| Other | `✅ I'm on it` · `✓ Resolved` |

#### 8.5 Demo data and mock actions

For the demo, several actions need fake-but-credible data to act on:

- **`Find available technician`** — pick from a YAML-configured list (`config/hotels/<hotel_id>/staff.yaml`) using a simple "least recently assigned" rule. Output: edit the ticket message to *"✅ Maintenance assigned: John Smith — ETA 5 min"*.
- **`Call guest`** — generate a `tel:` URL with the room's PMS-on-file number (mocked for demo). Output: a follow-up message with a clickable phone link.
- **`Add to reservations`** — for restaurant tickets, write to a per-hotel JSON file simulating the restaurant's ledger. Output: confirmation with the booking time.

These are deliberately mock implementations. When real PMS / staff scheduling integrations land, only the action handlers change.

#### 8.6 State machine

A ticket moves through:

```
open  → claimed (one staff member tapped "I'm on it")
      → assigned (auto-assigned by Find-available action)
      → resolved (any staff tapped "Resolved" or category-specific equivalent)
```

Race condition handling: the first click wins. Subsequent clicks on a now-stale button receive an `answerCallbackQuery` toast: *"Already claimed by John Smith."* No state mutation.

#### 8.7 Optional: feedback loop into the voice bot

If the action loop completes within a few seconds (fast resolution), an optional notification can be pushed into the active voice call, so the bot tells the guest: *"Maintenance is on the way — they'll be at your room in five minutes."* This requires a side channel from the action layer to the active Pipecat task. Worth scoping separately; flag as Phase 9 if pursued.

#### 8.8 Development steps

1. Extend `Ticket` flow to retain `telegram_message_id` after send (currently discarded).
2. Add `TicketRecord` table to a small SQLite database at `data/tickets.db`.
3. Build `InteractiveTelegramSink` as a thin extension of `TelegramSink` that attaches `inline_keyboard` markup and persists the record.
4. Build `TelegramListener` as a standalone async task. Start it from `bot.py` when `INTERACTIVE_ACTIONS_ENABLED=true`.
5. Build `ActionRegistry` and an initial set of three or four actions (`acknowledge`, `find_available`, `resolve`, `call_guest`). Wire them via per-category button maps.
6. Add `staff.yaml` per hotel with mock staff lists.
7. Manual test: post a ticket, click each button, observe message edits and channel toasts.
8. Add a small demo script (`scripts/test_interactive_buttons.py`) that posts a ticket and prints the listener's events for 30 seconds.

#### 8.9 Risks and edge cases

- **Callback data 64-byte limit.** Keep `action_id` short; consider hashing `session_id` to a 6-char prefix.
- **Bot-must-be-admin (already true)** — same permission as Phase 1, no new setup.
- **Listener crashes** — must restart cleanly without losing the polling offset. Persist offset to disk.
- **Multiple bots in one channel** — irrelevant for demo, becomes relevant at scale; bot only handles its own callbacks (Telegram dispatches by `bot_id`).
- **Reverting** — `INTERACTIVE_ACTIONS_ENABLED=false` falls back to plain `TelegramSink`. Tickets still arrive, just without buttons.

#### 8.10 Done when

- A staff member can click `I'm on it` on a maintenance ticket and the message updates with their name and timestamp.
- A staff member can click `Find available technician` and a name is auto-assigned within 2 seconds.
- A staff member can click `Resolved` and the ticket is visually marked done (struck through or prefixed with ✓).
- Two staff members tapping the same button in the same second produce one update; the slower one sees a "already claimed" toast.
- The whole loop survives killing and restarting the bot mid-demo without losing in-flight ticket state.

---

## 6. Out of scope (for now)

Things deliberately excluded from this phase. Add them after the demo lands its first paid pilot.

- **FreshdeskSink** — built immediately after the demo proves out, before first paid client.
- **ZendeskSink** — built when a client specifically requires it.
- **Audio clip attachment** — including the original voice recording with the ticket. Useful for verbatim review but not needed for v1.
- **Severity / priority field** — hotels may want this later. Skipping for now per decision in section 3.5.
- **Multi-channel routing** — separate Telegram channels per category. Single channel with prefixes is simpler and works for the demo.
- **PMS integration** — caller ID → reservation lookup → auto-fill guest name and room. Reduces friction but requires a real PMS partner.
- **Ticket status tracking** — Telegram is fire-and-forget. Status only matters once we move to a real ticketing system.
- **Multiple complaints in one tool call (batching)** — Claude currently makes sequential calls. Optimize only if it causes noticeable lag.

---

## 7. Open questions

These are not blockers, but should be revisited during or after Phase 6.

1. If the guest hangs up mid-confirmation (after the summary, before saying yes/no), should the bot file the ticket on a best-effort basis or drop it? Current default: drop.
2. Should the bot ever proactively suggest filing a ticket — e.g. *"That sounds like something I should report to maintenance — would you like me to?"* — when the guest is venting but hasn't explicitly asked? Worth A/B-ing with friendly hotel partners.
3. Rate limits: Telegram allows 30 messages/second per bot, far above demo volume. Revisit only if we run multi-tenant on shared bot tokens.
4. How do we handle ticket idempotency if the guest repeats themselves and Claude triggers the tool twice? In-session deduplication (same category + same room within N seconds → suppress) is one option. Worth measuring before solving.

---

## 8. Estimated effort

Rough engineering estimate, single developer:

| Phase | Hours |
|---|---|
| 1. Telegram plumbing | 3–4 |
| 2. Hotel config | 2 |
| 3. Tool definition + prompt | 4–5 |
| 4. Session state | 2 |
| 5. UX polish | 3 |
| 6. Testing | 4 |
| 7. Demo readiness | 2 |
| 8. Interactive staff actions (Telegram buttons) | 5–7 |
| **Total** | **~25–29 hours** |

Most of the risk is in Phase 3 (prompt engineering — getting Claude to consistently confirm before filing across languages). Budget a buffer there. Phase 8 can be deferred until Phases 1–7 are demo-stable; it is independently valuable and slot-ins behind a feature flag.

---

## 9. Success criteria for the feature

The feature ships successfully when:

1. A non-technical observer watching the demo says "wow, it actually did something" within the first action turn.
2. In 20 test calls across 5 languages, the bot correctly files a ticket on every clear request and never files on a plain question or a "no" confirmation.
3. The hotel config can be swapped in under 5 minutes per new demo hotel.
4. Replacing `TelegramSink` with `FreshdeskSink` (when we get there) requires zero changes to the bot's conversation logic.
5. (Phase 8) During the demo, an observer sees the bot file a ticket *and* sees a staff member click a button under it that triggers a visible state change — proving the loop is bidirectional, not just one-way notification.
