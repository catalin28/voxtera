# Voxtera — RAG System Development Plan
**Project:** VOX — Tourism Call Center Voice Agent
**Version:** 1.0 · June 2026
**Approach:** Chat-first, voice second
**Confidential — Internal Document**

---

## Overview

This document defines the phased development plan for the Voxtera RAG system. The system is built chat-first: all retrieval logic is implemented and validated in a chat interface before any voice components are added. This approach reduces debugging complexity, accelerates iteration on retrieval quality, and ensures the intelligence layer is proven before audio edges are introduced.

**Three knowledge sources:**
- **Hotel KB** (Qdrant) — curated hotel data, static, quality-controlled
- **Destination KB** (Qdrant) — country and city knowledge, semi-static
- **Web Search** — events, local operators, real-time conditions, dynamic

**Stack:** Elasticsearch · Qdrant · Redis · multilingual-e5-large · Claude Sonnet · FastAPI
**Voice stack (Phase 6 only):** Gladia Solaria-1 · Pipecat · Telnyx · Chirp 3 HD

---

## Why Chat-First

In chat mode you can:

- See the exact query that hits the system — no STT transcription errors masking retrieval bugs
- Inspect raw Qdrant chunks returned before Claude synthesises them
- Test Turkish text directly without Gladia in the loop
- Iterate on retrieval quality in seconds, not minutes
- Run automated test suites against every query type

Every retrieval bug found in chat is a bug that would have taken 3× longer to diagnose in the voice pipeline.

---

## Branch Strategy

```
main
  └── develop
        ├── feat/VOX-rag-foundation        Phase 0
        ├── feat/VOX-hotel-resolver        Phase 1
        ├── feat/vox-kb-retrieval          Phase 2a — Scoped search
        ├── feat/VOX-rag-broad             Phase 2b — Broad discovery
        ├── feat/VOX-rag-compound          Phase 2c — Compound AND
        ├── feat/VOX-rag-filters           Phase 2d — Budget + Geo filters
        ├── feat/VOX-rag-dual              Phase 2e — Dual / comparison search
        ├── feat/VOX-rag-ingest            Phase 2f — Ingestion, confidence, cache
        ├── feat/VOX-triage-decomposition  Phase 3
        ├── feat/VOX-web-search            Phase 4
        ├── feat/VOX-chat-assembly         Phase 5
        └── feat/VOX-voice-pipeline        Phase 6
```

Each phase (and sub-phase) branch merges to `develop` before the next begins.
`main` receives only from `develop` at milestone releases.
Voice pipeline (`feat/VOX-voice-pipeline`) **never starts** until Phase 5 is signed off.

**Why Phase 2 is split into 2a–2f.** The original Phase 2 bundled six search modes plus ingestion, confidence handling, and caching into a single 8–10 day branch. We split it into independently-mergeable sub-phases so each retrieval mode is proven (unit tests + mock smoke + live smoke) before the next is layered on top. The total Phase 2 scope and exit criteria are unchanged — only the delivery cadence.

---

## Phase 0 — Infrastructure Setup

**Branch:** `feat/VOX-rag-foundation`
**Estimated duration:** 3–4 days

### Goal

Three services running locally and verified end-to-end. No application logic yet — just the plumbing confirmed to work.

### Deliverables

- [ ] Elasticsearch running with Turkish analyzer configured
- [ ] Qdrant running with `hotel_kb` and `destination_kb` collections created
- [ ] Redis running with session key structure defined
- [ ] multilingual-e5-large embedding model loaded and verified
- [ ] Docker Compose for local dev environment
- [ ] FastAPI app skeleton with one `/chat` endpoint (returns hardcoded response)
- [ ] `.env` structure with all required keys documented
- [ ] 10-hotel seed dataset from call center data loaded into all three systems

### Verification Test

```
Insert hotel document
  → Embed text with multilingual-e5-large
  → Store vector + payload in Qdrant
  → Run test query
  → Retrieve correct document
  → Confirm payload fields present
```

