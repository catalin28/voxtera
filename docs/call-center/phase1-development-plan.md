# Voxtera Call Center - Phase 1 Detailed Development Plan

Project: VOX - Tourism Call Center Voice Agent  
Phase: 1 - Hotel Resolver  
Story Reference: VOX-RAG-P1-001  
Branch: feat/vox-hotel-resolver  
Date: June 2026

---

## 1. Purpose

This plan translates the Phase 1 user story into a concrete engineering implementation that a developer can execute end-to-end.

Primary outcome:

- Build a reliable Elasticsearch-based hotel resolver for Turkish call-center utterances.
- Meet decision thresholds:
  - score >= 0.85: auto_resolve
  - score 0.55-0.84: needs_clarification (top 3)
  - score < 0.55: no_match
- Achieve >= 90% unambiguous accuracy on the current 10-hotel baseline.

---

## 2. Scope Guardrails

### In scope

- Resolver module implementation.
- Elasticsearch analyzer/mapping/query improvements for hotel-name resolution.
- Resolver output contract and threshold logic.
- Unit and integration tests for mention classes.
- Baseline performance snapshot.

### Out of scope

- Chat endpoint integration.
- Session active_hotel_id wiring.
- Triage/decomposition wiring.
- Qdrant retrieval.
- Redis caching for resolver.
- Voice pipeline behavior.

---

## 3. Existing Baseline and Target Files

### Existing files used in this phase

- src/voxtera/call_center/server.py
- src/voxtera/call_center/clients.py
- data/seed/hotels.json
- docs/call-center/elasticsearch-optimisation.md
- docs/call-center/phase1-user-story.md

### New files to add

- src/voxtera/call_center/resolver.py
- tests/call_center/test_hotel_resolver.py
- tests/call_center/fixtures/hotel_mentions.json
- docs/call-center/phase1-test-report.md

Optional config file if needed for tuning:

- config/hotel_resolver_thresholds.json

---

## 4. Design Logic (Why This Works)

The resolver must handle noisy real-world hotel mentions. The design uses layered logic:

1. Turkish-aware lexical matching for suffixes and diacritics.
2. Weighted multi-field relevance so true hotel names outrank weak contextual matches.
3. Controlled fuzziness for STT noise.
4. Threshold-based decisioning for safe behavior under uncertainty.

### 4.1 Ranking logic

Ranking should prioritize stronger identity fields first:

- name (highest)
- aliases
- chain
- district/city

This prevents a weak city-level hit from outranking a true hotel-name hit.

### 4.2 Safety logic

The threshold bands map confidence to behavior:

- High confidence: proceed automatically.
- Medium confidence: ask for clarification with top ranked options.
- Low confidence: fail safe with no_match.

This keeps false positives low while preserving useful guidance for ambiguous mentions.

---

## 5. Resolver Contract

Resolver input:

- mention_text: str
- optional context dict (reserved for future use)

Resolver output schema:

```json
{
  "decision": "auto_resolve | needs_clarification | no_match",
  "hotel_id": "string | null",
  "top_score": 0.0,
  "candidates": [
    {
      "hotel_id": "string",
      "name": "string",
      "score": 0.0
    }
  ],
  "reason": "string",
  "normalized_mention": "string"
}
```

Rules:

- candidates populated only for needs_clarification.
- candidates sorted descending by score.
- no_match never raises uncaught exceptions.

---

## 6. Implementation Plan by Workstream

## Workstream A - Elasticsearch analyzer and mapping hardening

### A1. Protect brand tokens from aggressive stemming

Implement keyword marker filter in analyzer chain for known brands.

Examples to protect:

- rixos
- hilton
- marriott
- regnum
- maxx
- cornelia
- limak

Expected effect:

- Reduces wrong token stems causing missed exact/partial matches.

### A2. Add synonym support for frequent spoken forms

Use query-time synonyms for common colloquial forms and STT-like variants.

Examples:

- max royal => maxx royal
- riksos => rixos
- kornelia => cornelia

### A3. Confirm weighted query fields

Required weighting direction:

- name strongest
- aliases second
- chain third
- district/city lower

Use deterministic query boosts and keep minimum_should_match to 1.

---

## Workstream B - Resolver core module

Implement HotelResolver class in src/voxtera/call_center/resolver.py.

Suggested class API:

```python
class HotelResolver:
    def __init__(self, es_client, index_name: str = "hotels") -> None:
        ...

    async def resolve(self, mention_text: str) -> dict:
        ...
```

Suggested private methods:

- _normalize_mention(text: str) -> str
- _build_query(normalized: str) -> dict
- _parse_hits(hits: list) -> list[dict]
- _decide(candidates: list[dict]) -> dict

Decision pseudocode:

```python
if not candidates:
    return no_match

top = candidates[0]
if top.score >= 0.85:
    return auto_resolve(top)

if top.score >= 0.55:
    return needs_clarification(candidates[:3])

return no_match
```

Normalization steps:

