# Call-Center Concierge — 5 Multi-Turn Dialog Scenarios

Conversation-level test plan for `http://localhost:8080/travel-agent.html`. Unlike [test-scenarios.md](test-scenarios.md) (single-shot paths), each scenario here is a full dialog of 10+ turns run in ONE session (no page reload mid-dialog). What's under test is conversation STATE: slot accumulation across clarifications, pronoun and recall accuracy, region/language switches, corrections, and recovery after escalation.

Global checks every dialog: language of each reply matches the guest's turn; the bot never invents amenities/hotels; recall answers match what was actually said; no offers of impossible actions (calling, emailing, booking); every turn logged in the daily JSONL.

---

## Dialog A — Family vacation planner (slot accumulation → scoped → web → recall)

Region: All regions. Turns:

1. "Hi!" → conversational greeting, no retrieval
2. "We're planning a summer holiday with our two kids" → clarification (geography first, not budget)
3. "Somewhere on the Turkish coast, Antalya maybe" → search or follow-up using accumulated family context
4. "The kids are 4 and 9" → slot absorbed; results or refined search
5. "It must have a water slide and a kids club" → compound search, family context kept, NO adults-only hotels
6. "Which of those is closest to the beach?" → comparison over the JUST-RETURNED set, no new invented hotels
7. "Does it have babysitting?" → pronoun → single hotel; admit if not in guide, offer online check only
8. "What's the weather there in August?" → web; "there" resolves to the discussed region
9. "Are there any water parks nearby?" → hybrid; web query names the actual place, not "there"
10. "What have we settled on so far?" → conversational recall: region, kids' ages, requirements, hotels named
11. "Great, thanks!" → short close, no fake actions

## Dialog B — Romantic getaway with a change of mind (comparison → region switch → cross-region recall)

Region: All regions. Turns:

1. "I want to surprise my wife with a romantic getaway" → geography clarification
2. "Bodrum" → search with romantic context
3. "Adults-only with a spa, as quiet as possible" → refined compound search
4. "Can you compare the top two?" → comparison grounded in evidence for exactly those two
5. "Which one has better dining?" → evidence-grounded or honest "not in guide"
6. "Actually, what about Istanbul instead?" → region SWITCH; previous Bodrum context must not leak into new results
7. "A boutique hotel near the old town" → Istanbul search
8. "How far is that one from the airport?" → scoped; likely missing → online-check offer only
9. "What restaurants are near it?" → hybrid; web query anchored to the named hotel
10. "Remind me which Bodrum hotels you suggested before we switched" → cross-region recall, exact names
11. "Thanks, I'll think it over" → close

## Dialog C — Business traveler with mid-dialog escalation and recovery

Region: Istanbul. Turns:

1. "I need a hotel in Istanbul for a business trip next month" → search or one clarification max
2. "Mid-range, with conference rooms and an airport shuttle" → compound search
3. "Does the first one have fast wifi?" → ordinal reference ("the first one") resolves correctly
4. "What time is check-in?" → scoped fact
5. "Is there a gym?" → scoped fact; honest if missing
6. "Perfect, book it for me right now" → ESCALATION (booking) banner
7. "While I wait — is Topkapi Palace open on Mondays?" → conversation continues after escalation; web path
8. "How would I get there from the hotel?" → hybrid; "the hotel" = the discussed one, "there" = Topkapi
9. "Summarize everything about the hotel we discussed" → recall: name, confirmed facts only
10. "That's all, goodbye" → close

## Dialog D — Multilingual guest (tr → en → es switches, cross-language recall)

Region: Bodrum. Turns:

1. (tr) "Merhaba, Bodrum'da denize sıfır bir otel arıyorum" → reply in Turkish
2. (tr) "Bütçe önemli değil, ama sakin olsun" → Turkish; no repeat budget question
3. (tr) "Havuzu var mı?" → Turkish scoped follow-up on a recommended hotel
4. (en) "Let's switch to English — does it have a restaurant?" → English reply, SAME hotel context
5. (en) "What's the best beach near it?" → web/hybrid in English
6. (es) "¿Tienen habitaciones familiares?" → Spanish reply, context kept
7. (en) "What did I ask you at the very beginning, in Turkish?" → cross-language recall
8. (en) "So which hotel do you recommend overall?" → single grounded recommendation from the discussed set
9. (en) "Is it suitable for an elderly guest, any stairs?" → honest if not in guide
10. (en) "Teşekkürler, goodbye!" → graceful close in either language

## Dialog E — Tricky guest (impossible asks, false corrections, urgency, recovery)

Region: Paris → switched mid-dialog. Turns:

1. "I want a hotel in Paris with a water park" → honest no-match/region mismatch, no invented hotels
2. "Fine, anywhere in Turkey then" → region change honored
3. "With a water park and all-inclusive" → compound search
4. "I never said all-inclusive, why did you add that?" → FALSE accusation: bot should politely point to what was actually said (turn 3), not grovel or fabricate
5. "Whatever. Which of those is cheapest?" → honest if pricing absent from guide
6. "Do they allow pets? We have a dog" → policy lookup, honest if missing
7. "Forget the dog, he's staying home" → retraction absorbed; pets must not constrain later turns
8. "Is the pool heated?" → detail likely missing → online-check offer only
9. "I land in 2 hours and I have no hotel!!" → URGENCY escalation
10. "Relax, just kidding. Summarize my options" → post-escalation recovery + accurate recall
11. "Bye" → close

---

## Pass criteria (dialog level)

A dialog PASSES when: every turn answered in the right language and path, context survived all switches/corrections, recall turns contained no fabricated history, escalations fired exactly on turns 6/9 (C/E) and nowhere else, and no reply offered an impossible action. Per-turn defects are logged individually in the run report.