### Exit Criteria

Insert → embed → store → retrieve works end-to-end for one hotel document. All three services healthy and reachable from FastAPI.

---

## Phase 1 — Hotel Resolver

**Branch:** `feat/VOX-hotel-resolver`
**Estimated duration:** 4–5 days

### Goal

Elasticsearch hotel resolver working reliably on Turkish hotel mentions including suffixed forms, partial names, and mispronunciations.

### Context

Turkish is agglutinative. Callers attach case suffixes to hotel names that must be stripped before matching:

| Caller says | Suffix | Meaning | Stripped |
|---|---|---|---|
| `Rixosta` | -ta | at Rixos | `Rixos` |
| `Rixosa git` | -a | to Rixos | `Rixos` |
| `Rixostan` | -tan | from Rixos | `Rixos` |
| `Kaya'da` | -da | at Kaya | `Kaya` |
| `Hilton'a` | -a | to Hilton | `Hilton` |

The Elasticsearch Turkish analyzer handles this automatically. Without it, `Rixosta` does not match `Rixos Premium Belek`.

### Deliverables

- [ ] Turkish analyzer configured and verified on suffix stripping
- [ ] Hotel index with all mandatory fields loaded from call center sample data
- [ ] Multi-field weighted search: name (highest) → aliases → chain → city → district
- [ ] Resolution threshold logic:
  - Score ≥ 0.85 → auto-resolve, lock `hotel_id`
  - Score 0.55–0.84 → return top 3 candidates for clarification
  - Score < 0.55 → no match, proceed without hotel scope
- [ ] Turkish character normalisation verified: İ/i, I/ı, Ş/ş, Ğ/ğ
- [ ] Unit tests covering all mention types below

### Test Cases

```
# Exact match
"Rixos Premium Belek"         → rixos_premium_belek        ✓
"Hilton Bomonti Istanbul"     → hilton_bomonti_istanbul     ✓

# Partial name
"Rixos Belek"                 → rixos_premium_belek        ✓
"Kaya Palazzo"                → kaya_palazzo_belek         ✓

# Turkish suffixed forms
"Rixosta"                     → rixos_premium_belek        ✓
"Kaya'da"                     → kaya_palazzo_belek         ✓
"Hilton'a"                    → hilton_bomonti_istanbul    ✓

# Mispronunciation / transcription error
"Riksos Belek"                → rixos_premium_belek        ✓ (fuzzy)
"Kaaya Palazzo"               → kaya_palazzo_belek         ✓ (fuzzy)

# Ambiguous — multiple valid matches
"Hilton Antalya"              → [hilton_lara, hilton_belek] ← ask caller
"Rixos"                       → [rixos_premium_belek, rixos_sungate, rixos_downtown] ← ask

# No match
"Some random string"          → null — proceed without hotel scope
```

### Exit Criteria

Given 50 test hotel mentions in realistic Turkish phrasing, resolver returns correct `hotel_id` on ≥ 90% of unambiguous cases. Ambiguous cases correctly return candidate lists. No match cases handled without crash.

---

## Phase 2 — RAG Core (split into 2a–2f)

**Umbrella goal:** Qdrant retrieval working correctly across all six search modes, plus ingestion, confidence handling, and Redis caching. This is the largest phase — the quality of everything downstream depends on what is built here.

**Umbrella exit criteria (rolls up from 2a–2f):** Given 20 test queries across all search modes, retrieval returns relevant chunks with correct confidence handling on ≥ 80% of queries. Cache hit confirmed on repeated queries. Compound AND correctly enforces both requirements.

Each sub-phase below ships on its own branch and merges to `develop` independently. Sub-phases are ordered by dependency; 2b–2e can be reordered if needed once 2a is in.

---

### Phase 2a — Scoped Search

**Branch:** `feat/vox-kb-retrieval`
**Estimated duration:** 1–2 days
**Depends on:** Phase 1