1. trim whitespace.
2. lowercase.
3. normalize apostrophe variants.
4. collapse repeated spaces.
5. keep Turkish characters; avoid destructive transliteration for now.

---

## Workstream C - Test suite

Create tests/call_center/test_hotel_resolver.py with parameterized scenarios.

Test categories:

1. exact match
2. partial name
3. Turkish suffix forms
4. fuzzy variants
5. ambiguous mentions
6. no match
7. threshold boundary behavior
8. sorting behavior for clarification candidates

Fixture file tests/call_center/fixtures/hotel_mentions.json should contain:

- input text
- expected decision
- expected hotel_id or candidate set
- optional min score expectation

---

## 7. Detailed Task Breakdown with Stages

Use the same three stages for every task so progress is visible and auditable.

Stage definitions:

- START: task is prepared, scoped, and unblocked.
- DEVELOP: implementation and local validation are actively in progress.
- FINISH: acceptance evidence is complete and task is ready to merge.

Status values:

- Not Started
- In Progress
- Blocked
- Done

### 7.1 Stage Tracker Board

| Task | START (entry + setup) | DEVELOP (build + validate) | FINISH (evidence + closure) | Status | Owner | Notes |
|---|---|---|---|---|---|---|
| Task 1: Create resolver module skeleton | Confirm file path, class API, and output schema | Implement HotelResolver class, helpers, and safe no_match path | Module imports cleanly and empty-input test passes | Done | AI + Dev | Implemented in src/voxtera/call_center/resolver.py |
| Task 2: Implement ES query builder | Confirm weighted fields and analyzer choices | Implement deterministic query with boosts and fuzziness | Query-shape tests pass and query is documented | Done | AI + Dev | Mapping moved to src/voxtera/call_center/index_config.py with brand keyword_marker + synonym_graph |
| Task 3: Implement candidate parsing and decisioning | Confirm threshold constants and decision contract | Parse hits, sort candidates, apply thresholds | Boundary tests pass at 0.85/0.84/0.55/0.54 | Done | AI + Dev | Boundary tests passing |
| Task 4: Add mention-class tests | Finalize fixture schema and case inventory | Add exact/partial/suffix/fuzzy/ambiguous/no-match tests | Test suite passes locally and in CI | Done (local) | AI + Dev | 11/11 local; CI hookup pending |
| Task 5: Add integration smoke test | Confirm local ES and seed data are loaded | Run resolver against live ES sample mentions | At least one smoke run report captured | Done (mock) | AI + Dev | scripts/smoke_hotel_resolver.py — live-ES run deferred until creds available |
| Task 6: Produce phase1-test-report.md | Confirm report template and required metrics | Populate pass/fail counts and accuracy metrics | Report reviewed and committed | Done | AI + Dev | docs/call-center/phase1-test-report.md |

### 7.2 Task Checklists (Track per stage)

### Task 1: Create resolver module skeleton

- [x] START - Define class signature, output contract, and file location.
- [x] DEVELOP - Implement HotelResolver class, output helpers, and basic logging hooks.
- [x] FINISH - Verify module imports cleanly and resolve returns no_match safely for empty input.

### Task 2: Implement ES query builder

- [x] START - Confirm boost strategy and matching fields.
- [x] DEVELOP - Implement weighted ES query with deterministic structure.
- [ ] FINISH - Validate generated query in unit tests and document example query payload.

### Task 3: Implement candidate parsing and decisioning

- [x] START - Lock threshold policy and candidate output shape.
- [x] DEVELOP - Map ES hits to candidates, sort by score, enforce top-3 on clarification path.
- [x] FINISH - Pass all threshold boundary tests and decision-contract tests.

### Task 4: Add mention-class tests

- [x] START - Build test fixture list for all mention types.
- [x] DEVELOP - Implement parameterized tests (exact, partial, suffix, fuzzy, ambiguous, no_match).
- [ ] FINISH - Ensure full suite passes locally and in CI.

### Task 5: Add integration smoke test (optional but recommended)

- [ ] START - Confirm local Elasticsearch availability and seeded hotels index.
- [ ] DEVELOP - Execute resolver against real ES for representative mentions.
- [ ] FINISH - Capture one successful smoke-test evidence set.

### Task 6: Produce phase1-test-report.md

- [ ] START - Define required report sections (accuracy, failures, edge cases, next actions).
- [ ] DEVELOP - Fill report with actual test results and metrics.
- [ ] FINISH - Review, finalize, and commit report.

### 7.3 Stage Exit Rules

A task can move to FINISH only when all are true:

1. Required tests for that task are passing.
2. Evidence is captured in code, tests, or report artifacts.
3. No unresolved blocker remains for that task.
4. The task status in the Stage Tracker Board is updated to Done.

---

## 8. Examples for Developers

### 8.1 Expected behavior examples

Example A: clear match

Input:

- Rixos Premium Belek

Expected output:

```json
{
  "decision": "auto_resolve",
  "hotel_id": "rixos_premium_belek",
  "top_score": 0.93,
  "candidates": [],
  "reason": "score_above_auto_threshold",
  "normalized_mention": "rixos premium belek"
}
```

