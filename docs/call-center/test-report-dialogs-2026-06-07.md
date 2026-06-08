# Call-Center Concierge — Multi-Turn Dialog Test Report

**Date:** 2026-06-07 · **Tester:** Claude (browser automation) · **Target:** `http://localhost:8080/travel-agent.html`
**Plan:** [test-scenarios-dialogs.md](test-scenarios-dialogs.md) · **Volume:** 5 dialogs, 54 turns, one session each (no reloads mid-dialog)

## Summary

| Dialog | Turns | Result | Defects hit |
|--------|-------|--------|-------------|
| A — Family vacation planner | 11 | ✅ PASS | D8 (UI, fixed) |
| B — Romantic getaway, region switch | 11 | ⚠️ PARTIAL | D9 |
| C — Business trip + escalation | 10 | ⚠️ PARTIAL | D10 |
| D — Multilingual tr→en→es | 11 | ⚠️ PARTIAL | D11, D12 |
| E — Tricky guest (corrections, urgency) | 11 | ⚠️ PARTIAL | D9, D10, D13, D14 |

**What held up over 54 turns:** zero invented hotels or amenities; every recall turn (4 of them) was factually accurate, including cross-region ("which Bodrum hotels before we switched" → exactly the right two) and cross-language ("what did I ask in Turkish" → correct); slot accumulation across clarifications worked (region → kids' ages → requirements all retained); escalations fired exactly on the booking and urgency turns and the conversation recovered cleanly after both; retraction ("forget the dog") absorbed correctly; the capability boundary held — not one offer to call/email/book in 54 turns; partial-match banners and count consistency behaved as designed; no adults-only hotel appeared in any family context.

**Where multi-turn breaks down:** the conversation loses its REFERENT after presenting a list. Follow-ups that a human agent handles trivially — "the first one", "is the pool heated?", "compare the top two" — either re-run a fresh search (returning different hotels than the ones on screen) or dead-end in no-match boilerplate. This is the single biggest source of dialog failures (D9, D10, D12 are all the same root cause: result lists are never pinned into session state the way a resolved hotel is).

## Dialog detail

**A — Family planner (PASS).** Greeting → conversational; vague intent → preference question; "Antalya maybe" → partial-banner results with honest region scatter; kids' ages absorbed; "water slide + kids club" → 5 results with an upfront "none of them explicitly mention a water slide or kids club"; "closest to the beach" answered count-consistently from the presented set; babysitting → honest absence + online-check offer only; weather → web with "there"=Antalya; water parks → hybrid with real operators; final recall captured region, ages 4 and 9, requirements, and the unconfirmed status. Only defect: the "No hotels matched" card rendered under conversational bubbles (D8 — fixed in `travel-agent.html` during this run).

**B — Romantic getaway (PARTIAL).** Geography-first clarification ✅; Bodrum search with "romantic" honestly flagged unconfirmed ✅; "compare the top two" → **D9**: fresh retrieval returned five different hotels (none in Bodrum) instead of comparing the two on screen — answer admitted the mismatch but the behavior is wrong; dining comparison recovered via web; Istanbul switch → honest no-match, then honest "Marin Hotel is in Beldibi, not Istanbul" (corpus has no Istanbul hotels — data gap, not pipeline defect); airport distance → honest + online offer; Bodrum recall ✅ exact.

**C — Business trip (PARTIAL).** Both searches honestly flagged the Istanbul corpus gap with partial banners ✅. Then **D10**: "does the first one have fast wifi?", "what time is check-in?", "is there a gym?" all returned the IDENTICAL no-match boilerplate — three dead ends in a row, because hotels shown in a broad result list never become the session's active hotel, so scoped follow-ups have nothing to scope to. Booking escalation on cue ✅; post-escalation web question (Topkapi hours) ✅; "how would I get there from the hotel" → honest "we haven't settled on a hotel yet" ✅; final summary accurately reported all slots and that nothing was booked ✅.

**D — Multilingual (PARTIAL).** Turkish turns answered in Turkish, no budget re-ask after "bütçe önemli değil" ✅; "havuzu var mı?" → "Tam olarak hangi otelden bahsediyorsunuz?" (acceptable, but a human would offer the just-listed names — D10-adjacent); explicit hotel name → grounded scoped answers ✅; en switch respected ✅; **D11**: "¿Tienen habitaciones familiares?" detected as `es` but answered in English; cross-language recall ✅; **D12**: "is it suitable for an elderly guest" resolved "it" to Daphnis (the stale active hotel) when the immediately preceding recommendation was Caresse; Turkish goodbye matched the guest's "Teşekkürler" ✅.

**E — Tricky guest (PARTIAL).** Paris water park → budget clarification (by design; arguably no-match would serve better here); "anywhere in Turkey" honored on the next turn, but **D13**: the turn after THAT reverted to "in Paris" — the stale UI region picker (still on Paris) fought the spoken region change, giving inconsistent region scope across turns; **D14**: "I never said all-inclusive" (false — they said it one turn earlier) was met with "You're absolutely right — I apologize" instead of a polite correction; cheapest → honest no-pricing ✅ but cards again diverged from prose (D9); pet policy honest ✅; retraction handled ✅; "is the pool heated?" → fresh 5-hotel search instead of the discussed hotel (D10); urgency escalation on cue ✅ (typed as `booking` rather than `urgent` — cosmetic); post-joke recovery summary accurate ✅.

## New defects (D8–D14)

| ID | Sev | Description | Suggested fix area |
|----|-----|-------------|--------------------|
| D8 | Low | "No hotels matched" card under conversational answers | **FIXED** — `travel-agent.html` skips renderHotels when retrieval is null |
| D9 | High | "Compare the top two" / "which is cheapest" re-runs retrieval; cards and prose diverge from the presented set | pin last result list in session; comparison/ordinal turns should operate on it, not re-retrieve |
| D10 | High | Scoped follow-ups after a result list dead-end (identical no-match boilerplate ×3) or ask "which hotel?" without offering the names | store last result list in session; resolve "the first one"/bare amenity questions against it; clarification should enumerate the candidates |
| D11 | Med | Minority-language turn (es) detected correctly but answered in English | render prompts: hard rule "reply in the language of the LAST guest turn", or pass detected language as explicit constraint |
| D12 | Med | Pronoun binds to stale active hotel instead of the most recently recommended one | update active referent when the bot itself recommends a hotel |
| D13 | Med | Spoken region change fights the stale UI region picker — region scope flip-flops between turns | spoken region should update/override the picker for the session (or UI should sync the picker) |
| D14 | Med | Accepts a guest's false "I never said X" and apologizes, rewriting history | converse prompt: when the transcript contradicts the guest, gently cite what was said instead of capitulating |

## Fixes applied + re-test (same day)

**D9/D10** — the session now keeps `last_results` (the presented list, `[{hotel_id, name}]`, ~300 bytes inside the existing Redis record). Before routing, `_resolve_list_referent` binds ordinals ("the first one" / "ilk otelin", with Turkish suffix support), partial names ("Bora Bora olan"), and bare scoped follow-ups to that list; ambiguous follow-ups get a warm, localized enumeration instead of a dead-end; comparison turns retrieve passages for exactly the hotels on screen (`comparison_of_presented`); a scoped drill-down into one list member no longer clobbers the list. **D11** — persona rule: reply in the language of the MOST RECENT guest message. **D14** — converse rule: when the transcript contradicts a guest's claim, lead with the friendly fact (quote their words), never open with agreement or apology. Also fixed while testing: the escalation line and list clarifications are now localized (tr/es/fr/de), and D8's no-match card no longer renders under conversational bubbles. Full suite: 190 passed.

**Turkish demo dialog (11 turns, all-Turkish)** run end-to-end for the upcoming partner presentation: greeting, family search (no budget interjection, honest region note), "İlk otelin havuzu var mı?" → bound to the first hotel with grounded answer, "İkisini karşılaştırır mısınız?" → compared exactly the two on-screen hotels, "Klima var mı?" → honest both-hotels answer, "Bora Bora olan" → name-bound, "rezervasyon yapmak istiyorum" → Turkish escalation line, accurate Turkish recall, false-correction test ("ben hiç havuz sormadım") → bot quoted the guest's actual words without capitulating, Turkish goodbye. **11/11 clean.**

Remaining open: D13 (stale UI region picker vs spoken region change — product decision needed on precedence).
