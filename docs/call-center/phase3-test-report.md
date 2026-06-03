# Phase 3 — Concierge Agent — Test Report

**Branch:** `feat/VOX-rag-concierge`
**Date:** 2026-06-03

---

## 1. Unit suite

```
.\.venv\Scripts\python.exe -m pytest tests/call_center/test_concierge.py -q
8 passed in 0.27s
```

| Class | Tests | Notes |
|---|---|---|
| `TestShortCircuits` | 2 | empty utterance, empty region — no LLM, no compound call |
| `TestHappyPath` | 1 | decompose -> compound -> render flow, args propagated |
| `TestFailures` | 2 | decompose raises (compound not called), render raises (retrieval still exposed) |
| `TestReasonPassthrough` | 1 | retrieval `reason` and `missing_requirements` reach the caller |
| `TestDecompositionLimits` | 2 | `max_requirements` cap, missing optional fields default to None |

All LLM steps stubbed; no network access required.

## 2. Full call_center regression

```
.\.venv\Scripts\python.exe -m pytest tests/call_center/ -q
58 passed in 0.43s
```

Phase 2a/2b/2c suites untouched and still green.

## 3. Mock smoke (offline)

```
.\.venv\Scripts\python.exe scripts/smoke_concierge.py
```

```
Scenario                                        Reason                        Compound?  Verdict
------------------------------------------------------------------------------------------------
happy: spa+diving full match                    None                          True       PASS
partial: missing diving                         partial_match_only            True       PASS
no_match: nothing fits                          no_match_above_threshold      True       PASS
short-circuit: empty utterance                  empty_utterance               False      PASS
short-circuit: empty region                     no_region_scope               False      PASS
failure: decompose raises ...                   decompose_error               False      PASS

Results: 6 passed, 0 failed
```

## 4. Live smoke (real Claude + live Qdrant)

```
.\.venv\Scripts\python.exe scripts/smoke_concierge_live.py
```

Anthropic model: `claude-haiku-4-5-20251001`
Qdrant target:   `http://138.197.142.222:6333` (collection `hotel_kb`)

| # | Scenario | Decomp.lang | Retrieval.reason | Verdict |
|---|---|---|---|---|
| 1 | EN: spa + scuba diving | `en` | `None` (1 hotel: Akra Kemer, 0.797) | PASS |
| 2 | EN: family + kids club + beach | `en` | `no_match_above_threshold` | PASS |
| 3 | EN: luxury wellness retreat | `en` | `None` (1 hotel: Selectum Luxury Resort Belek, 0.805) | PASS |
| 4 | TR: family + kids club + beach | `tr` | `no_match_above_threshold` | PASS |

**Result: 4 passed, 0 failed. Exit code 0.**

### Sample answers (verbatim from live run)

**Scenario 1 (EN happy):**
> "Akra Kemer is an excellent choice for you both! It features a full spa
> center with Turkish bath, sauna, massage rooms and beauty treatments,
> plus a PADI-certified diving center where your partner can enjoy scuba
> diving. You'll also have year-round access to their indoor pool and
> morning yoga sessions to complement your relaxation."

**Scenario 2 (EN no_match — honest acknowledgement):**
> "I'm sorry, but I don't have any hotels in the Turkish Riviera that
> match all your requirements for a family resort with a kids club right
> on the beach. I'd recommend contacting our reservations team
> directly..."

**Scenario 4 (TR — answered in Turkish):**
> "Ne yazık ki Turkish Riviera bölgesinde arama kriterlerinize tam
> olarak uyan bir otel bulamadık. Çocuk kulübü ve özel plajı olan
> tesislerin mevcut veritabanımızda bu bölgede listelenmiyor..."

### Observations

- Decomposition consistently returns 1-3 well-formed noun-phrase
  requirements. Claude correctly extracts `activity_tags` ("diving",
  "kids") and `category_hint` ("wellness") when the utterance is
  unambiguous.
- Language detection worked first-try on Turkish; the render step
  answered in Turkish without any special-casing.
- The render step honoured `no_match_above_threshold` honestly in both
  scenarios — no hallucinated hotels.
- One scenario originally hit `partial_match_only` and produced a
  truthful acknowledgement ("none of these properties are specifically
  marketed as luxury wellness retreats, so you may want to confirm…").

## 5. No-regression snapshot

- 2a live smoke: 6/6 (re-run 2026-06-03) — see phase2a-test-report §8.
- 2b live smoke: 8/8 (re-run 2026-06-03) — see phase2b-test-report §8.
- 2c live smoke: 6/6 (carried forward) — see phase2c-test-report.

## 6. Encoding fix

The initial live-smoke run crashed when printing the Turkish utterance
under cp1252. Resolved by reconfiguring `sys.stdout`/`sys.stderr` to
UTF-8 at script entry. Display in PowerShell is still cp1252-decoded
(visual mojibake only); the script's stdout bytes are valid UTF-8.
