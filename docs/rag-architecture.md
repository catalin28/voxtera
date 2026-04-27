# Voxtera RAG Architecture (VOX-E5)

- **Status:** Proposed
- **Date:** 2026-04-26
- **Target epic:** VOX-E5 — RAG Layer for Tourism Knowledge
- **Owner:** Architect

> Companion doc: [`architecture.md`](architecture.md) (overall pipeline) and the four ADRs in [`decisions/`](decisions/) (Pipecat / Whisper / Chirp 3 HD / Daily.co choices).

---

## 1. Summary

Voxtera's voice loop currently answers from Claude's general knowledge only. For a hotel deployment, guests will ask questions like "what's on the menu tonight?", "when does the spa close?", or "my TV isn't working" — questions that require **the specific hotel's information**, which Claude cannot know.

This document describes the design of a **Retrieval-Augmented Generation (RAG) layer** that gives Claude grounded access to hotel-specific knowledge while staying within the voice latency budget. It also defines the boundary between **RAG (static knowledge)** and **tool calls (live data)**, since both are needed for a real deployment but they have very different architectures.

---

## 2. Goals

- Guests can ask hotel-specific questions and get accurate, grounded answers, in any of Voxtera's supported languages.
- Total turn latency stays at or below the VOX-6 target of ~3 seconds (95th percentile), even with retrieval added to the pipeline.
- The system supports **multiple hotels** in a single deployment without cross-tenant data leakage.
- Hotel content (menu, spa, FAQs, troubleshooting) is updateable without redeploying the bot.
- Failure modes are graceful: if retrieval fails, the bot still answers — just with a polite "I don't have specific information on that, but…".

## 3. Non-goals

- **Live data.** Room availability, current bookings, "is the pool open right now", maintenance ticket creation, billing — all out of scope for RAG. These belong to the **tool calls** path (see §6).
- **Long-form generation.** RAG here is for short conversational answers, not multi-paragraph summaries or document drafting.
- **Knowledge graph reasoning.** No multi-hop entity graphs in v1. Flat document retrieval is enough for the hotel domain.
- **Re-ranking with LLMs on the hot path.** Adds 500ms+ of latency that we don't have. Lightweight cosine retrieval only.
- **Per-guest personalisation.** Treat all guests the same in v1. Personalisation comes later if at all.

---

## 4. User scenarios

Concrete questions the system must handle. These are the test set we'll measure ourselves against.

| Category | Example user question | Expected source |
|---|---|---|
| Menu / dining | "What's the chef's special tonight?" | RAG (menu doc) |
| Hours | "When does breakfast end?" | RAG (hours doc) |
| Spa / amenities | "Do you have a couples' massage?" | RAG (spa services doc) |
| Room features | "How do I connect to the wifi?" | RAG (welcome guide) |
| Troubleshooting | "The TV remote isn't working" | RAG (troubleshooting guide) → fallback to tool (open ticket) |
| Local recommendations | "Where should I have dinner nearby?" | RAG (concierge guide) |
| Policies | "What time is checkout?" | RAG (policies doc) |
| Live availability | "Is the pool open right now?" | **Tool call** (PMS / facilities API) |
| Live booking | "Can I book a 7pm spa appointment?" | **Tool call** (booking system) |
| Issue reporting | "My room is too cold" | **Tool call** (ticketing system) |
| Personal data | "What room am I in?" | **Tool call** (guest profile, with auth) |

Note that some questions ("the TV isn't working") naturally span both — RAG suggests a fix; if the fix doesn't work, a tool call opens a ticket. The bot decides between them via the LLM's tool-use behaviour.

---

## 5. Architecture

### 5.1 Voice pipeline with RAG injected

```
Microphone
   │
   ▼
LocalAudioTransport (in)
   │
   ▼
VADProcessor  ──── Silero VAD, emits VAD events
   │
   ▼
OpenAI Whisper STT  ──── final TranscriptionFrame per turn
   │
   ▼
LLMUserAggregator   ──── adds user message to LLMContext
   │
   ▼
**RAGContextInjector**  ◀── NEW. Intercepts user message, retrieves
   │                         hotel-specific chunks, injects them as
   │                         a system or context message.
   ▼
Anthropic Claude  ──── answers with grounded context + can call tools
   │
   ▼
OpenAI TTS
   │
   ▼
LocalAudioTransport (out)
```

