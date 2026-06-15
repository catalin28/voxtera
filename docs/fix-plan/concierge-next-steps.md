# Concierge Fix Plan — Next Steps / Handoff

**Date:** 2026-06-15
**Branch:** `fix/concierge-prompt-and-language`
**Companion docs:** [`concierge-fix-plan.md`](concierge-fix-plan.md),
[`concierge-intent-flows-spec.md`](concierge-intent-flows-spec.md),
[`../call-center/phase6-verification.md`](../call-center/phase6-verification.md)

This is the running TODO for whoever picks the concierge work up next. All six
phases of the fix plan are **coded and offline-tested**; the remaining work is
live behavioural validation, one open bug, and a few smaller follow-ups.

---

## Where things stand

| Phase | What | Status |
|---|---|---|
| 1 | P0 prompt-scaffold leak (structured messages, stop_sequences, max_tokens) | ✅ shipped + tested |
| 2 | Language lock after first utterance | ✅ shipped + tested |
| 3 | STT hallucination filter + ~200-char voice cap | ✅ shipped + tested |
| 4 | Booking flow — prompt-guided + hotel-local dates + slot tracking | ✅ coded + tested; ⚠️ see open items |
| 5 | Grounding / anti-fabrication (capability boundary, no unsourced awards, dedupe) | ✅ shipped + tested |
| 6 | End-to-end verification (offline harness + matrix) | ✅ offline done; ⚠️ live pending |

Offline suite: **644 passing**, ruff clean (after the one-line lint fix in this
change). The concierge brain runs **text-in/text-out** via `scripts/ask.py`
(no STT/TTS) — that's the fastest way to manually exercise behaviour.

---

## Design decision to respect (don't re-litigate without the user)

The booking flow is **prompt-guided, not a Python state machine** (the user
chose this explicitly — see the project memory). The LLM owns the booking
conversation and files via the `create_ticket` tool. Python only:
- supplies the **hotel-local clock** (`HotelConfig.timezone` → `hotel_time.py`)
  so relative dates ("tomorrow 7pm") resolve to absolute dates;
- injects the **booking rules** (`intents.booking_guidance_block`, built from the
  declarative restaurant/spa schemas);
- tracks **booking slots** via a separate **slot-extraction LLM call**
  (`booking_extract.py`), persisted in `session["booking_slots"]` and fed back
  next turn as a LOCKED recap (`intents.booking_recap`).

Two mechanisms were tried and **rejected** for slot tracking — don't bring them
back without reason:
- Python regex/heuristic intent detection (user: "the LLM must determine this").
- The render model self-reporting slots via a `record_booking_progress` tool —
  on Haiku it either skipped the tool (slots never filled) or **narrated the
  tool into speech** ("I'll record the booking details…"), leaking machinery.

The current `booking_extract.py` (a dedicated, silent, parallel extraction call)
is the replacement. It runs **in parallel** with the spoken render, so no added
wall-clock latency, but it is **one extra LLM call per property turn** where
ticketing is enabled — see Cost below.

---

## Open items (prioritised)

### 1. ⚠️ Verify the slot-drift fix live (booking_extract)
The extraction approach is coded + unit-tested but **not yet confirmed live**.
The earlier prompt-only attempt failed live (re-asked the date; swapped the
chosen restaurant Tuğra → Le Fumoir on the confirmation read-back). Re-run the
multi-turn booking through `scripts/ask.py` and confirm the recap now pins the
venue/date/party and the model stops re-asking:
```
S=drift-check
python scripts/ask.py "book a table for two tomorrow 7pm" -s $S --hotel-id kempinski_ciragan --brief --language en
python scripts/ask.py "Tugra, we're outside guests" -s $S --hotel-id kempinski_ciragan --brief --language en
python scripts/ask.py "under the name Daniel, phone 555 1234" -s $S --hotel-id kempinski_ciragan --brief --language en
```
Expect: guest_type asked first, **Tuğra kept** through the read-back, **no
re-asking** of date/party, absolute date ("Tuesday the sixteenth of June"),
external → phone (never a room number), one "shall I send this?" and no ticket
until "yes". Do NOT confirm with a real "yes" unless you mean to file a real
Telegram ticket (`TELEGRAM_BOT_TOKEN` is live in `.env`).

### 2. Cost/latency decision on `booking_extract`
Right now the extractor runs on **every property turn where a ticketer exists**
(even "what time is breakfast?"), returning `{}` for non-booking turns. It's
parallel (no latency hit) but it is a second LLM call per turn (cost).
Options if cost matters: gate it (only run once a booking context exists), or
accept it. Left ungated intentionally — no Python heuristic decides "is this a
booking". Decide with the user.

### 3. Grounding spot-check: empty property KB still answered specifically
During live testing, `kempinski_ciragan` returned **no KB chunks**
("[property-kb] no guide chunks") yet the bot still gave specific breakfast
hours. Confirm that came from the hotel-config `system_prompt_addendum` (allowed)
and not unsourced invention (Phase 5 violation). If the RAG isn't ingested in
the target env, ingest it — see `voxtera ingest` / the Çırağan KB commits.

### 4. Live replay of the full Phase 6 matrix
Run the **LIVE** rows in [`phase6-verification.md`](../call-center/phase6-verification.md)
§3 against the running stack (keys + Qdrant/ES/Redis + audio via the Turkish
test kit). These cover LLM *compliance* (booking rules, language lock under real
STT, awards grounding) plus latency and leakage-guard frame counts — none of
which the offline suite can prove.

### 5. Uncommitted/earlier follow-ups
- `docs/fix-plan/last_chat.md` is an untracked scratch dump — delete or ignore.
- Phase 4 `_run_property_fast` skips the decomposer by design; if booking ever
  needs to work on the **travel** (multi-hotel) path too, that path differs.

---

## Key files (booking flow)

| File | Role |
|---|---|
| `src/voxtera/actions/hotel_config.py` | `HotelConfig.timezone` (IANA) + `_coerce_timezone` |
| `src/voxtera/call_center/hotel_time.py` | hotel-local "now" + the time anchor injected into the render |
| `src/voxtera/call_center/intents.py` | declarative restaurant/spa schemas, `booking_guidance_block`, `booking_recap`, `BOOKING_SLOT_KEYS` |
| `src/voxtera/call_center/booking_extract.py` | the parallel slot-extraction call (slot-drift fix) |
| `src/voxtera/call_center/prompts/booking_slot_extractor.md` | extractor prompt |
| `src/voxtera/call_center/property_render.py` | spoken render + `create_ticket`; injects guidance + anchor + recap |
| `src/voxtera/call_center/pipeline.py` | `_run_property_fast`: gathers render ∥ extract, persists slots, clears on ticket |
| `src/voxtera/call_center/session.py` | `session["booking_slots"]` |

Tests: `tests/call_center/test_phase4_booking.py`, `test_phase5_grounding.py`,
`test_phase6_verification.py`.
