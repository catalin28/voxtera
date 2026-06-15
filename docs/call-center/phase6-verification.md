# Phase 6 — End-to-End Verification

**Companion to:** [`concierge-fix-plan.md`](../fix-plan/concierge-fix-plan.md),
[`concierge-intent-flows-spec.md`](../fix-plan/concierge-intent-flows-spec.md)
**Date:** 2026-06-15
**Source complaints:** two tester sessions (EN + TR), live trace
`wacall_64e386fd266a`, the TR-log second-LLM analysis.

---

## 1. What "verified" means here

Two kinds of confirmation, kept honest and separate:

- **Offline (mechanism)** — a unit/integration test proves the *fix is wired*:
  the guard is sent, the prompt rule is present, the state transition happens.
  Runs in CI, no keys/services. This is most of the table below.
- **Live (behaviour)** — only a real audio replay against the running stack can
  prove the **LLM obeys** a prompt rule, plus latency and leakage-guard frame
  counts. The booking design is **prompt-guided** (the LLM owns the flow — see
  the project decision and Phase 4), so several complaints can only be
  *behaviourally* confirmed live. Those are marked **LIVE** and listed in §3.

The Phase 4–5 booking flow has **no Python state machine**: Python supplies the
hotel-local clock and injects the collection rules; the model decides intent,
collects slots, and files via `create_ticket`. So the offline tests pin the
*scaffolding the model is given*, not the model's compliance.

---

## 2. Complaint → fix → test matrix

| # | Tester complaint | Phase | Fix | Offline test (mechanism) | Live? |
|---|---|---|---|---|---|
| P0 | Prompt scaffold leaked into TTS; **fabricated a reservation** | 1 | structured `messages` (no `User:`/`Assistant:` text scaffold); `stop_sequences`; turn-sized `max_tokens`; one in-flight call | `test_phase6_verification.py::test_render_applies_stop_sequences_and_turn_sized_max_tokens`, `::test_no_actions_render_still_guarded`; `tests/test_smoke.py` (message turns) | — |
| C5 | Switches language mid-call without being asked | 2 | `maybe_lock_language` after first substantive utterance; explicit unlock only | `tests/call_center/test_session.py` (lock/unlock/decide) | LIVE (STT) |
| — | Silence/STT hallucinations ("Altyazı…", subtitle credits) became turns | 3 | `TranscriptionNoiseFilter` drops the hallucination families | `tests/test_audio_hallucinations.py` | LIVE (audio) |
| C7 | Replays the whole conversation back at the end | 1, 3 | scaffold fix + ~200-char voice cap, one ask per turn | brief prompt rules; `max_tokens=140` pinned above | LIVE |
| C1 | Collects reservation details **before** the guest commits | 4 | booking guidance is conditional: "when (and only when) the guest wants to book" | `test_phase4_booking.py::test_guidance_block_*` (rule present) | **LIVE** |
| C2 | Misreads relative dates ("tomorrow" → "today") | 4 | `HotelConfig.timezone` + `hotel_time_note` injects the hotel-local now; rule to read back the **absolute** date | `test_phase4_booking.py` (anchor + "absolute"); `test_phase6_verification.py` (anchor reaches model via pipeline, real tz) | **LIVE** |
| C3 | Tries to submit with no name / contact | 4 | mandatory-slot list in the guidance + `create_ticket` confirmation rule | `test_phase4_booking.py::test_guidance_block_covers_both_intents_from_schemas` | **LIVE** |
| C4 | Treats room number as mandatory; never asks external for phone | 4 | guest_type-first; in-house→room, external→phone/email; "NEVER ask an external visitor for a room number" | `test_phase4_booking.py` + `test_phase6_verification.py` (rule in system prompt) | **LIVE** |
| C6 | Offers to "look up" something it can't, then goes silent | 5 | persona capability boundary now **defers to a bound action tool**; otherwise no-actions + offer-online; "own the gap" | `test_phase6_verification.py::test_no_actions_render_still_guarded` (no-actions path); persona/render rules | **LIVE** |
| — | Unsourced awards / Michelin / dress code / superlatives | 5 | explicit "from evidence only, else 'I don't have that'" rule in render prompts | render prompt rules (Kempinski Michelin facts curated in its config) | **LIVE** |
| — | Inconsistent restaurant names (Ruya/Rüya) | 5 | canonical venue list per tenant; fold-dedupe | `tests/call_center/test_phase5_grounding.py` | — |
| — | Whole booking runs through the real pipeline | 4 | guidance + anchor + tool loop wired through `ConciergePipeline.run` | `test_phase6_verification.py::test_booking_flow_end_to_end_through_pipeline` | **LIVE** |

Offline suite: **637 passing** (`pytest -q`), ruff clean.

---

## 3. Manual live-replay checklist (needs the running stack)

These require API keys + Qdrant/ES/Redis and audio I/O (web widget via
BlackHole, or the PSTN bridge). Reuse the
[`docs/turkish-test-kit/`](../turkish-test-kit/turkish-test-kit.md) clips for TR
and an equivalent EN script. Confirm, per the matrix's **LIVE** rows:

1. **P0 / C7** — replay the TR dress-code turn (~283 s in the original call):
   no scaffold text, no fake `User:`/`Assistant:` turns, no fabricated
   reservation, a single ≤~200-char spoken reply.
2. **C5** — say "Allo?" / short interjections mid-call: language does **not**
   flip; "switch to English" **does**.
3. **STT** — feed silence / room noise: no turn is created; "Bosphorus Grill"
   and "Tuğra" resolve.
4. **C1–C4 (booking)** — EN: "a table for two tomorrow at 7pm" →
   books **tomorrow** (absolute date read back, hotel-local), asks guest_type
   first, collects name + contact, no premature collection, **no room number
   for an external visitor**, one closing line, no transcript playback.
5. **C6 / awards** — ask Michelin-star / dress-code questions: answers from RAG
   or says "I don't have that"; never invents; never offers an unwired action
   then goes silent.
6. **Spot-checks** — turn latency and `leakage_guard` / `suppressor` dropped-frame
   counts vs. the pre-fix baseline (trace dashboard / `[lg-state]` logs).

**Done when:** both tester scenarios replay clean and every **LIVE** row above
is observed to hold.
