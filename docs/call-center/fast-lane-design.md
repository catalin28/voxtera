# Fast-lane / slow-lane intake — voice-dialog latency design

**Status:** proposal (design only — not yet implemented).
**Goal:** make the concierge feel like a real travel agent on a phone call —
snappy back-and-forth — without removing the structured understanding that
makes it more than a search box.

---

## 1. The problem, framed for voice

On a phone call humans expect sub-second turn-taking. Today every turn waits on
the full decomposition before the bot can do anything:

```
classify ‖ decompose(~2.0s)  →  triage  →  route  →  retrieve(~0.3s)  →  render(~1.5s)
```

That's fine in a text demo (a 2s pause is invisible) but on a call it's dead
air. And we proved the 2s is a **floor**: it survived a model swap (Haiku↔nano),
connection reuse, and token trimming. It is not going away by tuning the call.

The key observation: **most dialog turns don't need retrieval at all.** A travel
agent spends half the call *asking* — "whereabouts?", "for how many?", "any
dates?" Those turns only need a quick *decision*, not a 27-field extraction or a
vector search. We're paying 2s for turns that should cost 200ms.

## 2. The idea: two lanes

Split the brain into a **fast intake lane** that handles conversational turns,
and a **slow retrieval lane** that only runs when we're actually going to search
the KB.

```
                       ┌─────────────────────────── FAST LANE (~0.8s) ───────────────────────────┐
utterance ─► INTAKE ──►│ escalate?            → hand off to human                                  │
            (~0.8s)    │ missing region?      → "Lovely — whereabouts are you thinking?"          │
                       │ which hotel? (unclear) → "Which hotel did you mean?"                      │
                       │ greeting / chit-chat → short conversational reply                         │
                       └───────────────────────────────────┬──────────────────────────────────────┘
                                                            │ enough info to search
                                                            ▼
                       ┌──────────────────── SLOW LANE (masked by filler + streaming) ─────────────┐
                       │ emit filler now ("Let me find the best spa resorts in Antalya…")           │
                       │ full/trimmed decompose → compound retrieve → STREAM render → TTS           │
                       └───────────────────────────────────────────────────────────────────────────┘
```

In a normal dialog you pay the 2s **once** — on the turn the caller is actually
waiting for results — and every clarify/escalate turn is fast.

## 3. The fast intake pass

Replace the current standalone escalation classifier with one slightly richer —
but still small and fast (`gpt-4.1-nano`, ~0.8s) — **intake** call. Same number
of fast calls as today; it just returns enough to drive the conversational
decision, *without* the heavy requirements/taxonomy extraction.

```jsonc
IntakeVerdict {
  "escalate":        bool,            // booking / cancellation / live complaint / distress
  "escalation_type": string | null,
  "language":        string,          // ISO-639-1, for the clarifying question + filler
  "turn_kind":       "search" | "scoped_followup" | "clarify_answer" | "chitchat" | "escalate",
  "region_in_utterance": string | null,   // "Antalya" if named THIS turn, else null
  "hotel_in_utterance":  string | null,   // a hotel name if the caller named one
  "wants_recommendation": bool         // is this a hotel-finding intent at all?
}
```

Note what's **not** here: `requirements`, `query_type_id`, `on_site_required`,
`vibe`, `budget`, etc. Those are only needed to *retrieve*, so they stay in the
slow lane. Smaller output + smaller prompt ⇒ the intake call is genuinely cheap.

## 4. Decision tree (runs in the pipeline, post-intake)

`region_known = intake.region_in_utterance OR session.active_region`
`hotel_known  = intake.hotel_in_utterance OR session.active_hotel_id`

```
if intake.escalate:
    → ESCALATE         (speak the hand-off line)                         ~0.8s

elif hotel named, or scoped_followup with an active hotel:
    → SLOW LANE (scoped)   (resolve name if needed, then retrieve)       needs search

elif intake.wants_recommendation:
    if not region_known:
        → ASK region      ("Which destination are you thinking of?")     ~0.8s  ← the key win
    else:
        → SLOW LANE (broad)   (region known → retrieve)                  needs search

elif turn_kind == "chitchat":
    → quick conversational reply (persona, no retrieval)                 ~0.8s

else:
    → SLOW LANE (let full triage decide)   ← safety net
```

The example that started this: *"I want to go to a resort with spa to relax"*
(no region) → intake sees `wants_recommendation=true`, `region_in_utterance=null`,
session has no region → **ask "whereabouts?" in ~0.8s** instead of 2s+. The
caller answers "Antalya" → `clarify_answer` merges with the prior utterance →
slow lane runs once, with the region in hand.

## 5. Latency budget by turn kind

