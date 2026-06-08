# Phase 3 — Concierge Agent — Development Plan

**Ticket:** VOX-RAG-P3-001
**Branch:** `feat/VOX-rag-concierge`

---

## Architecture

```
GET /call_center/api/concierge?region=...&q=...
                │
                ▼
        ConciergeAgent.answer(utterance, region)
                │
        ┌───────┴────────┐
        │ 1. decompose_fn │  ── Anthropic Claude Haiku ──> {requirements[],
        │   (LLM call)    │                                 activity_tags,
        └───────┬─────────┘                                 category_hint,
                │                                           language}
                ▼
        ┌────────────────────┐
        │ 2. CompoundAndDis- │  ── BroadHotelDiscovery × N  ── live Qdrant
        │    covery.discover │     (Phase 2c, unchanged)
        └────────┬───────────┘
                 │
                 ▼
        ┌──────────────────┐
        │ 3. render_fn      │  ── Anthropic Claude Haiku ──> answer: str
        │    (LLM call)     │     (grounded in evidence chunks only)
        └──────────────────┘
```

## Module layout

| File | Role |
|---|---|
| `src/voxtera/call_center/concierge.py` | `ConciergeAgent` class + default Anthropic decompose/render builders |
| `src/voxtera/call_center/server.py` | `handle_concierge` (≤12 lines, no business logic) + route |
| `tests/call_center/test_concierge.py` | 8 unit tests with stubbed LLM + fake compound |
| `scripts/smoke_concierge.py` | Mock smoke (offline, 6 scenarios) |
| `scripts/smoke_concierge_live.py` | Live smoke (real Claude + live Qdrant, 4 scenarios) |
| `docs/call-center/phase3-{user-story,development-plan,test-report,remaining-work}.md` | Docs |

## Public API

```python
class ConciergeAgent:
    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        compound: CompoundAndDiscovery | None = None,
        decompose_fn: DecomposeFn | None = None,   # (utt, region) -> dict
        render_fn: RenderFn | None = None,         # (payload) -> str
        max_requirements: int = DEFAULT_MAX_REQUIREMENTS,
        model: str = DEFAULT_MODEL,
    ) -> None: ...

    async def answer(self, *, utterance: str, region: str) -> dict: ...
```

Return shape:

```jsonc
{
  "utterance":  "...",
  "region":     "...",
  "decomposition": {
    "requirements": ["spa wellness", "scuba diving"],
    "activity_tags": ["diving"] | null,
    "category_hint": "wellness" | null,
    "language": "en"
  },
  "retrieval": { /* full CompoundAndDiscovery payload */ },
  "answer": "Hotel Aqua matches both your spa and scuba diving needs ...",
  "reason": null | "empty_utterance" | "no_region_scope"
          | "decompose_error" | "render_error"
          | "empty_requirements" | "partial_match_only"
          | "no_match_above_threshold"
}
```

## LLM contract

### Decompose system prompt
- Returns strict JSON (no markdown fences, no prose).
- Each requirement is a short noun phrase suitable for semantic search.
- Caps at 5 requirements (also enforced server-side via `max_requirements`).
- Detects language as ISO-639-1.

### Render system prompt
- Receives only `{hotel_id, name, score, evidence{req: chunk_text}}` per hotel
  (trimmed payload — no internal IDs, scores rounded, chunk text capped at 280
  chars) plus `reason`, `missing_requirements`, and detected `language`.
- Answers in the detected language, plain conversational text, 2-4 sentences.
- Must acknowledge `partial_match_only` / `no_match_above_threshold` honestly.

## In-process vs HTTP

The handler instantiates `ConciergeAgent` in-process and calls
`CompoundAndDiscovery` directly (NOT over HTTP to `/api/kb/compound`).
This avoids an extra hop, reuses the shared aiohttp session and the
already-loaded embedding model, and keeps end-to-end latency dominated
by the two Claude round-trips + one Qdrant fan-out.

## Short-circuits (no LLM cost)

| Trigger | reason | answer |
|---|---|---|
| utterance is empty/whitespace | `empty_utterance` | "I didn't catch that…" |
| region is empty/whitespace | `no_region_scope` | "Which region are you looking at?" |
| decompose_fn raises | `decompose_error` | generic apology, compound NOT called |
| render_fn raises | `render_error` | generic apology, retrieval still exposed |

## Test strategy

- **Offline unit tests** (8): stub `decompose_fn`, stub `render_fn`, fake
  CompoundAndDiscovery with scripted payloads. Cover: short-circuits,
  happy path, reason passthrough, decompose-output sanitisation,
  decompose/render exceptions.
- **Mock smoke** (6): same shape as unit tests but printed scenario
  table for manual scan; runs in <1s.
- **Live smoke** (4): real Claude Haiku + live Qdrant. EN happy, EN
  partial, EN no_match, TR language detection. Hard pass = decompose
  returned ≥1 requirement AND answer length > 10 chars.

## Known limitations (carried from Phase 2c)

- e5-large junk-overlap (0.74-0.79) means `no_match_above_threshold` is
  not always reachable on live corpus for nonsensical queries; render
  still handles it gracefully when it does fire.
- No latency instrumentation yet (Phase 3c).
- No cross-encoder rerank (Phase 3a).
- No admin UI panel (Phase 3b).
