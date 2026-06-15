# Concierge Fix Plan — Execution Order

**Companion to:** `concierge-intent-flows-spec.md`
**Date:** 2026-06-15

Ordered by impact and independence. Phases 1–3 are small, shippable on their own, and
fix the most user-visible bugs. Phase 4 is the larger state-machine refactor that the
remaining complaints depend on. Each phase ends with a verification step.

> **Phase 0 prerequisite:** the real repo (`ChatGPTPProjects/voxtera`) must be mounted.
> File/function names below are from prior context and will be confirmed against the
> live code before editing.

---

## Phase 0 — Ground the plan (before any edit)
- Mount the repo; locate the prompt builder, the LLM-call site, `decompose`/`render`,
  `CallContext`, `HotelConfig`, the STT/turn-aggregation path, and the Gladia config.
- Confirm which branch to work on and current state (uncommitted work per memory).
- Map each spec section to the exact file + function it touches.
**Done when:** a confirmed file/function list exists for every phase below.

---

## Phase 1 — P0 prompt-scaffold leak (ship alone)
*Spec: P0, §6 (one in-flight call). Highest priority — user-visible, fabricates bookings.*
1. Replace the hand-built plain-text prompt with a structured `system` + role-separated
   `messages` array to the Claude Messages API. Stop concatenating `User:`/`Assistant:`
   headers into one string.
2. Add stop sequences (`\nUser:`, `\nAssistant:`, `\nDetected language:`) as a guard.
3. Cap `max_tokens` to a turn-sized budget.
4. Enforce **one in-flight completion per turn**: a second `llm_start` cancels or
   coalesces the first — never two parallel calls. (Also kills the `now.Perfect` concat.)
**Verify:** replay the TR log / a silence+fragment turn; confirm no scaffold text, no
fake turns, no fabricated reservation, single reply.

---

## Phase 2 — Language lock (ship alone)
*Spec: §4. Small, high impact.*
1. On first user utterance, detect and set `CallContext.locked_language`.
2. Render + TTS in `locked_language` only; ignore Gladia per-utterance language after lock.
3. Switch only on explicit user request, with confirmation.
4. Pin `GLADIA_LANGUAGES` to the expected set (avoid full auto-detect).
**Verify:** "Allo?" / short interjections mid-call do not flip language; explicit
"switch to English" does.

---

## Phase 3 — STT hygiene + response length (ship together)
*Spec: §8, §9.*
1. Drop silence-hallucination patterns ("Altyazı …", subtitle credits) before they
   become a turn; require VAD-confirmed speech / min confidence before `llm_start`.
2. Confirm venue proper-noun bias + fuzzy resolver (`entity_resolver.py`) is active.
3. Enforce ~200-char reply cap (prompt instruction + `max_tokens`); one ask per turn.
**Verify:** silence produces no turn; "Bosphorus Grill" resolves; replies ≤ ~200 chars;
fewer barge-ins in a replay.

---

## Phase 4 — Intent / stage state machine (the refactor)
*Spec: §1, §2, §3, §7. Everything below depends on this.*
1. Add state to `CallContext`: `active_intent`, `stage`, `slots`, `guest_type`.
2. Declarative intent schemas (restaurant, spa): qualifying `guest_type`, mandatory
   slots, conditional contact (in_house→room_number, external→phone|email), optional slots.
3. Stage machine `EXPLORING → COLLECTING → CONFIRMING → SUBMITTED`; intent ≠ commitment.
   `decompose` classifies intent + commitment; `render` is constrained by stage.
4. `guest_type` asked first; never ask external visitors for a room number.
5. Collect one mandatory slot per turn; optional only after mandatory complete.
6. Add `HotelConfig.timezone` (IANA); resolve relative dates against hotel-local now to
   absolute; echo absolute date in the CONFIRMING read-back.
7. `SUBMITTED` emits exactly one closing line; no transcript playback.
**Verify:** EN script — "table for two tomorrow 7pm" books tomorrow, asks guest_type,
collects name+phone, no premature collection, no room-number for external, single close.

---

## Phase 5 — Grounding / anti-fabrication
*Spec: §5, §10.*
1. No-knowledge fallback: no RAG hit + no bound tool → "I don't have that information,"
   never offer an unwired capability, never go silent.
2. Venue/menu/dress-code/awards answers must come from RAG; no unsourced superlatives.
3. Canonical restaurant list per tenant; de-dupe name variants (Ruya/Rüya).
**Verify:** Michelin / dress-code questions either answer from RAG or say "I don't have
that"; no invented menu items; one consistent restaurant list.

---

## Phase 6 — End-to-end verification
1. Build/extend a replay harness from the EN + TR logs (reuse the Turkish test kit).
2. Run both scripts end-to-end; confirm every complaint from both testers is resolved.
3. Spot-check latency and leakage-guard frame counts vs. baseline.
**Done when:** both tester scenarios pass clean on replay.

---

## Suggested shipping order
`Phase 1 → Phase 2 → Phase 3` (quick wins, independently deployable) → `Phase 4`
(refactor) → `Phase 5` → `Phase 6`. Phases 1–3 can deploy before 4 is finished.
