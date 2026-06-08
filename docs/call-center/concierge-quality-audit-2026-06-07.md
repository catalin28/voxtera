# Concierge Answer-Quality Audit — Palace Standard

**Date:** 2026-06-07 · **Benchmark:** the concierge desk of Çırağan Palace Kempinski — what would a guest there accept?
**Scope:** answer voice/polish only (correctness was covered by the scenario and dialog reports). Corpus: Turkish resorts/hotels.

## Rubric

A palace concierge answer must: (1) never reveal machinery — no searches, systems, guides, results, scores; (2) be precise about unknowns without announcing honesty; (3) recommend with conviction and frame trade-offs as guidance; (4) anticipate one next need; (5) stay concise for voice; (6) hold all of this in EVERY language; (7) never oversell, never disparage; (8) offer only what it can actually do.

## Before — graded from today's 60+ live answers

The agent was grounded and truthful but spoke like a back office, not a concierge:

| Tic | Live examples (verbatim) |
|-----|--------------------------|
| Machinery leakage | "the search has returned five properties…", "the search just returned hotels on the Turkish Riviera…", "the system shows partial matches…", "our guide doesn't spell out…", "I don't see wifi speed or connectivity details in our guide…" |
| Honesty announcements | "I need to be honest with you —", "I have to be honest —", "I need to be straight with you —", "I need to be straightforward with you —" (4 variants in one afternoon) |
| Repetitive closer | "Would you like me to check online…?" ended nearly every unknown-answer |
| Flat recommendations | facts listed side by side; no conviction, no guidance, no anticipation |

## Changes

`concierge_persona.md` rewritten to palace standard: new DISCRETION section (machinery words forbidden, in every language — "rehberimizde", "sistemimizde" etc. explicitly listed; own the gap: "I don't have X to hand — shall I look into it for you?"; honesty tics forbidden — precision IS the honesty; never repeat a disclaimer or offer twice in a row) and new LUXURY CRAFT section (recommend with conviction, frame trade-offs as guidance, anticipate ONE next need, never oversell/disparage). `concierge_render.md` spoken examples purged of "our guide". Web-offer phrasing kept compatible with the `_answer_offers_web` keyword gate (verified: "look into", "bakayım", "araştır" all still arm the flag).

## After — probe results (EN + TR, live)

**Anniversary request:** "How lovely — an anniversary by the sea deserves somewhere truly memorable. I have five coastal properties across Turkey that would suit the occasion, each with its own character…" then an anticipatory narrowing question (intimate vs resort, Aegean vs Mediterranean). No machinery.

**"Which would you personally pick?":** "For an anniversary as meaningful as your tenth, I'd choose Ala Suites and Villas — it's genuinely intimate, built around privacy… that quiet, grown-up atmosphere where the two of you can simply be together." Conviction, guest-centric framing.

**Unknown detail (sommelier):** "I don't have the restaurant's staffing details to hand — whether they have a sommelier on staff is the sort of thing that matters for your evening, so shall I look that up for you?" Textbook.

**Turkish honesty probe ("Dürüst olun"):** "…denize yakınlık ve gizlilik arıyorsanız, evet; ama büyük spa ve gece hayatı istiyorsanız, başka yönler daha uygun olabilir. Sizin için balayıda en değerli şeyler nelerdir?" Honest trade-off, no tics.

**Turkish unknown (first attempt leaked "rehberimde"):** persona patched with language-independent forbidden list; next probe: "Golden Rose Otel'de spa merkezi bulunmuyor maalesef… başka seçenekleri araştırabilirim sizin için." Clean.

## Sophisticated-traveler stress dialog (13 turns, EN)

Persona: three weeks on the coast, remote work + diving, multi-base (Kaş → Urla/Alaçatı → Bodrum), wine, hammam, yoga, itinerary synthesis, and a "email me the itinerary" capability trap.

