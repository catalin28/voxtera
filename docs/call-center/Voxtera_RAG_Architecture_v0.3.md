# VOXTERA
## Tourism Call Center Voice Agent
### RAG System Architecture

**Version:** 0.3  
**Date:** June 2026  
**Status:** Confidential — Internal Architecture Document

> **Changes from v0.2:** Added Web Search as a fourth knowledge source (Section 10). Updated pipeline flow to show five retrieval paths. Updated query classifier taxonomy with four new web-native query types. Added hybrid Qdrant + Web pattern, latency mitigation strategy, quality and trust constraints for web results, and Redis caching for web results.

---

## 1. System Overview

This document defines the Retrieval-Augmented Generation (RAG) architecture for a Voxtera deployment serving a Turkish tourism call center. The system handles inbound voice calls from tourists seeking hotel and destination information across a catalogue of 5,000–20,000 properties, primarily in Turkey.

> **Pilot Scope:** Phase 1 targets the Antalya corridor (Belek, Kemer, Side, Alanya) and Istanbul. Language: Turkish-first, 90%+ of callers speak Turkish. Approximately 5,000 hotels at launch.

### 1.1 Three Knowledge Sources

The system draws from three distinct knowledge sources. Each source has a different nature, update frequency, and retrieval mechanism. Understanding which source owns which question type is the foundation of the architecture.

| Source | What It Contains | Nature | Retrieval |
|---|---|---|---|
| Hotel KB (Qdrant) | Amenities, policies, activities, atmosphere, F&B for each hotel | Static — curated, quality-controlled | Vector semantic search scoped by `hotel_id` |
| Destination KB (Qdrant) | Country and city knowledge — culture, geography, general weather patterns, major landmarks | Semi-static — updated periodically | Vector semantic search filtered by region |
| Web Search | Events, festivals, local operators, current conditions, real-time practical info, small businesses | Dynamic — live, uncontrolled quality | Live search at query time, cached in Redis |

### 1.2 The Core Challenge

A tourism call center RAG system must:

- Identify which hotel the caller is asking about from natural speech before any retrieval
- Gather missing context conversationally when the first utterance is too vague for useful retrieval
- Know which of three knowledge sources owns a given question — and route accordingly
- Combine sources when a complete answer requires both stored knowledge and live web data
- Handle Turkish morphology — suffixes, agglutination, code-switching with English hotel terms
- Respond in under 300ms for cached answers, under 3 seconds for live web queries
- Know when not to retrieve at all — and route to a human immediately

### 1.3 Technology Stack

| Layer | Technology | Role |
|---|---|---|
| STT | Gladia Solaria-1 | Speech to text, Turkish + code-switching |
| Triage | Claude (lightweight) | Context sufficiency assessment, one clarifying question if needed |
| Hotel Resolver | Elasticsearch | Fuzzy hotel name matching, Turkish analyzer |
| Vector Store | Qdrant | Semantic retrieval — Hotel KB and Destination KB |
| Web Search | Search API (e.g. Brave Search / SerpAPI) | Live internet queries for dynamic information |
| Cache / Session | Redis | Result caching for all three sources, session state |
| Embedding Model | multilingual-e5-large | Turkish-first vector embeddings |
| LLM | Claude Sonnet (claude-sonnet-4-6) | Decomposition, routing, synthesis, answer generation |
| TTS | Google Chirp 3 HD | Turkish voice output |
| Telephony | Telnyx | PSTN inbound calls |
| Pipeline | Pipecat | Real-time audio orchestration |

---

## 2. High-Level Query Flow

Every inbound call passes through the same sequence of stages. The router now has five paths after decomposition — three retrieval paths, one hybrid path combining Qdrant and web, and one escalation path.

```
Caller dials Telnyx number
        │
        ▼
Gladia Solaria-1  ──  STT + language detection
        │
        ▼
Stage 1 ── CLASSIFIER
        ├── ESCALATE ──────────────────── Human agent immediately
        └── RETRIEVE / SEARCH
                │
                ▼
Stage 2 ── TRIAGE LAYER
        Assess context sufficiency
        Ask ONE question if critical gap exists
        Max 2 clarification turns then proceed
                │
                ▼
Stage 3 ── QUERY DECOMPOSITION
        Full structured extraction from enriched input
        Outputs: hotel_mention, location, intent,
        requirements, traveller_type, children_ages,
        budget, vibe, dietary, accessibility,
        query_type, SOURCE_REQUIRED
                │
                ▼
Stage 4 ── SOURCE ROUTER
        Determines which knowledge source(s) to use
        │
        ├─ Path 1: Scoped Qdrant ──── hotel_id known + factual
        │
        ├─ Path 2: Broad Qdrant ───── recommendation + location
        │
        ├─ Path 3: Destination KB ─── general destination question
        │
        ├─ Path 4: Web Search ──────── dynamic / real-time / local ops
        │
        └─ Path 5: Hybrid ──────────── Qdrant first, web to fill gap
                │
                ▼
Stage 5 ── REDIS CACHE CHECK  (all paths)
        ├── HIT  ────────────────── skip to Stage 8
        └── MISS ──► Stage 6
                │
                ▼
Stage 6 ── RETRIEVAL EXECUTION
        Path 1/2/3: Qdrant semantic search
        Path 4:     Web search API → parse results
        Path 5:     Qdrant first → web fills gap
        All results cached in Redis
                │
                ▼
Stage 7 ── RESULT ASSESSMENT
        Single clear answer? ──────── proceed
        Multiple candidates? ───────── Progressive Narrowing
        Web results low quality? ───── caveat or escalate
                │
                ▼
Stage 8 ── CLAUDE ANSWER SYNTHESIS
        Grounds answer in retrieved content only
        Cites source type when relevant
        Adds freshness caveats for web and old chunks
        Adjusts tone for returning visitor
                │
                ▼
Chirp 3 HD  ──  TTS → caller hears response
```

