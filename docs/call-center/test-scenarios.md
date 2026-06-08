# Call-Center Concierge — 10 Test Scenarios

Manual test plan for the travel-agent concierge (`demo-hotel/travel-agent.html` → `POST /api/concierge` → `ConciergePipeline`). Each scenario lists the turns to type, the expected pipeline behaviour, and what to verify in the debug drawer (`Debug · decomposition + timings`) and the daily log (`logs/travel_agent_consierge-YYYY-MM-DD.jsonl`).

General checks for every scenario: the answer never invents amenities or geography; timings chips appear; a log record is written; the 👍/👎 feedback row appears under the answer and a submitted rating produces a `"type": "feedback"` record in the same log file.

---

## 1. Compound multi-requirement search (happy path)

**Region:** Antalya
**Turn:** "A quiet spa hotel with a kids' club and sea view"

**Expect:** `query_type: compound`, hotel cards with one evidence passage per requirement (spa, kids' club, sea view), every listed hotel matching ALL requirements. Debug: Qdrant used ×N (one per requirement), reranked.

## 2. Triage clarification → multi-turn continuation

**Region:** All regions
**Turn 1:** "I need a hotel"
**Turn 2 (after the bot asks):** answer the clarification, e.g. "Bodrum, with a private beach"

**Expect:** Turn 1 returns `clarification` (geography or hotel_or_recommend slot) with no hotel cards. Turn 2 reuses the same `session_id` and completes the search with the merged context. Log: turn 1 record has `clarification` set, `answer` null.

## 3. Hotel name resolution — ambiguous same-name hotels

**Region:** All regions
**Turn:** "Tell me about Casa Dell Arte"

**Expect:** ES resolver runs on the full utterance; the strong-score gate (0.82, rerank=False) picks one hotel deterministically. Debug: ELASTICSEARCH (resolver) shows the query, decision, and resolved `hotel_id`. Repeat the query — same hotel resolved both times.

## 4. Scoped hotel question (single hotel, single fact)

**Region:** Istanbul
**Turn 1:** "Does the <hotel from scenario 3> have an indoor pool?"
**Turn 2:** "And what time is breakfast there?"

**Expect:** `query_type: scoped` with `hotel_mention` set. Turn 2's "there" resolves to the same hotel from session history. If the KB lacks the fact, the bot admits it / offers to check online — it must NOT guess.

## 5. Destination knowledge (no hotel)

**Region:** Istanbul
**Turn:** "What are the main sights in Istanbul and what should I wear when visiting mosques?"

**Expect:** `query_type: destination` (ids 14/15), no hotel cards, no web call needed for stable facts. Answer grounded, concise, no invented landmarks.

## 6. Live web search (real-time info)

**Region:** Bodrum
**Turn:** "What's the weather forecast for Bodrum next week?"

**Expect:** `query_type: web` (id 18), Tavily used. UI shows the "🌐 Live web search" card with query + up to 3 sources. Debug: `WEB (Tavily): USED`, query sent is self-contained (no dangling pronouns). Log record has the `web` block (query, answer, source URLs, elapsed_ms).

## 7. Hybrid — hotel gap + nearby operator

**Region:** Antalya
**Turn 1:** "Find me a family all-inclusive hotel in Antalya"
**Turn 2:** "Does it have a dive center? If not, is there one nearby?"

**Expect:** Turn 2 is `query_type: hybrid` (id 23): hotel KB answers the on-site part, web answers the nearby part, and the synthesis keeps the on-site vs arranged-nearby distinction accurate. The web query must name the actual hotel, not "it".

## 8. Escalation — booking intent

**Region:** Rome
**Turn:** "I want to book a room for next weekend, two adults"

**Expect:** `query_type: escalate` (id 24): fast-gate stem triggers, LLM judge confirms, UI shows the "Concierge · escalating (…)" banner, no hotel retrieval. Variant: "I'm at the hotel and my room is not ready" → escalation_type complaint (id 26). Log: `escalation.escalate: true`.

## 9. Conversational memory — recall and chitchat

**Region:** any (after scenarios 1–7 in the same session)
**Turn 1:** "Thanks!"
**Turn 2:** "What hotels did you recommend so far?"

**Expect:** both `query_type: conversational` (id 28), `_handle_converse` answers from the Redis transcript with NO retrieval (debug: Qdrant/ES/web all "not used"). Turn 2 lists only hotels actually mentioned earlier. The bot never promises actions it can't do (booking, sending emails).

## 10. Multilingual + no-match fail-closed

**Region:** Paris
**Turn 1 (Turkish):** "Çocuk dostu, su kaydıraklı bir otel arıyorum"
**Turn 2:** "A hotel with a ski slope and an ice rink"

**Expect:** Turn 1: `language: tr`, answer (or clarification) entirely in Turkish. Turn 2: zero matches → the fail-closed `_no_match_answer` ("No hotels matched every requirement…") with the dashed no-hotels card; no invented hotels and no wrong-region claims. With Region switched to "All regions", the no-match message must respect the override.

---

## Logging & feedback verification (run once after the scenarios)

1. `tail logs/travel_agent_consierge-$(date -u +%F).jsonl` — one record per turn with `ts, session_id, utterance, answer, decomposition, trace, retrieval_summary, timings`.
2. Press 👍 on a good answer with a comment, 👎 + Skip on another. Confirm two `"type": "feedback"` records whose `session_id`/`utterance`/`answer` match the rated turns.
3. Confirm dialog records have no `type` field (the distinguishing marker).
