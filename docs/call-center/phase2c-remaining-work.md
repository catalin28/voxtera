# Phase 2c — Remaining Work

**Ticket:** VOX-RAG-P2C-001
**As of:** end of `feat/VOX-rag-compound`

The items below are explicitly **out of scope** for Phase 2c and
tracked for Phase 3 or a follow-up chore branch.

---

## 1. e5 junk-overlap mitigation (HIGH priority)

**Problem.** With `DEFAULT_MIN_SCORE = 0.70`, `multilingual-e5-large`
surfaces "plausible" matches in the 0.74–0.79 band even for genuinely
nonsensical queries (e.g. `"xyzzy plugh"` → 0.76 against a hotel-spa
chunk). As a result, compound intersections across nonsense
requirements may still return hotels with `reason: null` rather than
collapsing to `no_match_above_threshold`.

**Why margin doesn't fix this.** The relative-margin filter is
trim-only: the top chunk always survives. It tightens the kept set
around the peak but cannot zero a result.

**Options (pick one in Phase 3):**

1. **Cross-encoder re-rank** (`bge-reranker-v2-m3` or similar) on the
   union of per-requirement hits — would tighten the real-vs-junk gap
   significantly. Cost: ~150 ms per requirement, plus model footprint.
2. **Raise the per-requirement floor for compound only** — e.g.
   `compound_min_score = 0.78`. Cheap, but per-query tuning will be
   needed across regions/languages.
3. **LLM-based requirement validation** — pre-screen each requirement
   against a one-line corpus summary before fan-out. Adds an LLM call
   to the critical path.
4. **Maximum Marginal Relevance (MMR)** at the broad-discovery layer —
   diversifies results but doesn't help junk specifically.

Recommendation: prototype option 1 in Phase 3 and benchmark against
the calibration script.

## 2. Compound drop heuristic

**Current behaviour.** When the strict intersection is empty,
`CompoundAndDiscovery._intersect` drops the requirement whose
broad-discovery returned the **fewest hotels**.

**Alternative heuristics worth measuring:**

- Drop the requirement with the **lowest top score**.
- Drop the requirement that contributes the lowest *average* score
  among intersection candidates.
- Allow the caller to mark requirements as `required` vs.
  `nice_to_have` and only drop the latter.

No measurement framework exists yet; revisit when a real product flow
asks for it.

## 3. Negative requirements

`"hotel WITHOUT a casino"` is not handled. The cleanest path is a
caller-side decomposition into positive requirements plus a post-filter
on `payload.activity_tags` / `payload.category`. Deferred.

## 4. Multi-region union

Compound currently scopes every requirement to the same `region`. A
guest asking for *"a spa hotel in either the Turkish Riviera or
Aegean"* requires a union strategy. Out of scope for 2c.

## 5. Caching

There's no cache for repeat compound queries. Given that the
per-requirement `BroadHotelDiscovery` calls are independent and the
corpus is small, an embedding-keyed LRU at the broad-discovery layer
would amortise across compound calls that share a requirement. Defer
until traffic justifies it.

## 6. Latency / p95 budgets

Not yet instrumented. The expectation is that `max(per_req_latency)`
dominates total wall-clock; once `voxtera.call_center.metrics` exposes
per-request timings (Phase 3 admin/observability task), add a
percentiles dashboard and a budget assertion to the live smoke.

## 7. Admin UI surface

`http://localhost:8083/call_center/` does not expose a panel for the
compound endpoint yet. Trivial follow-up once a designer signs off on
the multi-input UX (requirements list editor with add/remove).

## 8. Authentication

`/call_center/api/kb/compound` is currently unauthenticated, same as
`/api/kb` and `/api/kb/discover`. The Phase 3 admin auth story will
cover all three together; no compound-specific work needed.

## 9. Internationalisation of `missing_requirements`

The list returned to the caller is the **normalised English-stripped**
requirement string the caller supplied. If the front-end wants a
localised rendering it must keep its own mapping. No server-side
translation planned.

## 10. Test gaps acknowledged

- No load test (compound concurrency vs. e5 GIL behaviour).
- No fault-injection test for partial Qdrant timeouts mid-fan-out (one
  requirement times out, others succeed → currently the whole call
  raises `retriever_error`). A partial-success mode is a Phase 3
  decision.

---

## Out-of-scope reminders (intentionally NOT done)

- Embedding model swap (e5-large → bge-m3 / nomic-embed-text). Tracked
  in `phase3-embedding-evaluation.md` (does not yet exist).
- Reranker integration (see §1).
- Hybrid BM25 + dense fusion at the broad-discovery layer.
- Persistent per-tenant configuration of `RELATIVE_MARGIN` and
  `DEFAULT_MIN_SCORE`.