Only one new processor is added to the pipeline (`RAGContextInjector`). Everything else stays as-is. The retrieval itself runs out-of-band against an external store; the processor is just glue.

### 5.2 Data flow

1. User speaks → STT produces a final `TranscriptionFrame`.
2. `LLMUserAggregator` appends "user said X" to the live `LLMContext`.
3. **`RAGContextInjector`** reads the latest user message, asks the **Retriever** for the top-K hotel chunks relevant to it, and rewrites the context to inject those chunks as a system-style message before the LLM call.
4. Claude answers with the injected context available, and may call **tools** for live data.
5. TTS speaks the reply.

### 5.3 Components

#### Document store
The "source of truth" — hotel docs in their original form. Examples: a `menu.md` per restaurant, a `spa-services.md`, a `troubleshooting.md`, a `policies.md`, a CSV of room amenities. Stored in object storage (S3 / GCS) or a content-managed system. Each document carries metadata: `hotel_id`, `language`, `category`, `updated_at`.

#### Chunker
Splits long docs into 100–300 token chunks with light overlap (~20 tokens). Markdown-aware so it doesn't split mid-sentence or mid-table. Output: rows of `(hotel_id, doc_id, chunk_id, text, metadata)`.

#### Embedding service
Converts each chunk into a vector. **Recommended: OpenAI `text-embedding-3-small`** — fast, cheap, multilingual out of the box. Alternative for self-hosted: `bge-m3` or `paraphrase-multilingual-mpnet-base-v2`.

