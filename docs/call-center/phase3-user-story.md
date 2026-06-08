# Phase 3 — Concierge Agent (Decompose + Compound + Render) — User Story

**Ticket:** VOX-RAG-P3-001
**Branch:** `feat/VOX-rag-concierge`
**Depends on:** Phase 2c (CompoundAndDiscovery)

---

## User-facing intent

> *"As a guest, I want to ask the concierge a natural-language question
> — in any language, with one or many implicit requirements — and get a
> short, honest, evidence-grounded answer that names matching hotels,
> acknowledges what's missing, and never invents amenities."*

Phase 2c gave us a structured `requirements[] -> hotels[]` retriever.
Phase 3 closes the user-visible loop:

1. **Decompose** the utterance (+ region scope) into a structured plan
   `{requirements, activity_tags, category_hint, language}` via a
   single LLM call.
2. Run the existing `CompoundAndDiscovery` against that plan.
3. **Render** a 2-4 sentence answer in the detected language, grounded
   strictly in the retrieved evidence chunks.

This is the first piece of the RAG stack that a real guest could talk
to end-to-end. All previous phases exposed admin/debug surfaces only.

## Scope — what's in / out

| In scope | Out of scope |
|---|---|
| `ConciergeAgent` orchestrator with injectable `decompose_fn` / `render_fn` | Voice-pipeline integration (bot.py / pipeline.py — deferred) |
| Default LLM backend: Anthropic Claude Haiku (`LLM_MODEL_OVERRIDE` honoured) | Streaming responses (one-shot only) |
| `GET /call_center/api/concierge?region=...&q=...` HTTP surface | Multi-turn conversation memory |
| Short-circuits: empty utterance, empty region, decompose error, render error | Persistent transcript / audit log (caller's responsibility) |
| Language detection + same-language answer | Tool-calling / function-calling flows |
| Honest handling of `partial_match_only` and `no_match_above_threshold` | Cross-encoder re-rank (Phase 3a, deferred) |
| Mock smoke (offline) + live smoke (real Claude + live Qdrant) | Admin UI panel (Phase 3b, deferred) |
| Unit tests with stubbed LLM (8 tests) | p95 latency claims (Phase 3c, deferred) |

## Gherkin scenarios

```gherkin
Scenario: Single-requirement EN utterance
  Given a guest says "Where can I find a luxury wellness retreat?"
  And the region is "Turkish Riviera"
  When the concierge answers
  Then the decomposition contains at least one wellness-related requirement
  And the answer is in English
  And the answer names a hotel that is present in the retrieval payload

Scenario: Multi-requirement EN utterance
  Given a guest says "I want a great spa and scuba diving for my partner"
  And the region is "Turkish Riviera"
  When the concierge answers
  Then the decomposition contains 2+ requirements
  And `compound.discover` is called with those requirements
  And the answer names hotels that satisfy both requirements

Scenario: Turkish utterance with language detection
  Given a guest says "Ailecek tatil için çocuk kulübü ve özel plajı olan bir otel arıyorum"
  And the region is "Turkish Riviera"
  When the concierge answers
  Then the detected language is "tr"
  And the answer is written in Turkish

Scenario: Partial match (corpus missing a requirement)
  Given the corpus has spa hotels but no scuba diving in the region
  When the guest asks for both spa AND scuba diving
  Then retrieval.reason is "partial_match_only"
  And the answer explicitly acknowledges that scuba diving is unavailable

Scenario: Empty utterance short-circuit
  Given an empty utterance
  When the concierge answers
  Then reason is "empty_utterance"
  And neither the decompose LLM nor the compound retriever is called

Scenario: Decompose LLM failure
  Given the decompose LLM raises an exception
  When the concierge answers
  Then reason is "decompose_error"
  And `compound.discover` is NOT called
  And a generic fallback answer is returned
```

## Acceptance criteria

- `ConciergeAgent.answer(utterance, region)` returns a dict with keys:
  `utterance`, `region`, `decomposition`, `retrieval`, `answer`, `reason`.
- All LLM steps are dependency-injected; default = Anthropic Claude.
- `GET /call_center/api/concierge?region=&q=` returns the same payload.
- Unit suite: 8/8 green, fully offline (no network).
- Mock smoke: 6/6 PASS (deterministic stubs).
- Live smoke: 4/4 PASS against real Claude + live Qdrant in EN + TR.
- No regressions in 2a/2b/2c suites (full call_center suite still 58/58 green).