**Goal.** Single-hotel scoped retrieval against `hotel_kb` filtered by `hotel_id`. Foundation for every other 2x sub-phase (they all reuse this module's embeddings, search-body builder, and result contract).

**Deliverables.**
- [ ] `HotelKBRetriever` class with injectable `embed_fn` / `search_fn`
- [ ] Qdrant search body with `must: hotel_id == X` filter
- [ ] Optional `category_hint` (additive — `{hint, "overview"}`)
- [ ] Decision contract with enumerated `reason` strings (`empty_query`, `no_hotel_scope`, `no_match_above_threshold`, `retriever_error`)
- [ ] Unit suite (10 tests) and mock-Qdrant smoke harness
- [ ] Thin `GET /call_center/api/kb` endpoint on the test server

**Exit criteria.** Given 10 scoped queries against the seed hotels, the retriever returns relevant chunks with zero cross-hotel leakage on 100% of cases. All 10 unit tests green; mock smoke matches Gherkin scenarios.

Full design: [phase2-user-story.md](phase2-user-story.md) · [phase2-development-plan.md](phase2-development-plan.md).

---

### Phase 2b — Broad Discovery Search

**Branch:** `feat/VOX-rag-broad`
**Estimated duration:** 2 days
**Depends on:** 2a

**Goal.** No `hotel_id` is known. Return top 5–8 candidate **hotels** for a region + intent query (e.g. "luxury hotel with spa in Antalya").

**Deliverables.**
- [ ] `BroadHotelDiscovery` class (sibling of `HotelKBRetriever`)
- [ ] Qdrant search with `must: region == X` (no `hotel_id` filter); optional `must: activity_tags any [...]`
- [ ] Hit aggregation: group chunks by `hotel_id`, score each hotel = max chunk score, return top N hotels with their best-supporting chunk
- [ ] Decision contract: `{region, query, count, hotels: [{hotel_id, score, evidence_chunk}], reason}`
- [ ] Unit suite + mock smoke + thin `/api/kb/discover` endpoint

**Exit criteria.** Given 8 broad queries, ≥ 6 return a top-3 candidate set containing the expected hotel. No region leakage (zero hotels outside the requested region).

---

### Phase 2c — Compound AND Search

**Branch:** `feat/VOX-rag-compound`
**Estimated duration:** 2 days
**Depends on:** 2b

**Goal.** Multi-requirement discovery ("spa + scuba", "PADI dive centre AND kids club age 6–9"). Every requirement must be satisfied by at least one chunk; each requirement is scored separately and intersected at the hotel level.

**Deliverables.**
- [ ] `CompoundAndDiscovery` class that takes `requirements: list[str]` and runs N parallel broad searches
- [ ] Hotel-level intersection: hotel passes only if it has ≥ 1 supporting chunk for every requirement
- [ ] Per-requirement evidence chunks attached to each surviving hotel
- [ ] Graceful degradation: if intersection is empty, return best partial match with `reason: "partial_match_only"` and a `missing_requirements: [...]` field so the chat layer can ask a priority question
- [ ] Unit suite + mock smoke + thin `/api/kb/compound` endpoint

**Exit criteria.** Given 6 compound queries (including the canonical "luxury hotel with spa for my wife AND scuba diving for me"), all 6 either return a correctly-intersected hotel list or correctly flag `partial_match_only` with the right missing requirement.

---

### Phase 2d — Budget + Geo Filters

**Branch:** `feat/VOX-rag-filters`
**Estimated duration:** 1–2 days
**Depends on:** 2b

**Goal.** Metadata pre-filters applied before semantic search.

**Deliverables.**
- [ ] `price_tier` filter (`budget` / `mid` / `luxury` / `ultra_luxury`) wired into broad + compound search
- [ ] Geo filter — coordinates + radius (km) using Qdrant `geo_radius` payload filter
- [ ] Combined filter builder shared across 2b, 2c, 2d
- [ ] Unit suite covers each filter in isolation and combined
- [ ] Mock smoke + extension to existing `/api/kb/discover` query string

**Exit criteria.** Given 6 filter queries ("budget hotel in Side under €80", "beachfront hotel within 5 km of Antalya centre"), all 6 return only hotels matching the metadata constraints, regardless of semantic score.

---

### Phase 2e — Dual / Comparison Search

**Branch:** `feat/VOX-rag-dual`
**Estimated duration:** 1 day
**Depends on:** 2a

**Goal.** Two parallel scoped queries for side-by-side hotel comparison ("Rixos vs Kaya Palazzo for families").

**Deliverables.**
- [ ] `DualScopedRetriever` that fans out two `HotelKBRetriever.retrieve()` calls in parallel
- [ ] Aligned-by-category result shape: `{hotel_a: {...}, hotel_b: {...}, common_categories: [...]}`
- [ ] Unit suite + mock smoke + thin `/api/kb/compare?hotel_a=...&hotel_b=...&q=...` endpoint

**Exit criteria.** Given 4 comparison queries across the same category (children, dining, wellness), both hotels' chunks are returned in the same category alignment.

---

### Phase 2f — Ingestion, Confidence, Cache

**Branch:** `feat/VOX-rag-ingest`
**Estimated duration:** 2–3 days
**Depends on:** 2a–2e (final consolidation)

**Goal.** Production ingestion pipeline + confidence band handling + Redis cache layer. Closes out Phase 2.

**Deliverables — Ingestion.**
- [ ] Structured data parser for call center CSV/JSON format
- [ ] Semantic chunker — splits by category, not by character count
- [ ] Chunk categories: `overview`, `rooms`, `amenities`, `food_beverage`, `wellness`, `policies`, `children`, `activities`, `accessibility`, `location`, `atmosphere`, `packages`
- [ ] Chunk quality validator — rejects chunks below minimum detail threshold
- [ ] Payload builder — attaches all structured fields to each chunk
- [ ] Batch embedder using multilingual-e5-large
- [ ] Qdrant upsert with idempotency — re-running ingestion updates, does not duplicate
- [ ] Ingestion audit log — records chunk count, rejected chunks, embedding time per hotel
- [ ] Destination KB — separate collection, filtered by country + region

**Deliverables — Confidence & cache.**
- [ ] Confidence band handler applied to all 2a–2e retrievers:
  - ≥ 0.82 → answer directly
  - 0.65–0.81 → answer with caveat
  - 0.50–0.64 → acknowledge limited info
  - < 0.50 → do not answer, route to human
- [ ] Redis cache layer wrapping every retriever
- [ ] Cache key strategy: `qdrant:{mode}:{hotel_id|region}:{intent_hash}` TTL 6h
- [ ] Pre-computed activity index in Redis: `activity:{tag}:{region}` TTL 24h

**Exit criteria (= umbrella Phase 2 exit criteria).** Given 20 test queries across all 6 search modes, retrieval returns relevant chunks with correct confidence band handling on ≥ 80% of queries. Cache hit confirmed on repeated queries. Compound AND correctly enforces all requirements.

### Phase 2 — Reference Test Queries (used across 2a–2f)

```
# Scoped — 2a
"Does Rixos Belek have a hamam?"
"What time is check-in at Kaya Palazzo?"
"Is the Hilton Bodrum adults-only?"

# Broad — 2b
"Family resort near Belek with water park"
"Romantic boutique hotel in Istanbul"
"Luxury hotel with spa in Antalya"

# Compound AND — 2c
"Luxury hotel with spa for my wife and scuba diving for me"
"Resort with cenote diving AND on-site yoga"
"Hotel with PADI dive centre AND kids club age 6-9"

# Budget / Geo — 2d
"Budget hotel in Side under €80"
"Beachfront hotel within 5 km of Antalya centre"
"Something close to city centre in Istanbul"

# Comparison — 2e
"Rixos vs Kaya Palazzo for families"
```

---

## Phase 3 — Triage and Decomposition

**Branch:** `feat/VOX-triage-decomposition`
**Estimated duration:** 6–7 days

### Goal

The Claude-powered intelligence layers that sit before retrieval. Query decomposition produces the full structured output. Triage asks the right clarifying question — or passes through correctly. Source router sends each query to the right path.

### Deliverables

#### Query Decomposition

Extracts all fields from caller utterance + session context:

```
hotel_mention, city, region, district
intent, query_type, source_required
requirements[], requirements_logic, on_site_required[]
traveller_type, children_ages[], adults_count
budget_tier, budget_signal
vibe_preferences[], dietary_religious[], accessibility_needs[]
time_reference, returning_visitor, urgency
```

- [ ] Decomposition prompt engineered and validated on Turkish input
- [ ] All 27 query types correctly classified
- [ ] `source_required` field correctly set: `hotel_kb` / `destination_kb` / `web` / `[hotel_kb, web]`

#### Triage Layer

- [ ] Sufficiency assessment — priority hierarchy evaluation
- [ ] Blocking gap detection — geography missing, intent unclear
- [ ] One-question-per-turn logic
- [ ] Two-turn maximum rule — proceed after 2 clarifications regardless
- [ ] Session update after each clarification answer
- [ ] Triage bypasses correctly for self-sufficient queries

**Triage priority hierarchy:**

| Priority | Need | Blocking |
|---|---|---|
| 1 | Geography (country/region) | Yes |
| 2 | Hotel vs recommendation intent | Yes |
| 3 | Non-negotiable requirement (halal, accessibility) | Sometimes |
| 4 | Traveller type | No |
| 5 | Budget | No |
| 6 | Vibe / atmosphere | No |

#### Source Router

- [ ] Deterministic routing decision tree (no LLM in the router — pure logic)
- [ ] Five paths: Scoped Qdrant / Broad Qdrant / Destination KB / Web / Hybrid
- [ ] Hybrid trigger: hotel known + query requires external info
- [ ] Escalation triggers fire before router runs

#### Classifier

- [ ] Escalation detection on first utterance
- [ ] Triggers: live complaint, medical/safety, urgency, booking intent, post-booking

#### Session Management

- [ ] Redis session object built and maintained across turns
- [ ] Turn history appended per query
- [ ] Hotel switches detected and `active_hotel_id` updated
- [ ] Session survives up to 30-minute gap (chat mode TTL)

### Test Cases

```
# Self-sufficient — triage passes through
"Rixos Belek'te çocuk kulübü var mı?"
→ No triage question. Decompose directly.

# Missing geography — triage asks
"Aile tatili için otel arıyorum."
→ Triage: "Nereye gitmek istiyorsunuz?"

# Family + children ages — targeted triage
"Antalya'da su sporları yapabileceğim bir yer arıyorum çocuklarımla."
→ Triage: "Çocuklarınız kaç yaşında?"

# Web query — triage asks geography
"Aralıkta festival var mı?"
→ Triage: "Hangi destinasyonu düşünüyorsunuz?"

# Escalation — no triage, immediate
"Oteldeyim ve odama giremiyorum."
→ Escalate immediately.

# Two-turn maximum
Turn 1: system asks destination → caller answers
Turn 2: system asks children ages → caller answers
Turn 3: even if more info missing → proceed with what we have
```

### Exit Criteria

Given 30 test queries across all types: decomposition correctly identifies `query_type` and `source_required` on ≥ 90%. Triage asks the right question or passes through correctly on ≥ 90%. Escalation triggers fire on 100% of escalation cases. Two-turn maximum enforced.

---

## Phase 4 — Web Search Layer

**Branch:** `feat/VOX-web-search`
**Estimated duration:** 5–6 days

### Goal

Live web search integrated for the four dynamic query types. Hybrid path working correctly. All web results handled under quality and trust constraints.

### Deliverables

#### Web Search Integration

- [ ] Web search API client (Brave Search or SerpAPI — to be confirmed, Open Question #10)
- [ ] Query construction per type:

| Type | Pattern | Example |
|---|---|---|
| Events | `{event_type} {city} {month} {year}` | `festivals Playa del Carmen December 2026` |
| Local operators | `{category} near {district} {city}` | `PADI dive schools near Belek Antalya` |
| Weather | `weather forecast {city} {month}` | `weather Bodrum December 2026` |
| Practical info | `{attraction} opening hours {year}` | `Topkapi Palace opening hours 2026` |

- [ ] Result parser — extract relevant content from search results
- [ ] Quality assessor — identify low-quality or irrelevant results
- [ ] Language selector — Turkish for Turkey, English for international destinations

#### Hybrid Path

- [ ] Qdrant check first on `activity_tags` payload
- [ ] Web fallback triggered only if Qdrant returns no match or low confidence
- [ ] Result synthesiser — combines hotel KB answer with web operator results

#### Trust and Quality Constraints

- [ ] Grounding rule enforced — Claude answers only from retrieved web content
- [ ] Source and date citation appended where available
- [ ] Operator recommendation caveat: *"I'd recommend checking reviews before booking"*
- [ ] No-result handler — acknowledge gap, offer human routing
- [ ] Safety filter — web results never used for medical, legal, or safety content

#### Redis Caching for Web

- [ ] `web:events:{region}:{year_month}` TTL 12h
- [ ] `web:operators:{category}:{district}` TTL 24h
- [ ] `web:weather:{city}:{date}` TTL 4h
- [ ] `web:practical:{attraction_id}:{year_month}` TTL 24h

### Test Queries

```
# Events
"Playa del Carmen yakınında Aralık ayında festival var mı?"
→ Web search: "festivals Playa del Carmen December 2026"
→ Return with date caveat

# Local operators — no hotel KB match
"Rixos Belek yakınında dalış okulu var mı?"
→ Qdrant: Rixos has no dive centre
→ Web: "PADI dive schools near Belek Antalya"
→ Synthesised answer with recommendation caveat

# Weather
"Bodrum'da Aralık ayında hava nasıl?"
→ Web: "weather Bodrum December 2026"
→ Return with source citation

# Practical info
"Topkapı Sarayı Pazartesi günleri açık mı?"
→ Web: "Topkapi Palace opening hours 2026"
→ Return with source and date

# Cache hit — second caller same query
→ Redis hit, no web call, instant response
```

### Exit Criteria

Given 10 web query tests across all four types: ≥ 70% return usable answers. 100% of web-sourced answers include appropriate caveat. Hybrid path correctly uses hotel KB first. Cache confirmed working on repeated queries. Zero web results used for escalation-type content.

---

## Phase 5 — Full Chat Assembly

**Branch:** `feat/VOX-chat-assembly`
**Estimated duration:** 6–8 days

### Goal

All components assembled into a working multi-turn chat interface. Real conversations in Turkish covering all query types work correctly end-to-end.

### Deliverables

#### Progressive Narrowing

- [ ] Result set assessor after every Qdrant query
- [ ] Trigger logic: 4+ strong matches → narrowing, 2–3 → present with differences, 1 → proceed
- [ ] Differentiating question selector — priority-ordered:
  1. Budget (if candidates span tiers)
  2. Children's ages (if family, ages unknown)
  3. Beach vs city (if candidates split)
  4. Scale preference (boutique vs large resort)
  5. Priority requirement (if compound AND failed)
- [ ] Re-query with tighter filter after caller answers
- [ ] Graceful degradation for compound AND failures — present best partial with honest caveat

#### Conversation Assembly

- [ ] Full multi-turn loop: triage → decompose → route → retrieve → assess → narrow → generate
- [ ] Pronoun resolution — "the hotel we just discussed" resolves from session `active_hotel_id`
- [ ] Hotel switch detection — "what about the Marriott instead?" updates session correctly
- [ ] Returning visitor tone adjustment — peer-level conversation, skip tourist-brochure content
- [ ] Status message during web search (chat equivalent of voice filler phrase): `"Checking current information..."`

#### Chat Interface

- [ ] Simple web UI — not for production, for testing
- [ ] Chat window with message history
- [ ] Debug panel showing: active session state, source used, chunks retrieved, confidence scores
- [ ] Language toggle for test input (Turkish / English)
- [ ] Conversation reset button

#### End-to-End Test Suite

- [ ] 27 query types covered with at least one test case each
- [ ] Multi-turn conversations: hotel switch mid-call, follow-up questions, triage flow
- [ ] Turkish-language test cases for all high-frequency patterns
- [ ] Edge cases: no hotel match, compound AND fail, web returns nothing, escalation mid-conversation

### Conversation Test Scripts

```
# Test script 1 — Hotel facts + follow-up
User: "Rixos Belek'te havuz var mı?"
User: "Çocuklar için de uygun mu?"
User: "Kaç yaşından itibaren?"
→ Session maintains hotel across all turns. Children chunk retrieved.

# Test script 2 — Triage → recommendation → narrowing
User: "Aile tatili için otel arıyorum."
System: "Nereye gitmek istiyorsunuz?" ← triage
User: "Antalya"
System: "Çocuklarınız kaç yaşında?" ← triage
User: "4 ve 8 yaşındalar"
→ Broad search with kids_age_min ≤ 4
→ If 4+ results → narrowing question
→ Final: top 2 recommendations

# Test script 3 — Hybrid
User: "Rixos Belek'te dalış imkanı var mı?"
→ Qdrant check: no dive centre in activity_tags
→ Web: PADI operators near Belek
→ Synthesised answer with caveat

# Test script 4 — Compound AND + graceful degradation
User: "Cenote dalışı ve resort içi yoga olan bir yer istiyorum."
→ Compound AND search
→ No perfect match found
→ Graceful degradation: best partial match + priority question

# Test script 5 — Escalation
User: "Rezervasyonum var ama iptal etmek istiyorum."
→ Escalation detected immediately. No retrieval attempted.
```

### Exit Criteria

Real conversation covering ≥ 10 query types with a Turkish-speaking tester completes without errors and returns accurate answers. Debug panel confirms correct source routing on all turns. Escalation triggers fire correctly. Session state persists correctly across a 10-turn conversation.

---

## Phase 6 — Voice Pipeline

**Branch:** `feat/VOX-voice-pipeline`
**Estimated duration:** 7–10 days
**Prerequisite:** Phase 5 signed off

### Goal

Add voice edges to the proven chat pipeline. All RAG logic remains unchanged — only the input (STT) and output (TTS) layers change.

### What Changes from Chat

```
Chat:  HTTP request (text) → pipeline → HTTP response (text)

Voice: Phone call (audio) → Gladia STT → pipeline → Chirp 3 HD → audio
                                ↑                          ↑
                          only these two edges are new
```

The entire RAG pipeline — triage, decomposition, router, Qdrant, web, Redis, Claude — runs identically.

### Deliverables

- [ ] Gladia Solaria-1 STT integration in Pipecat
- [ ] Validate Gladia on Antalya-region Turkish accent with real audio samples
- [ ] Validate Turkish-English code-switching: *"Poolda yer var mı?"*
- [ ] Pipecat orchestration layer wrapping the chat pipeline
- [ ] Telnyx PSTN webhook → spawn bot on incoming call
- [ ] Chirp 3 HD TTS with Turkish voice map
- [ ] Filler phrase audio triggered on web search path
- [ ] Session TTL adjusted for call duration (vs chat session)
- [ ] Call termination handling — session cleanup, transcript logging
- [ ] Silero VAD tuning for Turkish speech patterns
- [ ] End-to-end voice test: real phone call in Turkish, 5 query types

### Voice-Specific Edge Cases to Test

```
# Code-switching
"Poolda sunbed var mı?" (Turkish + English hotel words)
→ Gladia transcribes correctly
→ Pipeline processes as Turkish

# Suffix on brand name
"Rixosta rezervasyon var mı?"
→ Gladia transcribes "Rixosta"
→ Elasticsearch strips -ta → matches Rixos Premium Belek

# Filler phrase timing
Query routed to web search
→ Filler phrase starts immediately
→ Web search runs async
→ Answer ready before filler ends → seamless

# Call drop mid-conversation
→ Session preserved for 10 minutes
→ If caller redials, session context available
```

### Exit Criteria

Real phone call in Turkish, answered by Voxtera, successfully handles ≥ 5 query types from different categories (hotel fact, recommendation, event/web, hybrid, escalation). Response latency < 3s for cached answers, < 5s for live web queries. No STT errors on standard Antalya Turkish speech.

---

## Open Questions Blocking Development

The following must be resolved before the indicated phase begins:

| # | Question | Blocks | Owner |
|---|---|---|---|
| 1 | What format will call center provide hotel data? | Phase 0 ingestion | Call center |
| 2 | Who writes Turkish narrative chunk text? | Phase 2 ingestion | TBD |
| 3 | Which web search API — Brave Search or SerpAPI? | Phase 4 | Voxtera |
| 4 | Does call center want self-serve data update portal? | Phase 2 scope | Call center |
| 5 | What reservation system for booking handoffs? | Phase 6 scope | Call center |
| 6 | Should call center approve trusted source whitelist for web? | Phase 4 | Both |
| 7 | Acceptable end-to-end latency target? | Phase 5 exit criteria | Both |
| 8 | Validate Gladia on Antalya accent — when? | Phase 6 start | Voxtera |

---

## Summary

| Phase | Branch | Duration | Exit Criteria |
|---|---|---|---|
| 0 — Infrastructure | `feat/VOX-rag-foundation` | 3–4 days | Insert → embed → retrieve works |
| 1 — Hotel Resolver | `feat/VOX-hotel-resolver` | 4–5 days | ≥ 90% resolution on Turkish mentions |
| 2a — Scoped search | `feat/vox-kb-retrieval` | 1–2 days | Zero cross-hotel leakage, 10/10 scoped queries |
| 2b — Broad discovery | `feat/VOX-rag-broad` | 2 days | ≥ 6/8 broad queries surface expected hotel |
| 2c — Compound AND | `feat/VOX-rag-compound` | 2 days | 6/6 compound queries intersect or flag partial |
| 2d — Budget + Geo filters | `feat/VOX-rag-filters` | 1–2 days | 6/6 filtered queries respect metadata constraints |
| 2e — Dual / comparison | `feat/VOX-rag-dual` | 1 day | 4/4 comparison queries category-aligned |
| 2f — Ingestion + confidence + cache | `feat/VOX-rag-ingest` | 2–3 days | ≥ 80% relevant retrieval across all modes + cache hits |
| 3 — Triage + Decomposition | `feat/VOX-triage-decomposition` | 6–7 days | Correct routing on 30 test queries |
| 4 — Web Search | `feat/VOX-web-search` | 5–6 days | ≥ 70% web queries answered with caveats |
| 5 — Chat Assembly | `feat/VOX-chat-assembly` | 6–8 days | Real 10-query Turkish conversation clean |
| 6 — Voice Pipeline | `feat/VOX-voice-pipeline` | 7–10 days | Real phone call, 5 query types end-to-end |

**Total estimated duration:** 39–50 days
**Voice never starts until Phase 5 is signed off.**

---

*Voxtera RAG Development Plan v1.0 · June 2026 · Confidential*