#### Vector store
- **MVP:** SQLite with a single embeddings table. Cosine similarity computed in Python at query time. Works fine for one hotel with up to ~10,000 chunks. Zero infrastructure.
- **Production:** **pgvector** (if Postgres is already in your stack) or **Qdrant** (if you'd rather have a dedicated service). Both support per-tenant filtering, hybrid search, and millisecond-scale lookup.

#### Retriever
Given a user question, returns the top-K (default K=3) most relevant chunks. Steps:

1. Embed the question with the same model as the chunks.
2. Query the vector store with `WHERE hotel_id = :hotel_id AND language IN :langs` plus cosine similarity.
3. Apply a minimum similarity threshold (e.g. 0.3) so genuinely off-topic questions return nothing rather than the least-bad chunks.
4. Return the chunks with their metadata.

#### `RAGContextInjector` (Pipecat `FrameProcessor`)
Sits between `LLMUserAggregator` and `AnthropicLLMService`. On every `LLMContextFrame` it:

1. Extracts the latest user message.
2. Calls the Retriever asynchronously.
3. If chunks come back, prepends them to the LLM context as a system message: `"Here are relevant excerpts from the hotel's information. Use them when answering, but only if they're relevant. If they don't answer the question, ignore them.\n\n<chunk text…>"`.
4. Pushes the modified context downstream.

If retrieval errors out or returns nothing, it forwards the original context unmodified — failure is silent and recoverable.

---

## 6. RAG vs tool calls — the split

A real hotel deployment is not RAG-only. Roughly 70/30 RAG/tools, by question count. The split:

| Use RAG when… | Use a tool call when… |
|---|---|
| Information is static or changes slowly (menu, spa, policies, FAQs) | Information is live (occupancy, current weather, available appointments) |
| The same question gives the same answer for hours/days | The answer changes minute-to-minute |
| The bot just needs to *say* something | The bot needs to *do* something (book, ticket, pay) |
| Source content fits in a document | Source content lives in a database/API |

Tools are exposed to Claude via Anthropic's tool-use API. Pipecat already supports this. Examples:

- `get_pool_status() -> {open: bool, hours_today: str}` — facilities API
- `check_spa_availability(date, service) -> [time slots]` — booking system
- `open_maintenance_ticket(room, issue, urgency) -> ticket_id` — ticketing
- `get_my_room_info(guest_id) -> {room_number, checkout_time, …}` — PMS

Tools ship as a separate epic (potentially **VOX-E10**, to be created) but the design here assumes Claude can call them when grounded RAG context isn't enough.

---

## 7. Latency budget

Voice loops live or die by latency. VOX-6's acceptance criterion is ~3s end-to-end (`stop talking → bot starts talking`). Our current run measures around 1.3s without RAG. Here's the budget with RAG added:

| Stage | Without RAG (today) | With RAG (target) |
|---|---|---|
| STT (Whisper batch) | 400–800 ms | 400–800 ms |
| **Retrieval (embed + lookup)** | — | **150–350 ms** |
| LLM (Claude Haiku) | 600–1200 ms | 700–1400 ms (slightly larger context) |
| TTS first audio chunk | 200–400 ms | 200–400 ms |
| **Total** | **1.2–2.4 s** | **1.5–3.0 s** |

Mitigations to keep the budget:

- **Embed in parallel with anything we can.** The retriever doesn't have to wait for STT to fully finalise — it can start on the interim transcript and just retry once the final arrives if it changed. (Optional optimisation.)
- **Aggressive caching.** "What time does breakfast end?" gets asked many times a day. Cache the question→answer pair (or just the retrieval result) for an hour or so. Easy 200ms saving on repeat hits.
- **Top-K = 3.** Don't pull 10 chunks and overwhelm the LLM context.
- **No LLM reranking on the hot path.** It's tempting (better quality) but it doubles latency.
- **Pre-computed embeddings.** Document chunks are embedded at ingest time, never at query time.

If the budget slips during implementation, that's a real signal to revisit the approach (smaller embedding model, faster vector store, smaller context). Track P50 and P95 latency from day one.

---

## 8. Multilingual approach

Voxtera detects the user's language automatically and replies in it. RAG must work the same way.

### Option A — Multilingual embeddings (recommended)
Use a model that embeds across languages into a shared vector space. `text-embedding-3-small` does this reasonably well for European languages and Japanese. A user's French question retrieves chunks from English documents (and vice versa) without needing translation.

**Pros:** single index, simplest implementation, zero translation overhead at query time.
**Cons:** quality dips for low-resource languages and for jargon-heavy text (menu items, technical troubleshooting).

### Option B — Per-language indexes
Translate the source documents into each supported language at ingest time, embed each language separately, and at query time retrieve from the user's language only.

**Pros:** higher retrieval quality. **Cons:** translation cost, drift between languages, more index storage. Needed only if Option A's quality is unacceptable.

### Recommendation
Start with Option A. Add Option B for any language where retrieval quality suffers measurably (track this with a held-out evaluation set per language).

---

## 9. Multi-tenancy

Even if the first deployment is one hotel, design the data model for many. Migrating later is painful.

### Hard requirements

- Every chunk row has a `hotel_id`.
- Every retrieval query filters on `hotel_id` before similarity search.
- The bot's runtime config carries the active `hotel_id` (likely from the WebRTC session in VOX-E2 or a phone-number lookup in VOX-E7).
- No global / shared corpus. If two hotels both have a "spa pricing" doc, they live in separate rows under separate `hotel_id`s.

### Schema sketch (pgvector / SQLite)

```sql
CREATE TABLE chunks (
    id            BIGSERIAL PRIMARY KEY,
    hotel_id      TEXT NOT NULL,
    doc_id        TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    language      TEXT NOT NULL,            -- 'en', 'fr', 'auto', etc.
    category      TEXT,                     -- 'menu', 'spa', 'policies', …
    text          TEXT NOT NULL,
    embedding     VECTOR(1536) NOT NULL,    -- text-embedding-3-small dim
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (hotel_id, doc_id, chunk_index)
);

CREATE INDEX chunks_tenant_lang ON chunks (hotel_id, language);
CREATE INDEX chunks_embedding ON chunks USING ivfflat (embedding vector_cosine_ops);
```

`UNIQUE (hotel_id, doc_id, chunk_index)` makes re-ingestion idempotent: re-uploading a document overwrites by composite key rather than duplicating.

---

## 10. Failure modes & recovery

| Failure | Behaviour |
|---|---|
| Retrieval times out (e.g. >500ms) | Drop retrieval for this turn, log a warning, let Claude answer from base knowledge with an "I don't have specific information…" hedge in the system prompt. |
| Embedding API errors | Same as above — graceful degradation, never block the turn. |
| Empty result set (everything below similarity threshold) | Inject nothing. Claude knows it's a hotel context from the system prompt and will say so. |
| Cross-tenant leak (chunk from hotel A served to hotel B) | **Should be impossible by construction** (`hotel_id` filter is mandatory). Add an integration test that breaks loudly if it ever fires without a `hotel_id`. |
| Stale content (menu changed but doc not re-ingested) | Operational issue — surfaced via `updated_at` on chunks and a freshness check in the admin dashboard (VOX-E8). |

---

## 11. Phased delivery

Build the smallest thing that works end-to-end, then layer on.

### Phase 1 — MVP, single hotel, SQLite (LOCKED SCOPE)

The five open questions below have been resolved for the POC. This is the scope to build:

| Concern | Decision |
|---|---|
| **Source formats** | PDF, Excel/CSV, Markdown/plain text. DB ingestion deferred to Phase 2. |
| **Freshness** | Manual `voxtera ingest` re-run on demand. No watchers, no cron, no webhooks. |
| **Source language** | English documents only in the POC. |
| **Test guest languages** | English, French, Japanese, Romanian (10-question eval set per language). |
| **Hosting model** | Same process as the bot. SQLite file + in-process NumPy retrieval. |
| **Operator UI** | CLI only (`voxtera ingest`, `voxtera list-chunks`, `voxtera search`, `voxtera delete`). Web admin deferred. |
| **Vector store** | SQLite (single `chunks` table). Move to pgvector / Qdrant in Phase 2. |
| **Embedding model** | `text-embedding-3-small`. |
| **Tenancy** | One hard-coded `hotel_id` in v1, but schema is multi-tenant from day one. |

**POC done-when:**

- A guest can ask 10 hotel-specific questions per language and get correct grounded answers in ≥85% of cases.
- P95 turn latency stays at or below 3 seconds with retrieval enabled.
- Re-ingesting the same document is idempotent (no duplicate chunks).
- If retrieval fails or returns nothing, the bot still answers (graceful degradation, no crash).

### Phase 2 — Production data store, multi-tenant
- Move embeddings from SQLite to **pgvector** (or Qdrant).
- Multi-tenant with `hotel_id` resolution at session start.
- Ingestion pipeline (CLI + library) so non-engineers can add/update docs.
- Caching layer (Redis or in-memory LRU) for common queries.
- Latency monitoring (P50, P95) per hotel and per category.

### Phase 3 — Tools (separate epic)
- Tool calls for live data (pool, spa availability, ticketing).
- LLM decides when to use tools vs RAG; both can fire in the same turn.

### Phase 4 — Quality & ops
- Per-language evaluation sets and quality dashboard.
- Per-hotel admin to monitor freshness, coverage, and unmet questions.
- Optional reranking experiment on a slow lane (off the voice hot path).

---

## 12. Open questions — resolved for POC

All five questions have answers for the POC. Each one is recorded here with the decision, the reasoning, and the deferred work that pushes back to Phase 2.

### Q1 — Where do hotel docs come from? **RESOLVED**

**Decision:** PDF + Excel/CSV + Markdown/plain text for the POC. Database ingestion (PMS, POS, CMS) deferred to Phase 2.

**Reasoning:** PDF + Excel covers what hotels actually have on hand without forcing them to expose a database. Markdown supports anything we author ourselves for the demo. Direct DB integration is real engineering per source system and isn't needed to validate the retrieval pipeline.

**Deferred to Phase 2:** Source adapters for common PMS systems (Opera, Mews, Cloudbeds), POS systems (Square, Toast), and generic JDBC.

### Q2 — How fresh does the data need to be? **RESOLVED**

**Decision:** Manual `voxtera ingest` re-run on demand. No watchers, no cron, no webhooks for POC.

**Reasoning:** Even if menu changes daily, having a hotel staff member (or developer) run a single command is faster to build and cheaper to operate than any auto-watcher. We don't yet know which content categories actually need to be fresh — the POC will surface that.

**Deferred to Phase 2:** Folder watcher or scheduled re-ingest for content categories that prove to need it.

### Q3 — Multilingual quality. **RESOLVED**

**Decision:** Option A (single multilingual index using `text-embedding-3-small`). Source documents in English only for POC. Test in English, French, Japanese, Romanian.

**Reasoning:** Romance and major Asian languages perform well with `text-embedding-3-small` cross-lingually; one index is the simplest design that could possibly work. Romanian is included as a deliberate test of a smaller-resource language so we know early if Option B becomes necessary.

**Deferred to Phase 2:** Per-language indexes (Option B) for any language where the eval set shows quality below 85%.

### Q4 — Hosting model. **RESOLVED**

**Decision:** Same process as the bot. SQLite file on disk, retrieval performed in-process via NumPy cosine similarity.

**Reasoning:** No network cost, no extra deployment story, no infra to manage. A POC running on one developer laptop should not need a separate service.

**Deferred to Phase 2:** Move the vector store out of process to pgvector or Qdrant when scaling beyond one hotel and one bot instance.

### Q5 — Admin UI. **RESOLVED**

**Decision:** CLI only. Subcommands: `voxtera ingest`, `voxtera list-chunks`, `voxtera search`, `voxtera delete`.

**Reasoning:** The "operator" of the POC is a developer. A CLI is a fraction of the work of a web admin and is faster to use than a UI for someone who lives in the terminal. A web admin only earns its keep with non-technical operators.

**Deferred:** Web admin UI, likely combined with VOX-E8 (Admin Dashboard) when actual hotel staff are using the system.

## 13. Risks

- **Latency creep.** Each "small" optimisation (rerank, larger top-K, hybrid keyword+semantic search) adds ms. Defend the budget early and often.
- **Hallucination despite RAG.** Claude will sometimes make things up even with grounding. Mitigation: explicit system-prompt instruction to refuse when grounding is thin, and a slow-lane evaluation pipeline.
- **Cross-language quality.** Option A may underperform for some language pairs. Don't ship blind — evaluate.
- **Stale content.** Hotel ops people don't update Markdown files. Plan for the world where the menu in the doc is two months old.
- **Over-reliance on RAG vs tools.** Some questions look static but are actually live (e.g. "is the bar open?" — depends on day of week, holidays, private events). Get the split right.

---

## 14. Success metrics

- **Quality:** ≥85% correct answers on a 50-question per-hotel evaluation set, per language. Measured weekly.
- **Latency:** P95 turn latency ≤3s with RAG enabled. Measured per-turn in production, surfaced on the admin dashboard.
- **Coverage:** ≤5% of guest questions classified as "no relevant content found" after the first 30 days of operation per hotel.
- **Reliability:** No cross-tenant leak ever (one is too many). Verified by integration tests on every PR.
- **Operability:** New hotel onboarding ≤ one working day from "we got the docs" to "bot answers correctly."

---

## 15. Next steps

1. ~~Review this doc with stakeholders, fill in §12 open questions.~~ **Done.**
2. Hand off [`rag-implementation-plan.md`](rag-implementation-plan.md) to the LLM coder. It's a self-contained, 13-step plan with status tracking that replaces GitHub-Issue tracking for VOX-E5 Phase 1.
3. Decide: pgvector or Qdrant for Phase 2 — write `ADR-0005` once Phase 1 reveals real bottlenecks.

The implementation plan owns the granular work breakdown; this doc owns the architecture and the rationale.
