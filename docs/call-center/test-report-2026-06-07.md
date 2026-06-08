# Call-Center Concierge — Test Report

**Date:** 2026-06-07 · **Tester:** Claude (browser automation, Chrome) · **Target:** `http://localhost:8080/travel-agent.html` → `POST /api/concierge`
**Model:** claude-haiku-4-5 (decomposer) · **Plan:** [test-scenarios.md](test-scenarios.md) · **Log:** `logs/travel_agent_consierge-2026-06-07.jsonl` (109 records)

## Summary

| # | Scenario | Result |
|---|----------|--------|
| 1 | Compound multi-requirement search | ⚠️ PARTIAL |
| 2 | Triage clarification → multi-turn | ⚠️ PARTIAL |
| 3 | Ambiguous name resolution (Casa Dell Arte) | ✅ PASS |
| 4 | Scoped hotel facts + pronoun resolution | ✅ PASS |
| 5 | Destination knowledge | ✅ PASS (note) |
| 6 | Live web search (weather) | ✅ PASS |
| 7 | Hybrid — hotel gap + nearby operator | ✅ PASS (1 defect) |
| 8 | Escalation (booking, live complaint) | ✅ PASS |
| 9 | Conversational recall | ✅ PASS |
| 10 | Multilingual + no-match fail-closed | ⚠️ PARTIAL |
| — | Feedback thumbs + JSONL logging | ✅ PASS |

**7 pass, 3 partial, 0 fail.** Grounding/honesty is consistently good — the bot never invented an amenity or a hotel in 10 scenarios. The partials cluster around two root causes: compound retrieval not running per-requirement, and weak partial matches being rendered as result cards.

## Scenario detail

**1. Compound search** (Antalya, "quiet spa hotel with kids' club and sea view"). Decomposer extracted all 4 requirements with `requirements_logic: AND` but classified `query_type: broad` (id 2), not `compound`. A budget clarification interrupted. After answering, retrieval ran ONE merged Qdrant query (`"quiet atmosphere, spa, kids club, sea view, antalya"`, path `broad_broad`) — 5 hotels returned with only an `antalya` evidence tag, no per-requirement passages. Answer honestly admitted it couldn't confirm the amenities. The page's core promise ("exact passages from each hotel's own guide as evidence") was not delivered. → D1, D2.

**2. Clarification flow** ("I need a hotel", All regions). Clarification fired and the session correctly merged turn 2 ("Bodrum, with a private beach") → 5 Bodrum hotels, honest that none shows a confirmed private beach. Defects: with zero context it asked *budget* first instead of the triage *geography* slot, and the canned text "I found several great options" claims results before any search ran. → D2, D3.

**3. Name resolution.** "Tell me about Casa Dell Arte" → ES `auto_resolve (parafly_17955)` in two independent fresh sessions; grounded 12-room/Torba/Bodrum answer both times. Deterministic. ✅

**4. Scoped + pronouns.** "Does **it** have an indoor pool?" / "what time is breakfast **there**?" both resolved to the session hotel. The inline detector found a near-tie (0.820 vs 0.819) and the strong-score gate correctly **rejected** it — the same-name fix works in production. KB-missing facts were admitted, not guessed. Minor: offered to "call the property directly", an action the bot can't perform. → D6.

**5. Destination.** Istanbul sights + mosque dress code → `destination` (id 15). Accurate answer (Blue Mosque, Hagia Sophia, Topkapi; modest dress). Note: answered via Tavily (`destination_level` → web), not a destination KB — works, but means every destination question pays a ~3 s web call.

**6. Web.** Bodrum forecast → `web` (id 18), reason `time_sensitive`, self-contained query, 27–29 °C answer with AccuWeather/Thomas Cook sources rendered in the 🌐 card. ✅

**7. Hybrid.** "Does the Golden Rose Otel have a dive center? If not, nearby?" → `hybrid` (id 23), reason `local_operator_with_hotel`. Web query named the actual hotel + town (no dangling pronouns); answer kept on-site ("doesn't operate its own") vs nearby (Dragoman, SubAQUA, Kas Diving) accurate. Defect found in the setup turn: "family all-inclusive" search returned **Perge Hotels Adult Only +18**. → D4.

**8. Escalation.** "I want to book a room…" → `escalating (booking)` banner; "I'm at the hotel and my room is not ready" → `escalating (live_complaint)`. No retrieval, immediate handoff line. ✅