---

## 3. Triage Layer

The Triage Layer runs after the classifier confirms retrieval is appropriate, and before decomposition. Its job is to identify the single most critical context gap and ask one focused question if that gap would make retrieval useless.

> **Design Principle — Triage Is Not a Form:** The triage layer may ask at most ONE question per turn and at most TWO clarification turns across the entire call. After two clarifications it proceeds regardless and makes assumptions explicit in the response.

### 3.1 Sufficiency Assessment Priority

| Priority | Context Need | Blocking? | If Missing |
|---|---|---|---|
| 1 | Geography — country or region | Yes | Ask destination before anything else |
| 2 | Hotel vs recommendation intent | Yes | Ask 'specific hotel or looking for suggestions?' |
| 3 | Non-negotiable requirement | Sometimes | Ask only if query type strongly signals it |
| 4 | Traveller type | No | Gather progressively through conversation |
| 5 | Budget | No | Ask only if vague query returns too wide a range |
| 6 | Vibe / atmosphere | No | Semantic search handles this implicitly |

### 3.2 Triage Examples

#### Example A — Self-sufficient, no triage needed

**Caller:** *"Rixos Belek'te çocuk kulübü var mı?"* (Does Rixos Belek have a kids club?)

Hotel named, question specific. Zero gaps. Passes immediately to decomposition.

#### Example B — Geography missing

