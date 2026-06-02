# Voxtera Call Center - Phase 1 User Story

Project: VOX - Tourism Call Center Voice Agent  
Phase: 1 - Hotel Resolver  
Branch: feat/vox-hotel-resolver  
Date: June 2026

---

## Story ID

VOX-RAG-P1-001

## User Story

As a Turkish-speaking call center agent,
I want the system to reliably resolve the hotel a caller is referring to even when they use suffixes, partial names, or minor pronunciation/transcription errors,
so that I can answer hotel-specific questions quickly without asking unnecessary clarification questions.

---

## Business Value

- Reduces friction in early call turns by identifying the correct hotel quickly.
- Prevents incorrect answers caused by matching the wrong property.
- Creates a dependable foundation for scoped RAG retrieval in later phases.

---

## Scope (In)

- Elasticsearch-based hotel mention resolver.
- Turkish-aware matching behavior for hotel mentions.
- Weighted matching across hotel metadata fields:
  - hotel name (highest)
  - aliases
  - chain
  - city and district
- Resolver decision thresholds:
  - score >= 0.85: auto-resolve
  - score 0.55-0.84: return top 3 candidates for clarification
  - score < 0.55: no match
- Unit tests for exact, partial, suffixed, fuzzy, ambiguous, and no-match cases.
- Initial validation against current 10-hotel seed dataset.

## Scope (Out)

- Chat endpoint integration and session hotel locking.
- Triage and decomposition integration.
- Qdrant retrieval logic and confidence handling.
- Redis resolver caching.
- Voice pipeline behaviors.

---

## Acceptance Criteria

Given the resolver has access to the hotel index,
When the caller mentions a hotel clearly (exact or strong partial mention),
Then the resolver returns the correct hotel_id with score >= 0.85 and auto-resolves.

Given the caller uses Turkish suffixed hotel forms,
When the mention is submitted for resolution,
Then the resolver resolves to the same canonical hotel_id as the unsuffixed form in at least 90 percent of test cases.

Given a mention produces multiple plausible hotels,
When the top score falls in the clarification band,
Then the resolver returns up to 3 ranked candidates and does not auto-resolve.

Given the mention is not recognized,
When no candidate reaches minimum confidence,
Then the resolver returns no-match safely (null resolution) without exception.

Given the Phase 1 test suite is executed,
When all tests complete,
Then unambiguous resolution accuracy is at least 90 percent on the current dataset.

Given threshold boundary test inputs,
When candidate scores are at 0.85, 0.84, 0.55, and 0.54,
Then behavior exactly follows threshold policy.

---

## Functional Rules

1. Resolver input is plain mention text from caller utterance.
2. Resolver output must include:
   - decision: auto_resolve | needs_clarification | no_match
   - hotel_id (for auto_resolve only)
   - candidates (for needs_clarification only)
   - top_score
   - reason
3. Candidate list must be sorted by descending score.
4. No-match path must never crash request flow.

---

## Test Matrix (Phase 1)

Minimum cases to cover:

- Exact matches
- Partial matches
- Turkish suffixed forms (for example: Rixosta, Kaya'da, Hilton'a)
- Fuzzy/transcription-like variants (for example: Riksos Belek)
- Ambiguous mentions (multiple valid Hilton/Rixos variants)
- No-match random strings

Recommended baseline count: at least 50 assertions total (can include parameterized cases), while keeping the current 10-hotel data source.

---

## Definition of Done

1. Resolver implementation completed with threshold policy and ranked candidates.
2. Unit tests added and passing for all mention classes.
3. Unambiguous accuracy reaches >= 90 percent on current Phase 1 dataset.
4. Boundary behavior for threshold transitions is explicitly tested and passing.
5. Resolver behavior documented with sample inputs/outputs.
6. Deferred items are recorded for next phase handoff.

---

## Dependencies

- Elasticsearch instance and hotel index available.
- Hotel seed data loaded (current 10-hotel baseline).
- Existing Phase 0 infrastructure from call-center foundation branch.

---

## Risks and Mitigations

- Risk: Brand names over-stemmed in Turkish analysis.
  - Mitigation: protect known hotel brands in analyzer strategy and verify with dedicated tests.

- Risk: Small dataset hides edge cases.
  - Mitigation: log unresolved/ambiguous mentions and expand corpus in next checkpoint.

- Risk: Over-aggressive fuzzy matching returns wrong hotel.
  - Mitigation: enforce strict threshold policy and ambiguity fallback.

---

## Handoff Notes for Next Phase

When this story is complete, Phase 3 integration can consume resolver output to:

- set active_hotel_id in session context,
- ask disambiguation questions for candidate sets,
- route scoped hotel queries to retrieval path 1.