**Held up beautifully:** destination conviction with real specifics (Kaş dive sites, Teruar and Vino Locale in Urla); per-hotel honesty in the wellness search ("though I can't confirm their spa or yoga offerings from what I have to hand"); pronoun binding across topics (hammam → Saylam); a genuinely anticipatory recovery ("shall I look into Kaş properties with hammam access, so you could stay based there all three weeks rather than moving mid-trip?"); flawless turn-12 synthesis of the whole plan including the open Urla slot; and the email trap declined honestly with the plan recapped instead.

**New defects found and fixed:**

| ID | Sev | Finding | Fix |
|----|-----|---------|-----|
| D15 | High | "Anywhere near Saylam with a proper wine list?" triggered the dietary/accessibility triage question ("halal food?") — tone-deaf non-sequitur; the slot-merge then polluted the next query into a no-match cascade | `triage.py`: non_negotiable slot now fires only for recommendation query types (broad/compound/comparison), never hybrid/web/scoped/destination |
| D16 | High | Vibe-led searches ("design-forward boutique", "food scene", "~€200 Urla") no-matched repeatedly although the corpus has Alaçatı/Urla/Çeşme properties, and the cold no-match string repeated verbatim 3× | `_no_match_answer` rewritten in palace voice (en+tr); the findability half (vibe-term requirement extraction vs chunk vocabulary) is OPEN — see below |
| D17 | Med | Conversational comparison invented physical texture with zero retrieval ("shaded corners away from the main gathering spaces") — audit watch-item 1 confirmed live | converse prompt: NO NEW TEXTURE rule — comparisons from history may only restate previously stated details |
| D18 | Low | Residual machinery vocabulary ("the evidence I have"), "confirm September availability" capability drift, blunt "I don't have an email system" | monitor via 👎 feedback; phrasing-level |

**Open engineering item (D16a):** requirement extraction sends style adjectives ("design-forward", "food scene") that the chunk vocabulary can't match, so legitimate hotels zero out. Candidate fixes: map vibe terms into `vibe_preferences` (not hard requirements) at the decomposer, or relax the degradation cap when ALL dropped requirements are vibe-class. Needs a corpus-side look before choosing.

**PORTFOLIO BOUNDARY (business rule, supersedes the web-rescue experiment):** a web rescue was briefly added so KB no-matches fell through to live web search — it worked, but it recommended hotels the agency does not sell (Payam, Casa Fontana, Four Seasons Bosphorus). Removed same day: hotel recommendations are KB-only; a portfolio miss fails closed with the ease-a-criterion reply. Enforced at three levels: pipeline (no web fallback on hotel searches), `concierge_web_synth.md` (web hotels may inform area knowledge, never lodging suggestions), `concierge_persona.md` (lodging is portfolio-only; naming a web hotel sends the guest to a competitor). The web remains active for restaurants, activities, weather, transport. Consequence: D16a's findability gap matters MORE now — the durable fix is corpus-side (wifi/style/atmosphere attributes in chunks), since the agent may only sell what the corpus can find.

## Watch items (for the Turkish demo rehearsal)

1. **Embellishment risk:** the polished register invites color the evidence may not carry ("suites are spread across the property so you're not hearing other guests"). Spot-check conviction answers against the debug drawer's evidence during rehearsal; thumb-down any invented texture — the grounding rules still apply, but polish raises the temptation.
2. **Absence vs unknown:** "spa merkezi bulunmuyor" asserts the hotel HAS no spa; if the knowledge merely doesn't mention one, the right phrasing is "elimde spa bilgisi yok". Watch for over-assertion of absence.
3. **Capability drift:** one reply offered to "talk through dates and availability" — booking-adjacent. The capability boundary held everywhere else; watch this phrasing.
4. **Turkish naturalness:** occasional clunky constructions ("merak edip etmediğinizi sorabilirim"). Native-speaker pass over a transcript before the demo would be worthwhile.
5. These are prompt-level (probabilistic) behaviors — monitor the 👎 feedback records in `logs/travel_agent_consierge-*.jsonl` after the demo.
