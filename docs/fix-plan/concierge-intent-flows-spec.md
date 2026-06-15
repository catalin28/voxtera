# Concierge Dialog: Structured Intent Flows — Design Spec

**Status:** Draft for implementation
**Date:** 2026-06-15
**Source:** Two tester sessions (EN + TR) + live trace `wacall_64e386fd266a` + second-LLM TR-log analysis

---

## P0 — Prompt-scaffold leak (fix before anything else)

**Symptom:** At ~283s in the TR call the bot answered the dress-code question, then
kept generating — emitting fake `User:` / `Assistant:` turns, the
`Detected language:` and `Conversation so far:` headers, and the raw transcript into
TTS, and **fabricated a reservation the guest never made.** User-visible, spoken aloud.

**Root cause:** the prompt is assembled as one plain-text string with role headers and
**no stop sequence**, so once the model finishes the real answer it autocompletes the
scaffold pattern it was shown. The split-utterance race (below) fired a **second
`llm_start` before the first finished**, and that second, unguarded call is the one
that leaked.

**Fix (code, not prompt wording):**
1. **Use structured messages, not a hand-built string.** Pass `system` + a
   role-separated `messages` array to the Claude Messages API. Do not concatenate the
   transcript with `User:` / `Assistant:` headers into a single text prompt — that is
   what the model learns to continue.
2. **Add stop sequences** as a belt-and-suspenders guard (e.g. `\nUser:`,
   `\nAssistant:`, `\nDetected language:`) so generation halts even if scaffold text
   reappears.
3. **Cap `max_tokens`** to a turn-sized budget so a runaway generation can't dump a
   full transcript.
4. **One in-flight LLM call per turn** (see §6) — cancel or coalesce a second
   `llm_start`; never run two completions for one turn.

This is the top priority: it is user-facing, breaks trust, and invents bookings.

**Reproduced in trace `wacall_e89b37e8562e` (2026-06-15T08:11):** the dress-code turn
fired `llm_start` twice on one turn — at 276.5s for "…Bosphorus Grill için dress code
var mı?" and again at **278.4s for "Altyazı M.K."** (a subtitle-credit STT
hallucination) **before the first completed**. The second call's `llm_full` ran 4921ms
and produced the leak; the spoken reply literally contained `User: Hayır, teşekkür
ederim…` / `User: Bosphorus G`. This is the exact double-`llm_start` mechanism above.
Stop sequences (step 2, ✅ shipped) stop the *scaffold text* being spoken, and Phase 3's
hallucination filter now drops "Altyazı M.K." before it becomes a turn — so this
reproduction is fully closed. (The single-in-flight guard, step 4, was NOT implemented
and on review is not required: no trace shows two overlapping spoken replies, and the EN
`now.Perfect` concat was a *single* `llm_start`, not a double call. It remains optional
defence-in-depth only.)

---

## Problem

The concierge has no explicit dialog state. The LLM re-decides every turn what stage
the conversation is in, what fields are required, and what language to speak. That
improvisation produces every bug both testers hit:

- Collects reservation details before the guest has committed to booking.
- Misreads relative dates ("tomorrow" → "today").
- Tries to submit a reservation with no name or contact.
- Treats room number as mandatory; phone as a fallback.
- Switches language mid-call without being asked.
- Offers to "look up" information it has no tool to fetch, then goes silent.
- Replays the whole conversation back at the end.

The fix is to make stage, slots, and language **explicit state** carried in
`CallContext`, with the LLM filling slots inside a fixed state machine rather than
driving the flow freely.

---

## 1. Intent schemas (mandatory vs optional slots)

Each bookable intent declares required and optional slots. The flow cannot reach
`SUBMITTED` until every mandatory slot is filled. Optional slots are offered only
after mandatory slots are complete, and never block submission.

**Qualifying slot — `guest_type` (asked first, mandatory for every booking intent):**
Is the caller an **in-house guest** (staying at the hotel) or an **external visitor**
(coming in just for the restaurant/spa)? This is collected early because it branches
which downstream slots are mandatory.