Example B: ambiguous mention

Input:

- Hilton Antalya

Expected output:

```json
{
  "decision": "needs_clarification",
  "hotel_id": null,
  "top_score": 0.78,
  "candidates": [
    {"hotel_id": "hilton_lara", "name": "Hilton Lara", "score": 0.78},
    {"hotel_id": "hilton_belek", "name": "Hilton Belek", "score": 0.76}
  ],
  "reason": "score_in_clarification_band",
  "normalized_mention": "hilton antalya"
}
```

Example C: no match

Input:

- abc xyz random hotel

Expected output:

```json
{
  "decision": "no_match",
  "hotel_id": null,
  "top_score": 0.31,
  "candidates": [],
  "reason": "score_below_min_threshold",
  "normalized_mention": "abc xyz random hotel"
}
```

Example D: Turkish suffixed form

Input:

- Rixosta

Expected behavior:

- should resolve same as Rixos in most cases.
- if score falls in middle band, clarification is acceptable.

---

## 9. Test Cases (Concrete)

## 9.1 Core table

| ID | Type | Input | Expected |
|---|---|---|---|
| T01 | exact | Rixos Premium Belek | auto_resolve -> rixos_premium_belek |
| T02 | exact | Hilton Bomonti Istanbul | auto_resolve -> hilton_bomonti_istanbul |
| T03 | partial | Rixos Belek | auto_resolve or clarification with top candidate rixos_premium_belek |
| T04 | partial | Kaya Palazzo | auto_resolve or clarification with top candidate kaya_palazzo_belek |
| T05 | suffix | Rixosta | resolves to rixos_premium_belek path |
| T06 | suffix | Kaya'da | resolves to kaya_palazzo_belek path |
| T07 | suffix | Hilton'a | resolves to hilton family path |
| T08 | fuzzy | Riksos Belek | auto_resolve or clarification with top candidate rixos_premium_belek |
| T09 | fuzzy | Kaaya Palazzo | auto_resolve or clarification with top candidate kaya_palazzo_belek |
| T10 | ambiguous | Hilton Antalya | needs_clarification with <=3 candidates |
| T11 | ambiguous | Rixos | needs_clarification with <=3 candidates |
| T12 | no_match | Some random string | no_match |

## 9.2 Boundary tests

Use a mocked hit parser to force top_score values:

- B01: top_score = 0.85 -> auto_resolve
- B02: top_score = 0.84 -> needs_clarification
- B03: top_score = 0.55 -> needs_clarification
- B04: top_score = 0.54 -> no_match

## 9.3 Candidate sorting test

Input with 4 candidates in random order should return only top 3 sorted descending by score.

---

## 10. Suggested Pytest Structure

```python
@pytest.mark.parametrize(
    "mention,expected_decision,expected_hotel",
    [
        ("Rixos Premium Belek", "auto_resolve", "rixos_premium_belek"),
        ("Hilton Antalya", "needs_clarification", None),
        ("Some random string", "no_match", None),
    ],
)
async def test_resolver_decisions(...):
    ...
```

Add separate test modules if preferred:

- test_hotel_resolver_decision.py
- test_hotel_resolver_matching.py
- test_hotel_resolver_boundaries.py

---

## 11. Execution Sequence

1. Create branch feat/vox-hotel-resolver and pull latest.
2. Implement analyzer and query changes.
3. Implement resolver class and decision policy.
4. Add tests and fixtures.
5. Run unit tests.
6. Run integration smoke against local ES data.
7. Produce phase1-test-report.md with measured outcomes.
8. Open PR into develop.

---

## 12. Verification and Sign-off

Sign-off checklist:

- [ ] Resolver module implemented and documented.
- [ ] Threshold policy validated by boundary tests.
- [ ] Mention-class tests implemented and passing.
- [ ] Unambiguous accuracy >= 90% on current dataset.
- [ ] Ambiguous/no-match behaviors are safe and deterministic.
- [ ] Test report generated with evidence.

---

## 13. Risks and Response Plan

Risk: false positives from fuzzy matching.  
Response: keep conservative thresholds and enforce clarification band.

Risk: over-stemming brand names in Turkish.  
Response: keyword marker and dedicated regression tests.

Risk: limited 10-hotel dataset hides long-tail issues.  
Response: complete Phase 1 on this baseline, then schedule immediate corpus expansion checkpoint.

---

## 14. Deferred Work Notes (Next Phases)

Not part of this implementation but planned:

- Session active_hotel_id updates.
- Clarification prompt generation in chat flow.
- Integration with triage/decomposition and source routing.
- Redis memoization for repeated mentions.

---

## 15. Developer Quick Start

Use this as direct build order:

1. Build resolver class.
2. Build decision thresholds.
3. Build mention tests.
4. Tune analyzer/query until acceptance criteria are met.
5. Publish report.

If these five steps are completed with evidence, Phase 1 is considered implementation-complete.