**Caller:** *"Aile tatili için otel arıyorum."* (I'm looking for a hotel for a family holiday.)

**Triage asks:** *"Nereye gitmek istiyorsunuz?"* (Where are you looking to travel?)

#### Example C — Web query, geography missing

**Caller:** *"Aralıkta festival var mı?"* (Are there any festivals in December?)

Web query detected. No geography. Web search across no location is useless. Triage asks destination first — same rule applies regardless of whether the query will go to Qdrant or the web.

**Triage asks:** *"Hangi destinasyonu düşünüyorsunuz?"* (Which destination are you thinking of?)

---

## 4. Query Classification

The classifier makes two decisions: whether to retrieve or escalate, and which source type the query requires. The source type classification feeds directly into the Source Router at Stage 4.

### 4.1 Full Query Type Taxonomy

#### Hotel KB Queries — Path 1 or 2 (Qdrant)

| # | Type | Example |
|---|---|---|
| 1 | Hotel-specific fact | Does Rixos Belek have a hamam? |
| 2 | Activity recommendation | Family resort with water park near Belek |
| 3 | Hotel comparison | Rixos vs Kaya Palazzo for families |
| 4 | Budget filtering | Good hotel in Bodrum under €100 |
| 5 | Proximity / location | Something close to the beach in Side |
| 6 | Vibe / atmosphere | Something romantic and boutique |
| 7 | Group / event | Conference for 80 people, need AV |
| 8 | Dietary / religious | Helal yemek var mi? |
| 9 | Accessibility | Wheelchair accessible rooms? |
| 10 | Multi-destination planning | 3 days Istanbul then 5 days Antalya |

#### Destination KB Queries — Path 3 (Qdrant, destination collection)

| # | Type | Example |
|---|---|---|
| 11 | General destination info | What is Cappadocia known for? |
| 12 | Stable weather patterns | What is the weather like in Antalya in July? |
| 13 | Visa and entry requirements | Do Turkish citizens need a visa for the Maldives? |
| 14 | Cultural etiquette | What should I wear at a hotel in Dubai? |
| 15 | Major landmarks and museums | What are the main sights in Istanbul? |

#### Web Search Queries — Path 4 (Live Web)

| # | Type | Example |
|---|---|---|
| 16 | Events and festivals | Are there any festivals near Playa del Carmen in December? |
| 17 | Local operators and small businesses | Dive shops near Riviera Maya / cooking class near Side |
| 18 | Current / forecast conditions | What is the weather forecast for Bodrum next week? |
| 19 | Real-time practical info | Is Topkapi Palace open on Mondays? Current entry price? |

#### Hybrid Queries — Path 5 (Qdrant + Web)

| # | Type | Example |
|---|---|---|
| 20 | Hotel + nearby activity | Are there dive shops near Rixos Belek? |
| 21 | Hotel + local event | Are there any markets near my hotel this weekend? |
| 22 | Hotel + current conditions | Is the sea warm enough to swim near the Hilton Bodrum in October? |
| 23 | Hotel gap + local operator | The hotel has no spa — is there one nearby? |

#### Escalation — Human Agent

| # | Type | Example |
|---|---|---|
| 24 | Booking intent | I want to book for next weekend |
| 25 | Post-booking query | I need to cancel my reservation |
| 26 | Live complaint | I am at the hotel and my room is not ready |
| 27 | Urgent / distress | I land in 2 hours and have no hotel |

---

## 5. Source Router

The Source Router sits after decomposition and determines which retrieval path to take. It replaces implicit routing logic with an explicit, auditable decision process.

### 5.1 Routing Decision Tree

```
Is this an escalation trigger?
  YES → human agent immediately (Path: Escalate)
  NO  → continue
          │
          ▼
Is the query time-sensitive or about real-time data?
(events, forecasts, current conditions, live prices)
  YES → is geography known?
           YES → Web Search (Path 4)
           NO  → triage should have caught this — ask destination
  NO  → continue
          │
          ▼
Is the query about local operators or small businesses?
(dive shops, tour companies, independent restaurants)
  YES → is hotel known?
           YES → Hybrid: Qdrant check first, web fills gap (Path 5)
           NO  → Web Search with location filter (Path 4)
  NO  → continue
          │
          ▼
Is the query about a specific hotel?
  YES → hotel_id resolved? → Scoped Qdrant (Path 1)
         hotel ambiguous?  → Elasticsearch resolve first
  NO  → continue
          │
          ▼
Is the query a recommendation across multiple hotels?
  YES → Broad Qdrant with region + tag filters (Path 2)
  NO  → continue
          │
          ▼
Is the query about destination-level knowledge?
(culture, geography, major sights, stable weather)
  YES → Destination KB (Path 3)
  NO  → default to Broad Qdrant (Path 2)
```

### 5.2 Hybrid Path Trigger

The Hybrid path (Path 5) activates when a query involves a known hotel but the answer requires information outside the hotel's own KB. The pattern is always: Qdrant first, web only if Qdrant cannot fully answer.

| Trigger | Qdrant Checks First | Web Fills |
|---|---|---|
| 'Are there dive shops near my hotel?' | Does hotel have own dive centre? (activity_tags) | Local PADI operators within X km if no |
| 'Any markets near here this weekend?' | Does hotel KB mention a local market? | Live search for weekend markets in district |
| 'Is there a spa near the hotel?' | Does hotel have own spa? (wellness chunk) | Nearby spa operators if hotel has none |
| 'What's the sea temperature near Hilton Bodrum?' | Hotel location + beach details | Current sea temperature for that coastline |

---

## 6. Elasticsearch — Hotel Resolver

Elasticsearch serves one purpose: resolving a natural-language hotel mention to a canonical `hotel_id`. It is only called when decomposition identifies a `hotel_mention`. If no hotel is mentioned, Elasticsearch is skipped entirely.

### 6.1 Why Elasticsearch Over Postgres

- Native Turkish language analyzer — agglutinative morphology, suffix stripping, stemming without custom code
- Fuzzy matching via Levenshtein distance — tolerates mispronunciations and partial names
- Multi-field weighted search — hotel name scores higher than city, city higher than region
- Turkish character normalisation — İ/i, I/ı, Ş/ş, Ğ/ğ handled correctly

### 6.2 The Turkish Morphology Problem

```
Caller says        Suffix   Meaning            Stripped root
──────────────────────────────────────────────────────────
"Rixosta"          -ta      at Rixos           "Rixos"
"Rixosa git"       -a       to Rixos           "Rixos"
"Rixostan"         -tan     from Rixos         "Rixos"
"Rixosun havuzu"   -un      Rixos's pool       "Rixos"
"Kaya'da"          -da      at Kaya            "Kaya"
```

### 6.3 Resolution Thresholds

| Score | Action |
|---|---|
| ≥ 0.85 | Auto-resolve. Lock `hotel_id`. Store in Redis session. |
| 0.55 – 0.84 | Return top 3 candidates. Claude asks for clarification. |
| < 0.55 | No match. Proceed without hotel scope — broad or web search. |

---

## 7. Qdrant — Semantic Retrieval

Qdrant stores knowledge chunks for hotels and destinations. It is the meaning layer — where natural language is matched against descriptions using vector similarity. It covers everything that can be known in advance and curated for quality.

### 7.1 Two Collections

| Collection | Contents | Primary Filter |
|---|---|---|
| `hotel_kb` | Hotel knowledge — amenities, policies, activities, F&B, atmosphere | `hotel_id` |
| `destination_kb` | Country and city knowledge — culture, weather patterns, visas, major landmarks | `country` + `region` |

### 7.2 Chunk Taxonomy — hotel_kb

| Category | Content | Key Payload Fields |
|---|---|---|
| `overview` | Property summary, stars, architecture, character | `stars`, `year_built`, `year_renovated` |
| `rooms` | Room types, bed configs, views, sizes | `room_types[]`, `max_occupancy` |
| `amenities` | Pools, gym, sports — detail with hours and rules | `activities[]`, `amenity_tags[]` |
| `food_beverage` | Restaurants, bars, room service, dietary options | `halal`, `alcohol`, `pork_free`, `board_basis` |
| `wellness` | Spa, hamam, fitness classes, certifications | `hamam`, `spa_rooms`, `certifications[]` |
| `policies` | Cancellation, pets, children, smoking, check-in/out | `pets_allowed`, `adults_only`, `checkin_time` |
| `children` | Mini club ages, baby club, teen club, babysitting | `kids_age_min`, `kids_age_max`, `babysitting` |
| `activities` | Water sports, excursions, dive centres, golf, tennis | `activity_tags[]`, `padi_certified` |
| `accessibility` | Wheelchair access, pool lift, elevator | `wheelchair_rooms`, `pool_lift`, `elevator` |
| `location` | Beach distance, airport distance, neighbourhood | `coordinates`, `beach_distance_m` |
| `atmosphere` | Vibe, guest profile, nationality mix | `vibe_tags[]`, `primary_segments[]` |
| `packages` | All-inclusive tiers, honeymoon, family packages | `all_inclusive_tier`, `package_types[]` |

### 7.3 Confidence Thresholds

| Score | Claude Behaviour |
|---|---|
| ≥ 0.82 | Answer directly and confidently. |
| 0.65 – 0.81 | Answer with light caveat — suggest confirming with hotel. |
| 0.50 – 0.64 | Acknowledge limited information. Offer human routing. |
| < 0.50 | Do not answer. Route to human or trigger web search fallback. |

---

## 8. Redis — Cache and Session Layer

Redis caches results from all three knowledge sources and maintains conversation state across every turn of a call. Web search results are cached here with shorter TTLs than Qdrant results, given their time-sensitive nature.

### 8.1 Data Stored in Redis

| Data Type | Key Pattern | TTL | Notes |
|---|---|---|---|
| Session state | `session:{call_id}` | Call + 10 min | All accumulated context — location, hotel, traveller type, requirements |
| Hotel resolution | `resolved:{call_id}:{mention_hash}` | Call duration | Resolved `hotel_id` for this call |
| Qdrant — hotel results | `qdrant:{hotel_id}:{intent_hash}` | 6 hours | Cached hotel KB chunks |
| Qdrant — destination results | `qdrant:dest:{region}:{intent_hash}` | 24 hours | Destination KB results — change less frequently |
| Web search results | `web:{query_hash}:{date}` | 4 hours | Web results keyed by query + date — stale after 4 hours |
| Pre-computed activity index | `activity:{tag}:{region}` | 24 hours | Hotel lists for common activity + location pairs |
| Web — events cache | `web:events:{region}:{month}` | 12 hours | Festival/event results by region and month |
| Web — operator cache | `web:operators:{category}:{district}` | 24 hours | Local operator results by type and area |

### 8.2 Session State Structure

```json
{
  "call_id":             "telnyx_abc123",
  "language":            "tr",
  "triage_turns_used":   1,
  "location": {
    "country":  "TR",
    "region":   "antalya",
    "district": "belek"
  },
  "active_hotel_id":     "rixos_premium_belek",
  "traveller_type":      "family",
  "children_ages":       [6, 9],
  "returning_visitor":   false,
  "requirements":        ["water_sports", "kids_club"],
  "vibe_preferences":    ["family", "beachfront"],
  "budget_tier":         "upper",
  "non_negotiables":     ["halal"],
  "turn_history": [
    { "turn": 1, "source": "hotel_kb",  "hotel_id": "rixos_premium_belek", "intent": "amenities" },
    { "turn": 2, "source": "hotel_kb",  "hotel_id": "rixos_premium_belek", "intent": "children" },
    { "turn": 3, "source": "web",       "query": "dive shops near belek antalya", "intent": "local_operators" }
  ]
}
```

---

## 9. Query Decomposition

After triage, Claude performs full structured decomposition of the enriched input. The decomposition now includes a `source_required` field that feeds directly into the Source Router.

### 9.1 Full Extracted Field Set

| Field | Type | Example |
|---|---|---|
| `hotel_mention` | string \| null | `'Rixosta'`, `'o büyük Hilton'`, `null` |
| `city` | string \| null | `'Antalya'`, `'Istanbul'` |
| `region` | string \| null | `'antalya'`, `'aegean_coast'` |
| `district` | string \| null | `'Belek'`, `'Lara'`, `'Sultanahmet'` |
| `intent` | enum | `amenities / activities / food / policy / atmosphere / comparison / recommendation / event / local_operator / weather / practical_info` |
| `query_type` | enum | `scoped / broad / compound / comparison / destination / web / hybrid / escalate` |
| `source_required` | enum[] | `['hotel_kb']` / `['destination_kb']` / `['web']` / `['hotel_kb', 'web']` |
| `requirements` | string[] | `['cenote_diving', 'yoga_onsite', 'sea_view']` |
| `requirements_logic` | enum | `AND` / `OR` |
| `on_site_required` | bool[] | `[false, true]` — per requirement |
| `traveller_type` | enum \| null | `solo / couple / family / group / corporate` |
| `children_ages` | int[] \| null | `[6, 9]` |
| `adults_count` | int \| null | `2` |
| `budget_tier` | string \| null | `budget / mid / upper / luxury` |
| `budget_signal` | string \| null | `'ekonomik'`, `'bütçem kısıtlı'` |
| `vibe_preferences` | string[] | `['romantic', 'boutique']` |
| `dietary_religious` | string[] | `['halal', 'vegan']` |
| `accessibility_needs` | string[] | `['wheelchair', 'pool_lift']` |
| `time_reference` | string \| null | `'December'`, `'next weekend'`, `'this week'` |
| `returning_visitor` | bool | `true` if caller signals prior experience |
| `urgency` | enum | `normal / urgent / immediate_escalation` |

### 9.2 Decomposition Example — Hotel Specific (Path 1)

**Caller:** *"Rixos Belek'te çocuk kulübü var mı, 6 yaşındaki çocuğum için?"*

```json
{
  "hotel_mention":    "Rixos Belek",
  "intent":           "children",
  "query_type":       "scoped",
  "source_required":  ["hotel_kb"],
  "requirements":     ["kids_club"],
  "children_ages":    [6],
  "traveller_type":   "family"
}
```

### 9.3 Decomposition Example — Activity Only, No Hotel (Path 2)

**Caller:** *"Antalya'da çocuklarımla su sporları yapabileceğim bir yer arıyorum."* — after triage has gathered `children_ages: [6, 9]`

```json
{
  "hotel_mention":       null,
  "region":              "antalya",
  "intent":              "recommendation",
  "query_type":          "broad",
  "source_required":     ["hotel_kb"],
  "requirements":        ["water_sports", "kids_club"],
  "requirements_logic":  "AND",
  "on_site_required":    [true, true],
  "traveller_type":      "family",
  "children_ages":       [6, 9]
}
```

### 9.4 Decomposition Example — Event Query (Path 4 — Web)

**Caller:** *"Playa del Carmen yakınında Aralık ayında festival var mı?"* (Are there any festivals near Playa del Carmen in December?)

```json
{
  "hotel_mention":     null,
  "city":              "Playa del Carmen",
  "region":            "riviera_maya_mexico",
  "intent":            "event",
  "query_type":        "web",
  "source_required":   ["web"],
  "time_reference":    "December",
  "returning_visitor": false
}
```

### 9.5 Decomposition Example — Hybrid (Path 5)

**Caller:** *"Rixos Belek yakınında dalış okulu var mı?"* (Are there any dive schools near Rixos Belek?)

```json
{
  "hotel_mention":    "Rixos Belek",
  "region":           "antalya",
  "district":         "belek",
  "intent":           "local_operator",
  "query_type":       "hybrid",
  "source_required":  ["hotel_kb", "web"],
  "requirements":     ["scuba_diving"]
  // hotel_kb checked first: does Rixos Belek have dive centre?
  // if not → web: 'PADI dive schools near Belek Antalya'
}
```

---

## 10. Web Search Layer

The Web Search Layer is activated by the Source Router for query types 16–19 (pure web) and 20–23 (hybrid). It handles all information that is too dynamic, too time-sensitive, or too granular for a curated knowledge base to reliably maintain.

> **Product Positioning — The Web Layer Is a Differentiator:** When a caller asks about scuba diving and no hotel in the catalogue offers it, a lesser system says *"I don't have that information."* Voxtera says *"None of the hotels we work with have their own dive centre, but there are several PADI-certified operators near Belek — let me find you the options."* The web layer turns dead ends into useful answers. This is worth communicating to the call center client as a named capability.

### 10.1 The Four Web Query Types

#### Type 1 — Events and Festivals

Questions about scheduled events, cultural festivals, markets, concerts, or seasonal activities near a location. These are inherently time-sensitive — a destination KB entry about festivals will be stale within weeks.

- *'Are there any festivals near Playa del Carmen in December?'*
- *'Is there a market in Bodrum this weekend?'*
- *'What's happening in Istanbul in the next two weeks?'*

> **Web Search Strategy:** Query construction: `{event type} {district/city} {month/year}`. Filter results by date relevance. Cache key includes month — results expire after 12 hours or at end of month, whichever comes first.

#### Type 2 — Local Operators and Small Businesses

Questions about services offered by small independent businesses that no hotel KB or destination KB can track reliably. This includes dive shops, local tour operators, cooking class studios, boat charter companies, and independent restaurants.

- *'Are there dive shops near Riviera Maya?'*
- *'Can I find a local cooking class near the hotel?'*
- *'Is there a boat tour company near Side?'*

> **Web Search Strategy:** Query construction: `{operator type} near {district} {city}`. Prioritise results with ratings and reviews. For dive operators specifically, filter for PADI or CMAS certification mentions. Cache by operator category and district for 24 hours.

#### Type 3 — Current and Forecast Conditions

Questions about current weather, sea temperatures, forecasts, or unusual environmental conditions. General seasonal patterns live in the destination KB. Specific current or forecast data requires the web.

- *'What is the weather forecast for Bodrum next week?'*
- *'Is the sea warm enough to swim in Antalya in November?'*
- *'Is it currently raining season in Bali?'*

> **Web Search Strategy:** Query construction: `weather {city} {month}` or `sea temperature {coastline} {month}`. Use weather API if available for structured data. Cache for 4 hours — forecasts change frequently.

#### Type 4 — Real-Time Practical Information

Questions about current opening hours, entry prices, booking requirements, or operational status of attractions and services. A destination KB entry from six months ago may have wrong prices or outdated booking policies.

- *'Is Topkapi Palace open on Mondays?'*
- *'How much does entry to the Blue Mosque cost?'*
- *'Do I need to book the Cappadocia balloon ride in advance?'*

> **Web Search Strategy:** Query construction: `{attraction name} opening hours {year}` or `{attraction} entry fee {year}`. Prioritise official sources (museum websites, tourism board sites) over aggregators. Cache for 24 hours.

### 10.2 The Hybrid Pattern — Qdrant First, Web Fills Gap

The hybrid path always checks the hotel KB first. Web search is only triggered if the hotel KB does not contain a satisfactory answer.

```
HYBRID PATTERN — Example: 'Are there dive shops near Rixos Belek?'

Step 1: Resolve hotel_id = 'rixos_premium_belek' via Elasticsearch

Step 2: Qdrant scoped search on hotel_kb
  filter: hotel_id = rixos_premium_belek
  query:  'scuba diving dive centre PADI'
  category: activities

Step 3: Assess Qdrant result
  Score ≥ 0.82 AND activity_tags includes scuba_diving?
    YES → answer from hotel KB: 'Rixos Belek has its own
           PADI dive centre open daily at 8am and 2pm.'
           Web search NOT needed. Done.
    NO  → hotel has no dive centre. Proceed to Step 4.

Step 4: Web search
  Query: 'PADI dive shops near Belek Antalya'
  Returns: 3-4 local operators with ratings
  Cache: web:operators:diving:belek  TTL 24h

Step 5: Claude synthesises both sources
  'Rixos Belek doesn't have its own dive centre,
   but there are several PADI-certified operators
   in Belek town, about 10 minutes from the resort.
   [Operator A] has excellent reviews and runs
   morning and afternoon sessions.'
```

### 10.3 Query Construction Rules

| Query Type | Construction Pattern | Example Output |
|---|---|---|
| Events | `{event_type} {city/district} {month} {year}` | `festivals Playa del Carmen December 2026` |
| Local operators | `{operator_category} near {district} {city}` | `PADI dive schools near Belek Antalya` |
| Weather / conditions | `weather forecast {city} {month}` or sea temp API | `weather Bodrum December 2026` |
| Practical info | `{attraction} opening hours {year}` or `{attraction} entry fee` | `Topkapi Palace opening hours 2026` |
| Hybrid gap | `{activity_type} near {hotel_district} {city}` | `yoga studios near Sultanahmet Istanbul` |

### 10.4 Quality and Trust Constraints

> **Grounding Rule for Web Results:** Claude must answer only from what the web search actually returns — never from its own training knowledge combined with partial web results. The same grounding rule that applies to RAG chunks applies to web results. If the search returns no usable result, Claude acknowledges the gap and offers to connect the caller with a specialist.

| Risk | Mitigation |
|---|---|
| Stale web result | Cite source and date when available — *'According to [source], as of [date]...'* Caller can judge freshness. |
| Unvetted operator recommendation | Always append: *'I'd recommend checking reviews before booking.'* Never present as an endorsement. |
| Conflicting results across sources | Present the conflict transparently: *'Sources differ on this — the official site says X but a recent review mentions Y.'* |
| No usable result returned | Do not attempt to answer. Say: *'I wasn't able to find reliable current information on that — let me connect you with someone who can check directly.'* |
| Safety-relevant information from web | Never use web results for medical, legal, or safety advice. Escalate immediately. |

### 10.5 Latency Mitigation

A Qdrant query takes 30–80ms. A live web search takes 800ms–2000ms. On a voice call, 2 seconds of silence is noticeable and damages trust. Two mitigations are required.

#### Mitigation 1 — Filler Phrase While Searching

When the Source Router decides on Path 4 or Path 5 (web), Pipecat triggers a holding phrase before the async web search begins. The caller hears a natural response rather than silence.

```
Event query:    "Sizin için kontrol edeyim..."
                (Let me check for you...)

Local operator: "Size yakın seçeneklere bakıyorum..."
                (I'm looking at options near you...)

Weather:        "Güncel hava durumuna bakıyorum..."
                (I'm checking the current weather...)

Practical info: "En güncel bilgiyi buluyorum..."
                (I'm finding the most up-to-date information...)
```

The filler phrase takes 1.5–2.5 seconds to speak. The web search runs async during that time. In most cases the search result is ready by the time the filler phrase ends, and the response flows naturally.

#### Mitigation 2 — Redis Caching of Web Results

| Query Type | Cache Key Pattern | TTL | Rationale |
|---|---|---|---|
| Events | `web:events:{region}:{year_month}` | 12 hours | Event listings change daily — refresh twice per day |
| Local operators | `web:operators:{category}:{district}` | 24 hours | Operator listings change slowly — daily refresh sufficient |
| Weather | `web:weather:{city}:{date}` | 4 hours | Forecasts update frequently — short TTL required |
| Practical info | `web:practical:{attraction_id}:{year_month}` | 24 hours | Hours and prices stable day-to-day but change monthly |

### 10.6 What the Web Layer Cannot Do

- Booking or reservation actions — web search cannot execute transactions
- Medical or safety advice — even if web results contain medical information, never relay it
- Verifying specific reservation details the caller holds — this requires CRM access, not web search
- Answering complaints about a current hotel stay — escalate immediately regardless of what web might return
- Replacing the destination KB for stable knowledge — using live web for *'what is the capital of Turkey?'* wastes latency and quota

---

## 11. Progressive Narrowing

Progressive Narrowing handles the case where retrieval — from any source — returns too many valid candidates. The system asks one focused question that best differentiates the candidates, then re-queries with the tighter filter.

### 11.1 When Progressive Narrowing Triggers

| Result Set | Action |
|---|---|
| 1 strong match (score ≥ 0.82) | Proceed directly to answer generation |
| 2–3 meaningfully different matches | Claude presents top 2 with key differences |
| 4+ strong matches | Progressive Narrowing — ask one differentiating question |
| All matches below 0.65 | Do not answer — acknowledge gap, offer human or web fallback |
| Compound AND partial match only | Graceful degradation — present best partial with caveat |
| Web returns 5+ operators | Present top 2 with ratings, offer to narrow by preference |

### 11.2 Graceful Degradation — Compound AND Fails

**Scenario:** Caller wants resort in Riviera Maya with cenote diving excursions AND on-site yoga studio. No hotel in catalogue satisfies both simultaneously.

```
Claude response (graceful degradation — Turkish):

"Riviera Maya'da her iki koşulu tam karşılayan bir resort
 bulamadım. Cenote dalışı için en iyi seçenek [Resort A] —
 günlük rehberli cenote turları sunuyor, ancak yoga dersleri
 resort içinde değil, 5 dakika uzaklıkta bir stüdyoda.
 Hangisi sizin için daha öncelikli — resort içi yoga mı,
 yoksa cenote dalışı mı?"
```

---

## 12. Multilingual Strategy

The pilot serves a Turkish-speaking call center where 90% of callers speak Turkish. The system is designed Turkish-first. Web search queries are constructed in the language most likely to return useful results for the destination — typically English for international destinations, Turkish for Turkey.

### 12.1 Web Search Language Strategy

| Destination | Web Search Language | Rationale |
|---|---|---|
| Turkey | Turkish | Highest result quality for Turkish domestic content |
| Mexico / Riviera Maya | English or Spanish | Most operator and event listings in English/Spanish |
| UAE / Dubai | English | Primary tourism content language |
| Greece / Aegean islands | English | Primary tourism content language |
| Other international | English | Default — broadest result coverage |

### 12.2 Turkish-Specific Mandatory Fields

| Field | Why Mandatory for Turkey |
|---|---|
| `all_inclusive` | Dominant product in Antalya corridor — asked on almost every call |
| `all_inclusive_tier` | Standard AI vs ultra AI is commercially significant |
| `halal` | Binary requirement for significant portion of Turkish travellers |
| `alcohol_served` | Binary preference — some callers want it, others explicitly do not |
| `hamam` | Cultural expectation at quality Turkish resorts |
| `mosque_distance_m` | Relevant for observant travellers planning prayer times |
| `turkish_staff` | Domestic travellers often prefer Turkish-speaking staff |

---

## 13. Hotel Data Model

This section defines the fields the call center must provide per hotel. Fields are categorised as Mandatory, Important, or Optional.

### 13.1 Mandatory Fields

| Field | Format | Notes |
|---|---|---|
| `hotel_id` | string | Unique slug — e.g. `rixos_premium_belek` |
| `name` | string | Full official name |
| `city` | string | City or town |
| `district` | string | Sub-area — Belek, Lara, Sultanahmet |
| `region` | keyword | `antalya / istanbul / aegean / cappadocia` |
| `country` | keyword | ISO 3166-1 alpha-2 — TR for Turkey |
| `coordinates` | lat/lon | Required for geo-filter and hybrid web queries |
| `stars` | integer | Official classification 1–5 |
| `all_inclusive` | boolean | Is all-inclusive available? |
| `alcohol_served` | boolean | Is alcohol available on property? |
| `halal` | boolean | Is kitchen halal-certified? |
| `adults_only` | boolean | Is the property adults-only? |
| `checkin_time` | HH:MM | Standard check-in time |
| `checkout_time` | HH:MM | Standard check-out time |
| `last_updated` | YYYY-MM-DD | Date of most recent data review |

### 13.2 Chunk Quality Standard

> **Minimum Chunk Quality:** Each chunk must answer at least 3–5 follow-up questions about its category. Generic one-line descriptions are rejected during ingestion. Narrative text must be written natively in Turkish — not translated from English marketing copy.

### 13.3 Controlled Vocabulary — Tags

#### Activity Tags

```
Water:     scuba_diving, snorkeling, cenote_diving, water_sports, sailing,
           windsurfing, kitesurfing, fishing, boat_trips, kayaking
Land:      golf, tennis, padel, horse_riding, cycling, hiking, yoga,
           pilates, beach_volleyball
Wellness:  spa, hamam, sauna, steam_room, massage, meditation
Family:    kids_club, baby_club, teen_club, waterpark, playground
Culture:   cooking_class, local_tours, language_class, music
Night:     nightclub, live_music, casino, entertainment_shows
```

#### Vibe Tags

```
Style:     boutique, design_hotel, historic, contemporary, traditional
Segment:   romantic, family, adults_only, party, business, wellness
Scale:     intimate (under 50 rooms), mid_size (50-150), large_resort (150+)
Position:  beachfront, hillside, city_centre, rural, island
Market:    luxury, upper_midscale, midscale, budget
```

---

## 14. Data Freshness and Staleness

| Field Type | Change Frequency | Risk | Update Process |
|---|---|---|---|
| Name, location, stars | Rare — years | Low | Annual review |
| Amenities, activity tags | Occasionally | Medium | Quarterly + hotel-triggered |
| Opening hours, policies | Seasonally | High | Before each season — May and October |
| Renovation / closure | Unpredictable | Critical | Call center flags immediately |
| Packages and offers | Frequently | High | Monthly or exclude from RAG |

### 14.1 Chunk Age Caveat Policy

- **90 days** — light caveat: *'Based on our most recent information from [month]...'*
- **180 days** — strong caveat: *'Our information may not be current — recommend confirming with the hotel directly'*
- **365+ days** — do not use for factual answers. Surface the age and route to a specialist.

---

## 15. Feedback and Improvement Loop

| Signal | Indicates | Action |
|---|---|---|
| Caller repeats question | Retrieval failed or answer insufficient | Flag chunk for quality review |
| 'That's not what I meant' | Decomposition or routing error | Review decomposition logs |
| Escalated after retrieval | Confidence threshold too low or chunk missing | Check chunk and score |
| Web search returned no result | Missing local operator data or query poorly constructed | Review query construction for that type |
| Web result used but caller unsatisfied | Web result quality too low for voice response | Adjust source priority for that query type |
| Score consistently below 0.65 | Chunk quality too low for that hotel | Flag for enrichment |
| Progressive narrowing loops > once | Candidate set too broad | Review tag granularity |
| Triage asked 2 questions still insufficient | Session context not building correctly | Review session accumulation logic |

### 15.1 Review Cadence

- **Weekly** — unresolved hotel mentions. Add missing hotels to Elasticsearch.
- **Weekly** — web search failure rate. Identify query types returning no usable results.
- **Bi-weekly** — low-confidence Qdrant retrievals. Identify hotels needing data enrichment.
- **Monthly** — web result quality. Sample web-sourced answers for accuracy and trust compliance.
- **Monthly** — triage and progressive narrowing loop rates.
- **Quarterly** — full data quality audit. Hotels with `last_updated` > 90 days flagged.

---

## 16. Open Questions — To Be Resolved

| # | Question | Impact | Owner |
|---|---|---|---|
| 1 | What format will the call center provide hotel data in? | Ingestion pipeline design | Call center |
| 2 | What is the data update process? | Freshness architecture | Both |
| 3 | Who writes Turkish narrative chunk text? | Quality and timeline | TBD |
| 4 | Does the call center want a self-serve data update portal? | Significant scope difference | Call center |
| 5 | What reservation system is used for booking handoffs? | Phase 2 integration scope | Call center |
| 6 | Validate Gladia on Antalya-region Turkish accents before pilot? | STT accuracy risk | Voxtera |
| 7 | Acceptable end-to-end latency target for voice response? | Infrastructure sizing | Both |
| 8 | Should destination KB cover all Turkey or pilot regions only? | Scope and build time | Voxtera |
| 9 | Acceptable triage clarification rate? | Triage sensitivity tuning | Both |
| 10 | Which web search API — Brave Search, SerpAPI, or other? | Cost and result quality | Voxtera |
| 11 | Should the call center approve a whitelist of trusted web sources for operator recommendations? | Trust and liability | Both |
| 12 | Is the call center comfortable with web-sourced answers being presented to callers, or should web results always be caveated? | Response design policy | Call center |

---

## 17. Pilot Phasing

| Phase | Scope | Duration | Success Criteria |
|---|---|---|---|
| Phase 1 — Foundation | Elasticsearch hotel index for Antalya corridor. Qdrant hotel_kb. Redis session + cache. Triage layer. Query types 1–15 (Hotel KB + Destination KB). Web search disabled. | 6 weeks | Hotel resolution > 90%. Answer confidence > 0.75 on 80% of queries. Latency < 3s. |
| Phase 2 — Web Layer | Web search layer activated. Event and local operator queries (types 16–19). Hybrid path (types 20–23). Redis caching for web results. Filler phrase implementation. | 3 weeks | Web query success rate > 70%. No web result served without caveat. Perceived latency < 4s with filler phrase. |
| Phase 3 — Coverage + Integration | Istanbul + Aegean coast. Destination KB. Progressive narrowing. Reservation system handoff. Feedback loop tooling. | 4 weeks | 90% of query types handled. Escalation rate < 15%. Zero booking-intent queries answered with RAG or web. |

---

*Voxtera RAG Architecture v0.3 · June 2026 · Confidential — Internal Architecture Document*