| Turn kind | Today | With fast lane | How |
|---|--:|--:|---|
| Escalation | ~2.5s | **~0.8s** | intake only |
| Ask for region | ~2.5s | **~0.8s** | intake only, no decompose/retrieve |
| Ask which hotel | ~2.5s | **~0.8s** | intake only |
| Greeting / chit-chat | ~2.5s | **~0.8s** | intake only |
| Actual search (broad) | ~3.8s | ~3.5s wall, **~0.8s to first words** | filler starts after intake; answer streams |
| Scoped answer | ~3.6s | ~3.3s wall, **~0.8s to first words** | same |

The search turns still take ~3s of total work — but the **time-to-first-speech**
drops to ~0.8s on every turn, because either the bot is asking a question
(fast) or it's saying a filler line while it works.

## 6. Masking the unavoidable: filler + streaming

On a slow-lane turn the bot speaks **immediately** after intake, before
retrieval finishes:

- **Filler / backchannel** — a short, persona-appropriate acknowledgment built
  from what intake already knows: *"Ooh, a spa retreat — let me pull up the best
  options in {region} for you."* This is good travel-agent UX **and** it covers
  the decompose+retrieve+render window. (Fits the "Her"-style warm concierge
  direction.)
- **Streaming render → TTS** — start synthesizing speech on the first tokens of
  the answer instead of waiting for the full text. First audio ~0.5s after
  render starts rather than ~1.5s.

Together: the caller hears the bot acknowledge at ~0.8s and start answering
while it's still "thinking," exactly like a person.

## 7. Optional: trim the slow-lane decompose

Once intake already captured region / hotel / language, the slow decompose only
needs `requirements` + `query_type` + `requirements_logic` + `on_site_required`.
A trimmed prompt (drop the captured fields and half the taxonomy prose) means
fewer input tokens ⇒ lower TTFT. Worth measuring, but secondary to filler +
streaming.

## 8. What changes in code

| Module | Change |
|---|---|
| `classifier.py` | Generalise `EscalationClassifier` → `IntakeClassifier`: same nano call, richer JSON (the schema in §3). Escalation stays a field. |
| `pipeline.py` | Insert the §4 decision tree **before** decompose. Fast exits return without ever calling the slow lane. Slow lane = today's decompose→triage→route→retrieve→render. |
| `triage.py` | Largely unchanged — it becomes the slow-lane authority/safety net. The region/hotel clarifications it does today move earlier (intake), so triage focuses on non-negotiables. |
| `decompose.py` | (Phase 2) optional trimmed prompt variant for the slow lane. |
| voice transport | (Phase 2) emit filler at slow-lane entry; stream render tokens to TTS. |

The retrieval core — `CompoundAndDiscovery`, region filter, reranker, the
"matches every requirement" intersection — **does not change at all.**

## 9. Rollout (incremental, each step shippable)

1. **Intake classifier** — extend the nano call to the §3 schema; log its verdict
   alongside the real decomposition (shadow mode) to measure agreement before
   trusting it. No behaviour change yet.
2. **Fast clarification exits** — wire the §4 tree for *escalate* and
   *ask-region* only (the safest, highest-value exits). Conversational turns get
   fast; search turns unchanged.
3. **Filler line** — emit the acknowledgment at slow-lane entry.
4. **Streaming render → TTS** — the biggest felt-latency win on search turns.
5. **Trimmed slow decompose** — measure, adopt if it helps.

Ship 1–2 first: they make the *dialog* feel responsive (which is most turns) at
near-zero risk, because the slow lane stays the authority and the conversation
eval (Tier 1) + decomposer eval (Tier 2) guard against regressions.

## 10. Risks & mitigations

- **Intake misjudges "needs region" / over-asks.** → It only adds *fast exits*;
  the full triage still runs on search turns as the safety net. Grade intake in
  shadow mode (step 1) before it drives behaviour. Add intake cases to the
  Tier-2 eval.
- **Two LLM passes on a search turn (intake + decompose).** → Intake *replaces*
  today's escalation classifier (not an extra call), and it runs in parallel with
  the filler, so it's off the critical path for the spoken response.
- **Filler feels canned.** → Generate it from intake's region/intent and vary
  wording; this is persona work, which is wanted anyway.

## 11. What this explicitly does NOT do

- It does **not** replace decompose with raw vector search — the
  multi-requirement "matches every part" promise stays.
- It does **not** remove clarification — it makes it *faster and earlier*, which
  is exactly the travel-agent behaviour you want.
- It does **not** touch the retrieval/ranking core.

The one-line summary: **keep the brain, but only run the expensive part of it
when you're about to search — and let the agent's own voice cover the rest.**