```
restaurant_reservation
  qualifying: guest_type (in_house | external)        # asked first
  mandatory:  date, time, party_size, restaurant, guest_name
  contact:    if in_house  -> room_number (phone optional)
              if external  -> phone OR email
  optional:   special_requests   # occasion, dietary, seating

spa_reservation
  qualifying: guest_type (in_house | external)        # asked first
  mandatory:  date, time, service, guest_name
  contact:    if in_house  -> room_number (phone optional)
              if external  -> phone OR email
  optional:   therapist_pref, special_requests
```

Rules:
- **Ask `guest_type` before any contact field.** Don't assume the caller is staying.
- **In-house** → room number is the contact handle (phone optional extra).
- **External** → phone OR email is required; **never ask for a room number.** (Fixes
  the "insisted on room number / I'm not staying" complaint — the bot was assuming
  everyone is a guest.)
- New intents (transport, activity booking, etc.) are added as new schemas only.

---

## 2. Dialog stages (per active intent)

State machine carried in `CallContext.active_intent` + `CallContext.stage`:

| Stage | Bot behavior | Exit condition |
|-------|--------------|----------------|
| `EXPLORING` | Answer questions, give info, offer help. **Do not collect slots.** When a detail slips out, store it silently but stay in EXPLORING. | Explicit commitment signal → COLLECTING |
| `COLLECTING` | Fill missing **mandatory** slots one at a time. Offer optional slots only after mandatory complete. | All mandatory filled → CONFIRMING |
| `CONFIRMING` | Read back the full resolved summary, ask for a single yes/no. | "Yes" → SUBMITTED. "No"/correction → back to COLLECTING |
| `SUBMITTED` | Emit **exactly one** closing line, then stop. | Terminal |