**9. Conversational.** "Thanks!" and "What hotels did you recommend so far?" → `conversational`, Qdrant/ES/web all unused, recall matched the actual prior recommendations with context ("you were originally looking for all-inclusive in Antalya"). Minor: said "three" while 5 cards had been rendered. → D7.

**10. Multilingual + no-match.** Turkish utterance → `language: tr`, clarification and full answer in Turkish across turns; honest that Paris had no kids-club/waterslide matches and proposed Turkey alternatives. "Ski slope + ice rink" → prose correctly failed closed ("not… anywhere in our current system"). Defects: the dashed no-match card never appeared — 5 irrelevant hotel cards (incl. Perge Adult Only again) rendered under both no-match answers, visually contradicting the spoken answer. → D4, D5.

**Feedback feature.** 👎 + comment and 👍 + Skip both returned HTTP 200; form opens/closes correctly, thumb locks in, "Thanks for the feedback" shown. Two `"type": "feedback"` records landed in the same daily JSONL, correctly carrying `session_id`, `utterance`, `answer`, `rating`, `comment`. ✅

## Defects (prioritized)

| ID | Sev | Description | Suggested fix area |
|----|-----|-------------|--------------------|
| D1 | High | Multi-requirement queries run as ONE merged vector query (`broad_broad`) — no per-requirement evidence; `compound` path effectively unused for natural phrasing | decomposer prompt (broad vs compound boundary) or route AND-logic broad queries through `CompoundAndDiscovery` |
| D2 | High | Budget clarification is over-eager: fired in 4/10 scenarios, even with zero context (before geography) and after explicit "no budget" | triage slot ordering + suppress budget slot when user opts out |
| D3 | Med | Clarification text "I found several great options" claims results before retrieval ran | clarification template |
| D4 | High | Adults-only hotel (Perge +18) surfaced twice in family/child-friendly searches | hard filter on traveller_type vs hotel attributes at retrieval |
| D5 | Med | No-match turns still render 5 weak-partial hotel cards; `_no_match_answer` / `.no-hotels` card unreachable in practice | score threshold before returning hotels, or suppress cards when render admits no match |
| D6 | Low | Bot offers actions it can't perform ("call the property directly") | concierge_render prompt rule (already exists in concierge_converse) |
| D7 | Low | Prose says "three" hotels while 5 cards rendered | render prompt: enumerate what retrieval returned |

## Re-test after fixes (same day)

Fixes applied: degradation capped at half the requirements (`compound.py`), narrowing skipped when ≥2 requirements given + geography asked before budget + honest clarification text (`pipeline.py`), adults-only name filter on family/child queries (`pipeline.py`), partial-match banner (`travel-agent.html`). Full suite: **190 passed, 3 skipped**.

| Scenario | Before | After |
|----------|--------|-------|
| 1 — compound search | budget interjection; partials with only `antalya` evidence | straight to results; capped degradation → semantic fallback; honest region-mismatch answer (D1, D2 fixed) |
| 2 — "I need a hotel" | "I found several great options — budget range?" | "Happy to help — which destination are you thinking of?" (D2, D3 fixed) |
| 7 setup — family all-inclusive | Perge Adult Only +18 in results | no adults-only properties (D4 fixed) |
| 10 — ski slope + ice rink | 5 irrelevant cards under a no-match answer | zero cards, dashed no-match card, deterministic fail-closed reply (D5 fixed) |

D6 and D7 fixed in a second pass (prompt-level, hot-reloaded): a CAPABILITY BOUNDARY block in `concierge_persona.md` (can answer from the guide and check online — never offer to call/email/book) and a COUNT CONSISTENCY rule in `concierge_render.md`. Re-verified in browser: the missing-breakfast-time turn now offers only "check online for that detail", and a 2-result family search said "I found two properties" naming both. **All 7 defects closed.**

Also noted: turn 2 of scenario 2 routed to live web rather than hotel KB on re-test — routing is decomposer-dependent; answer was correctly labeled and sourced.

## Test environment notes

Latencies were healthy: clarification turns ~3.3 s total, web turns ~3–4 s Tavily + render. The thinking indicator, debug drawer (editable decomposition, Qdrant/ES/web trace, timing chips), region picker, and feedback UI all functioned. Two automation misses (typed input not landing after window resize/navigation) were tester-side, not product defects.
