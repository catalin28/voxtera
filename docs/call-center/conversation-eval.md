# Conversation Eval — multi-turn flows for the concierge pipeline

**Status:** Tier 1 (offline) implemented and green — 9 flows.
**Harness:** [`tests/call_center/test_conversation_flows.py`](../../tests/call_center/test_conversation_flows.py)

---

## 1. Why this exists

Every concierge bug found in manual testing (stale hotel shadowing a new
question, a fresh broad request hijacked into a scoped lookup, "do they have
bars?" answered with a clarification, the carry-over slug echo) passed the
component unit tests. The unit tests check each stage in isolation with
hand-written, *perfect* decompositions. The real failures only appear when
three things meet:

1. a real (sometimes sloppy) decomposition,
2. multi-turn **session state** (`active_hotel_id`, `active_region`, `pending_slots`),
3. the **routing / triage** branches.

That combination is never exercised by the component suite, so it stays green
while live conversations break. The user ends up being the integration test —
one random question at a time. This eval turns "find bugs by chance" into
"find bugs by coverage."

## 2. Two tiers

| Tier | Runs | Needs | Catches |
|---|---|---|---|
| **1 — structural (offline)** | CI, every commit | nothing (no keys, no Qdrant) | routing, session-state, triage, the input guards (slug / generic-ref / empty-requirements), escalation |
| **2 — live (end-to-end)** | manual / nightly | API keys + Qdrant + ES + Redis | decomposer **quality** (does the LLM classify the follow-up as scoped? extract the right region / requirements / intent?) |

Tier 1 is the harness in this folder. It drives the **real**
`ConciergePipeline` (classifier, `QueryDecomposer._coerce`, `Triage`,
`SourceRouter`, session store, scoped/empty/guard logic, inline resolver). The
only fakes are the network leaves — the KB retriever, the hotel resolver, and
the render LLM. Each turn **scripts what the decomposer returns**, including the
messy outputs that caused real bugs, and asserts the path, the hotels
retrieved, and the resulting session state.

Tier 2 is not yet built — it would replay the same utterances through the live
decomposer and grade the decomposition + final answer. It belongs behind a
`-m live_eval` marker like the existing exit-criteria suite, because it costs
tokens and needs the droplet services.

## 3. Tier-1 golden conversations

Each flow is a sequence of turns sharing one session. Grouped by the failure
class it pins down.

| Flow | Turns | What it proves |
|---|---|---|
| `spa_then_scoped_then_followup_then_broad` | broad → scoped(named) → follow-up("do they have bars?") → broad | the full happy path: follow-up stays scoped to the active hotel; a later broad request resets it |
| `new_mention_overrides_session` | scoped(Akra) → "how about Crystal Tat?" | a freshly named hotel re-resolves; stale `active_hotel_id` does **not** shadow it |
| `slug_echo_does_not_hijack_broad` | scoped(Akra) → broad with `hotel_mention="akra_kemer"` echoed | the slug guard drops the carry-over echo; the broad query is not hijacked |
| `generic_reference_scopes_to_session_hotel` | scoped → "is **the hotel** on the beach?" (`intent=practical_info`) | "the hotel" is dropped as anaphora; the practical_info intent does **not** divert a scoped query to web; it scopes to the session hotel |
| `empty_requirements_injects_overview` | scoped → "tell me more" with `requirements=[]` | empty requirements get an overview default instead of failing closed |
| `scoped_food_not_clarified` | scoped → "do they have bars?" (`intent=food`) | triage does **not** interrupt a scoped factual question with a dietary clarification |
| `broad_missing_geography_clarifies` | broad, no region | triage **does** ask for geography when it's genuinely missing |
| `named_hotel_resolves_inline` | "tell me about Regnum Carya" | inline resolver maps a typed name → id → scoped retrieval |
| `live_complaint_escalates` | "I'm at the hotel and my room is not ready" | escalation short-circuits before retrieval |

Each `expect` asserts some of: `path`, the exact `hotel_ids` retrieved,
`clarify` (true/false), `escalate`, and `active_hotel_after` (the session's
`active_hotel_id` once the turn completes).

## 4. Running it

```bash
# offline, no keys needed
PYTHONPATH=src python -m pytest tests/call_center/test_conversation_flows.py -v
```

It is part of the normal `tests/call_center` run, so it gates every change to
the pipeline.

## 5. The harness has teeth

A passing suite is only useful if it fails when the behaviour regresses.
Verified: reverting the triage scoped-guard makes `scoped_food_not_clarified`
fail; reverting the `_run_kb` session-hotel fix makes the scoped flows fail.
The flows assert on real outcomes (path + hotels + session state), not just
"it didn't crash."

## 6. Extending it

Add a new entry to `CONVERSATIONS` in the harness: a list of
`(utterance, raw_decomposition, expect)` turns. Use `_D(...)` for the
decomposition with overrides, and script the *messy* output you want to defend
against (a wrong `query_type`, an echoed mention, an empty `requirements`) so
the test proves the pipeline copes. Mirror the row in the table above.

Candidate flows still worth adding:

- Language switch mid-conversation (en → tr) and the Turkish demo script.
- A second clarification turn hitting the 2-turn budget, then proceeding.
- Comparison query ("Rixos vs Kaya") routing to broad.
- A destination question ("what is Antalya known for?") → destination placeholder.
- Pronoun follow-up **after** a broad turn (no single active hotel) — should
  clarify, not silently scope to a random hotel.

## 7. Tier 2 — live decomposer grading (built)

Tier 1 proves the *machinery* around the decomposer is correct. Tier 2 proves
the decomposer itself classifies a follow-up as scoped, extracts the right
region/requirements, and doesn't echo carry-over state — the LLM's job, and the
source of the remaining live failures (nano tagging "do they have bars?" as
`broad`, "on the beach" as `practical_info`).

- **Labelled cases:** [`tests/call_center/eval_data/decompose_cases.jsonl`](../../tests/call_center/eval_data/decompose_cases.jsonl)
  — 36 utterances (broad / scoped / follow-up / echo / generic-ref / geography /
  Turkish / destination / web / escalate / comparison / compound), each with the
  expected `query_type`, and where unambiguous also `intent` / `region` /
  `hotel_named` / `language` / `requires`.
- **Runner:** [`scripts/eval_decompose.py`](../../scripts/eval_decompose.py)
  — runs the live decomposer `--runs` times per case, scores each field, and
  reports per-field accuracy, query_type run-to-run **stability**
  (non-determinism), and the failing cases. Pass `--models A,B` to compare.

```bash
python scripts/eval_decompose.py --selftest                 # offline scorer check
python scripts/eval_decompose.py                            # grade Haiku
python scripts/eval_decompose.py --models claude-haiku-4-5-20251001,gpt-4.1-nano
```

Needs API keys + costs tokens, so it runs on the droplet/your machine, not CI.
`query_type` is the headline metric (full coverage); `intent` is graded only on
the few cases where it's unambiguous, since the taxonomy overlaps
(amenities/atmosphere/recommendation) and would otherwise add noise.

### Still open
- **Answer-quality grading.** Tier 2 grades the decomposition. A further pass
  could run the full pipeline live (real retrieval + render) and LLM-judge
  whether the final answer addressed the question and stayed grounded (no
  hallucinated amenities). That's the natural Tier 3.