**Intent ≠ commitment.** Detecting "restaurant reservation" lands the guest in
`EXPLORING`, not `COLLECTING`. (Fixes complaint #1.)

Commitment signal = explicit user intent to book ("yes, let's book", "make the
reservation", "go ahead"). In `EXPLORING`, when the guest has shown interest but not
committed, the bot asks a readiness question instead of collecting:

> "Is there anything else you'd like to know about our restaurants, or are you ready
> to make the reservation?"

---

## 3. Relative date & time normalization

The LLM drops the relative offset ("tomorrow at 7" → "today at 7"). Fix:

- **Timezone comes from `HotelConfig`, not the call.** Add a `timezone` field (IANA,
  e.g. `Europe/Istanbul`) to each hotel's config, next to the existing per-hotel
  greeting. The reservation clock is always the **hotel's local time** — a guest
  calling from another country still books "7pm" in the hotel's timezone, not theirs.
- Inject **current date + time in the hotel's timezone** into the dialog context every
  turn (derived from `HotelConfig.timezone`).
- Resolve relative expressions ("tomorrow", "tonight", "this Friday") against the
  hotel's local now, to an **absolute date at capture time**, and store the absolute
  value in the slot.
- Always **echo the resolved absolute date** in the CONFIRMING read-back:
  *"tomorrow, Tuesday June 16, at 7:00 PM."*

This makes the error visible to the guest before submission instead of silently
booking the wrong day. (Fixes complaint #2.)

---

## 4. Language-lock policy

Detect language once, lock it, never switch unless explicitly asked.

- On first user utterance, detect and set `CallContext.locked_language`.
- For the rest of the call, **render and TTS in `locked_language` only.**
- Do **not** switch on per-utterance STT language guesses. Short interjections like
  "Alo" (Turkish phone-hello) must not flip the session to French.
- Switch language **only** on an explicit user request ("can we continue in English?").
  On such a request, confirm and update `locked_language`.

Implementation note: this pairs with the Gladia config — pin `GLADIA_LANGUAGES` to the
expected set rather than full auto-detect, and ignore the provider's per-utterance
language field once `locked_language` is set. (Fixes complaint #2 from the TR call.)

---

## 5. No-knowledge / no-tool policy

When the guest asks something not answerable from RAG:

- If RAG returns no confident hit **and** no tool is bound for that query type:
  say plainly *"I don't have that information."* (optionally offer to pass the
  question to staff).
- **Never offer a capability that isn't wired.** Do not say "shall I look that up?"
  unless a web-search / lookup tool is actually bound and will execute.
- **Never go silent.** Every turn must produce a spoken response; a RAG miss is a
  valid answer, not a dead end.

(Fixes complaint #4 from the TR call — the Michelin-star question that produced a
false offer followed by dead air.)

---

## 6. End-of-call single-closing rule

Both the trace and the TR tester show the bot concatenating/replaying at the end
(`...now.Perfect —` in the trace; full-conversation replay in the TR call).

- `SUBMITTED` emits **exactly one** closing utterance, then stops.
- Guard against double-generation: the streaming reply and the final reply must not
  both be flushed (this is the `now.Perfect` concat seam).
- No transcript playback. The closing line confirms the booking and the contact
  channel — nothing more.

(Fixes complaint #1 from the TR call and the concat bug in the trace.)

**One in-flight LLM call per turn.** When a trailing STT fragment arrives while a
completion is already running for the current turn, **cancel or coalesce** it — do not
spawn a second `llm_start`. In the TR call a trailing fragment ("Altyazı M.K.") started
a second call before the first finished, and that second call is the one that leaked
the prompt scaffold. A turn owns at most one running completion.

---

## 8. Response-length cap

Replies ran 300–430 chars — roughly 2× the ~200-char target — and guests repeatedly
talked over the bot (`user_started` coinciding with `bot_done` 5+ times; ~8,000 frames
silenced by the leakage guard).

- Constrain replies to the ~200-char target; one question or one confirmation per turn.
- In `COLLECTING`, ask for **one slot at a time** — don't stack multiple asks.
- Shorter replies reduce barge-in, leakage-guard load, and TTS cost.

---

## 9. STT hallucination filtering

Gladia/Whisper emit phantom transcripts on silence and mishear proper nouns:

- "Altyazı M.K." — a subtitle-credit hallucination on silence, treated as a real turn.
- "Bosphorus Rouille" for "Bosphorus Grill"; Turkish misrecognized as English ("It's fun.").

Rules:
- Drop known silence-hallucination patterns (subtitle credits like "Altyazı …",
  "Subtitles by …", channel sign-offs) before they become a turn.
- Require VAD-confirmed speech / a minimum confidence before treating a transcript as a
  user turn — don't `llm_start` on a fragment that arrived during silence.
- Pin proper nouns (venue names) via the Gladia bias list + fuzzy resolver already in
  `entity_resolver.py`.

---

## 10. Grounding / anti-fabrication

The bot invented facts when it lacked data: a "Smart Chic" dress code, "Hünkâr Beğendi
2.0", Michelin claims, and inconsistent restaurant lists (Ruya/Rüya duplicated;
Bosphorus Grill introduced mid-call but absent from the original list).

- Answers about venues, menus, dress codes, awards, and hours must come from RAG. If
  it's not retrieved, fall back to the §5 no-knowledge response — **never invent.**
- Keep one canonical restaurant list per tenant; de-dupe name variants (Ruya/Rüya) in
  the knowledge base so the bot can't present conflicting lists.
- No unsourced superlatives ("Michelin-starred", "the best") unless present in RAG.

---

## 11. Out of scope (tracked separately)

- **Audio glitches** (TR complaint #3) — STT/TTS/transport layer, not dialog logic.
- **Latency:** end-to-end 1.8–3.0s is acceptable; Gladia STT spiked to 4.4–5.1s several
  times — provider finalization, tracked with the STT config work.

---

## Implementation surface

State lives on the per-call `CallContext` (`active_intent`, `stage`, `slots`,
`locked_language`). The decompose step classifies intent and commitment signals and
advances the stage; the render step is constrained by the current stage (what it may
ask, what it may not). Intent schemas are declarative data so new flows (spa,
transport) are added without touching the state machine.
