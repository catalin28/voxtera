# Phase 2c — Compound-AND Discovery + Relative-Margin Filter — User Story

**Ticket:** VOX-RAG-P2C-001
**Branch:** `feat/VOX-rag-compound`
**Depends on:** Phase 2a (single-hotel KB retrieval), Phase 2b (broad cross-hotel discovery)

---

## User-facing intent

> *"As a guest planning a trip to the Turkish Riviera, I want to ask for a
> hotel that satisfies **multiple** requirements at once — e.g. 'luxury
> hotel with a spa AND scuba diving for me AND a kids club for my son' —
> and get back hotels that satisfy **all** of them, with one piece of
> supporting evidence per requirement."*

Phase 2b can already answer single-aspect cross-hotel questions
("luxury hotel with spa"). Phase 2c composes the same retriever once
per requirement and intersects the results at the hotel level.

When the strict intersection is empty (the corpus has no hotel that
satisfies every requirement), the service degrades gracefully: it drops
the weakest-supported requirement(s) until at least one hotel remains
and returns those with `reason: "partial_match_only"` plus a
`missing_requirements` list, so the front-end can say *"I couldn't find
a hotel with X, but here are ones with everything else."*

## Scope — what's in / out

| In scope | Out of scope |
|---|---|
| `CompoundAndDiscovery` class fanning out one `BroadHotelDiscovery` per requirement | LLM-based decomposition of free-form prompts into requirements (caller does this) |
| Strict set-intersection at hotel-id level | Re-ranking by a cross-encoder |
| Graceful degradation → `partial_match_only` with `missing_requirements` | Multi-region union searches |
| Relative-margin tail trim applied per requirement (`RELATIVE_MARGIN = 0.05`) | Negative requirements ("hotel WITHOUT a casino") |
| `GET /call_center/api/kb/compound?region=...&requirements=a\|b\|c` | Persistent caching of compound results |
| Mock + live smoke harnesses | Multi-tenant or per-user customisation |

## Gherkin scenarios

```gherkin
Feature: Compound-AND multi-requirement hotel discovery (Phase 2c)

  Background:
    Given the corpus is the seed Turkish Riviera hotels
    And BroadHotelDiscovery uses min_score=0.70 with relative_margin=0.05

  Scenario: Strict intersection on two requirements
    When I request region="Turkish Riviera" with requirements=["luxury spa", "outdoor pool"]
    Then I get count >= 1
    And every returned hotel has evidence for both requirements
    And reason is null

  Scenario: Strict intersection on three requirements
    When I request region="Turkish Riviera" with requirements=["beach front", "spa massage", "restaurant dinner"]
    Then I get count >= 1
    And every returned hotel has evidence for all three requirements
    And reason is null

  Scenario: Partial match degradation
    Given one requirement matches no hotel in the intersection
    When I request that requirement together with a matchable one
    Then reason is "partial_match_only"
    And missing_requirements contains the unmatchable requirement
    And the returned hotels carry evidence only for the kept requirements

  Scenario: Empty region scope
    When I request region="  " with requirements=["spa"]
    Then count is 0 and reason is "no_region_scope"

  Scenario: Empty requirements list
    When I request region="Turkish Riviera" with requirements=["", "  "]
    Then count is 0 and reason is "empty_requirements"

  Scenario: Single requirement passes through to Phase 2b semantics
    When I request region="Turkish Riviera" with requirements=["beach sea"]
    Then count >= 1 and reason is null
    And every returned hotel has evidence["beach sea"] set

  Scenario: Max requirements cap
    Given I supply 7 requirements but max_requirements is 5
    When I call discover
    Then only the first 5 are processed
    And normalized_requirements has length 5

  Scenario: Relative-margin filter at the per-retriever level
    Given Phase 2b returns hotels with scores 0.90, 0.87, 0.70 for one requirement
    When the requirement is processed
    Then only the 0.90 and 0.87 hotels enter the intersection (0.70 trimmed)

  Scenario: Retriever fan-out failure
    Given BroadHotelDiscovery raises an exception
    When CompoundAndDiscovery.discover is called
    Then count is 0 and reason is "retriever_error"
```

## Acceptance criteria

1. **Strict intersection works.** At least 3 live "strict" scenarios
   return ≥ 1 hotel with evidence for every requirement.
2. **Graceful degradation works.** When the strict intersection is
   empty, `reason: "partial_match_only"` is returned with a populated
   `missing_requirements` list, *or* `reason: "no_match_above_threshold"`
   if every requirement individually returns nothing.
3. **Margin trim is silent.** `RELATIVE_MARGIN = 0.05` is applied
   inside `HotelKBRetriever` and `BroadHotelDiscovery`; the top chunk
   always survives so no non-empty result becomes empty; no new
   `reason` value is introduced for the margin.
4. **Existing 2a/2b live smokes still pass** (6/6 and 8/8) — margin
   only trims tail.
5. **`server.py` handler is thin** (≤ 12 lines, no business logic).

## Known limitations (documented, not blockers)

- **e5 junk-overlap at the absolute floor.** With
  `DEFAULT_MIN_SCORE = 0.70`, the multilingual-e5-large model surfaces
  weak matches (0.74–0.79) even for genuinely nonsensical queries (e.g.
  `"xyzzy plugh"`). This means a compound intersection across two
  nonsense requirements does **not** collapse to
  `no_match_above_threshold` against the live corpus the way it does
  against the mock token-overlap backend. The relative-margin filter
  cannot fix this either — by design it never zeros a non-empty result.
  Mitigation strategies (cross-encoder re-rank, raising the per-req
  floor for compound, LLM-based requirement validation) are tracked in
  `phase2c-remaining-work.md`.
