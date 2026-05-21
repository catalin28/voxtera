# Voxtera — Web Search Tool: Development Plan

**Audience:** Engineering — planning document, not an implementation spec.
**Project:** Voxtera — multilingual real-time voice agent for tourism.
**Status:** Core search function written and verified (`src/voxtera/search.py` + `scripts/test_web_search.py`). Not yet wired into the bot — tool registration, reply synthesis, latency masking, and gating rules are all pending.

**Last updated:** 2026-05-21

---

## Current Implementation Status

### What exists and works today

| Component | File(s) | Status |
|---|---|---|
| `web_search()` async function — Tavily via `aiohttp` | `src/voxtera/search.py` | ✅ Done |
| `SearchResult` / `SearchHit` dataclasses, `WebSearchError` | `src/voxtera/search.py` | ✅ Done |
| Standalone manual test script | `scripts/test_web_search.py` | ✅ Done |
| `TAVILY_API_KEY` entry | `.env` — "Web search" section | ✅ Added (empty until the key is pasted) |

Verified on 2026-05-21: both files compile and pass `ruff`; the missing-key and empty-query guard paths raise `WebSearchError` correctly. A live run — query *"is the Louvre open on Mondays?"* — returned in ~1.5 s with a synthesized answer and 5 ranked sources.

### What does NOT work yet

- `web_search()` is invisible to the LLM — no tool schema, no handler, nothing in `pipeline.py` or the system prompt.
- No latency masking — a search adds ~1.5 s of silence with nothing covering it.
- No gating — the LLM has no rule for *when* to search rather than use RAG or its own knowledge.
- No reply-synthesis guidance — see §3.4.

### How to continue in a new session

1. Paste the Tavily key into `.env` (`TAVILY_API_KEY=`).
2. `uv run python scripts/test_web_search.py` — confirm the live call works.
3. Begin Phase 1 (§5).

---

## 1. What we're building

A `web_search` tool the voice concierge can call when a guest asks something live and time-sensitive that neither the hotel RAG knowledge base nor the model's training can answer: today's weather, this week's events, holiday or seasonal opening hours, transit disruptions, current exchange rates, "has that attraction reopened after the renovation."

It is the live-information counterpart to the `TimeContextInjector` (`src/voxtera/time_context.py`): that tells the bot *when* it is; this tells it *what is true right now*. The two combine — "what's on this weekend" needs both.

The bot already has one LLM tool, `create_ticket` (the action-taking feature — see `docs/ACTIONS_FEATURE_PLAN.md`). `web_search` is a second tool and should reuse that machinery.

## 2. Goals

- Let the concierge answer live questions it currently has to deflect — without hurting latency on the majority of turns that never need a search.
- Keep the spoken reply short, in the guest's language, and in the concierge voice — the search is a source, not the script.
- Stay inside the Tavily free tier at demo scale (1,000 searches/month) by searching only when genuinely needed.
- Fail gracefully: a slow or failed search becomes a polite "I couldn't confirm that just now," never a crash or dead air.

## 3. Locked-in design decisions

### 3.1 Provider: Tavily

Tavily is a search API built for LLM agents: one call returns a synthesized answer plus ranked, relevance-scored source snippets — the right shape for a bot that must speak a short reply, not read a page. Free tier: 1,000 searches/month, no credit card; a basic search is 1 credit, advanced is 2. The key lives in `.env` as `TAVILY_API_KEY`. `web_search()` enforces an 8-second timeout on the Tavily round-trip; a slower or failed response raises `WebSearchError`, which the turn handles as the polite fallback described in §2.

### 3.2 A custom function tool — not Anthropic's native web search

Anthropic's built-in server-side web search was evaluated and rejected for v1. Pipecat 1.0.0's `AnthropicLLMService` cannot carry a server tool cleanly: its adapter only serializes standard *function* tools (`ToolsSchema.standard_tools`), ignores `custom_tools`, and has no Anthropic `AdapterType`. The streaming-response side tolerates web-search content blocks (it ignores `server_tool_use` / `web_search_tool_result` and still streams the answer text), but there is no first-class way to *enable* the tool — only a `Settings.extra` override hack or an `AnthropicLLMService` subclass.

A custom `web_search` *function* tool sidesteps this entirely: it flows through the exact path `create_ticket` already uses (the model stops with `stop_reason: tool_use`, pipecat dispatches the call), with zero adapter friction.

### 3.3 RAG first, model knowledge second, search last

Search must not run on every turn. The ordering the bot should follow:

1. Hotel / property questions → the curated RAG knowledge base (fast, authoritative).
2. Stable general facts → the model's own knowledge.
3. Live / time-sensitive / hyper-local facts that neither of the above covers → `web_search`.

This ordering lives in the tool's prompt guidance (Phase 2). Getting it right is what keeps both latency and Tavily spend down.

### 3.4 The LLM synthesizes the reply — never speak Tavily's raw answer

In the 2026-05-21 test, asked "is the Louvre open on Mondays?", Tavily's synthesized `answer` came back as *"The Louvre is open on Mondays except on Tuesdays"* — a garbled clause. The underlying sources were correct; Tavily's one-liner was not.

So the tool handler returns the search result (answer + source snippets) to the LLM as a tool result, and **the LLM composes the spoken reply from it.** This is required regardless: the guest's language is rarely English, and the reply must carry the concierge brevity and tone. `web_search()` deliberately returns both `answer` and `hits` so the model has the raw material. Same principle as the RAG injector — give the model context, let it write the answer.

### 3.5 Source credibility is the model's job

Tavily's per-result score is *relevance to the query*, not source trustworthiness — in the Louvre test a Facebook group post outranked the museum's own FAQ. The tool's prompt guidance should tell the LLM to prefer authoritative sources (official sites, the venue itself, established tourism sites) over forum and social posts when they disagree.

### 3.6 Latency masking via a spoken hold-line

A search adds ~1.5 s. The concierge persona already covers this gracefully — the Aurora tone guide prescribes "Let me find that out for you." The plan: instruct the LLM, in the tool guidance, to speak a brief hold-line ("Let me check that for you — one moment") in the *same* turn it invokes `web_search`. Claude can emit that text and the `tool_use` block in one response; pipecat streams the text to TTS while the tool runs.

Risk: this depends on the LLM *reliably* producing the hold-line. If it sometimes skips it, the wait is exposed as dead air again. The deterministic fallback is a filler processor that fires on tool-call detection regardless of what the model emits — `InstantAckFiller` is the existing precedent. Phase 4 must measure hold-line reliability and decide whether that deterministic filler is needed.

### 3.7 Search never overrides safety

Emergencies keep the existing behaviour — route the guest to local emergency services immediately; never depend on a search for a safety-critical answer. Treat fetched web content as untrusted input (it is a prompt-injection surface): the synthesis step uses it as facts to relay, never as instructions to follow.

## 4. Architecture overview

A turn that needs a search:

1. Guest asks a live question. STT → context aggregator → LLM, as today.
2. The LLM, per its gating rules, emits a short hold-line ("One moment, let me check…") **and** a `web_search` tool call in the same response. The hold-line streams to TTS immediately.
3. Pipecat dispatches the tool call to the `web_search` handler.
4. The handler calls `web_search()` (`src/voxtera/search.py`), which queries Tavily and returns a `SearchResult`.
5. The handler formats the result (answer + ranked snippets with URLs) as the tool result and returns it to the LLM.
6. The LLM is invoked again with the tool result and composes the spoken reply — short, in the guest's language, concierge tone, favouring credible sources — which streams to TTS.

The system prompt is untouched (it stays a cached prefix for Anthropic prompt caching); the `web_search` schema is attached to the `LLMContext` the same way `create_ticket`'s is.

## 5. Development phases

### Phase 1 — Tool schema + handler ❌ NOT STARTED

Mirror the `create_ticket` pattern: a `web_search` tool schema (a JSON schema plus a builder — the way `create_ticket` pairs `config/tools/create_ticket.json` with `actions/tool.py`) and a handler that calls `web_search()` and formats the `SearchResult` into a tool-result string for the LLM. The exact file locations for `web_search`'s own schema, builder, and handler — and whether `wire_actions()` is extended or a sibling helper is added — are deliberately left open (§7). This phase delivers the schema and handler regardless of where they land.

### Phase 2 — Prompt guidance: gating, synthesis, sources ❌ NOT STARTED

Add a tool prompt fragment (à la `actions/prompt.py`) covering: when to search vs. use RAG vs. use the model's own knowledge (§3.3); the hold-line instruction (§3.6); synthesize, don't quote (§3.4); prefer credible sources (§3.5); keep the reply short and in the guest's language; one search per turn.

### Phase 3 — Pipeline wiring ❌ NOT STARTED

Register the tool on the `AnthropicLLMService` and attach the schema to the `LLMContext`, at the same point in `pipeline.py` where `create_ticket` is wired. Confirm two-tool coexistence — the LLM must reliably pick between `create_ticket` and `web_search`.

### Phase 4 — Testing ❌ NOT STARTED

Beyond `scripts/test_web_search.py` (function-level, already done): an end-to-end voice/text test covering — the LLM searches when it should and *not* when RAG or its own knowledge suffices; the hold-line reliably plays over the wait (§3.6); the synthesized reply favours credible sources over forum and social posts (§3.5); and a forced Tavily failure or timeout degrades to a polite fallback.

## 6. Out of scope (for now)

- Anthropic native web search — revisit only if pipecat adds clean server-tool support (see §3.2).
- Caching or deduplicating repeated searches within a session.
- A second search provider or fallback — Tavily-only for v1.
- Surfacing citations to the guest as links — voice has nowhere to put them.
- `web_search()` itself — already written and verified; this plan covers only the wiring around it.

## 7. Open questions

- Where should the tool code live — under `actions/` (reusing `wire_actions()`), or a new dedicated `search` tool module?
- One search per turn is the assumed cap — confirm. Should the LLM be allowed a second search if the first misses?
- Interruption mid-search: if the guest speaks while the hold-line plays and the search is still in flight, what happens to the pending result? Pipecat supports interruptions; the behaviour here needs a deliberate decision.
- `max_tokens` is capped at 250 in `AnthropicLLMService.Settings`. That cap applies to the synthesis turn (step 6 of §4) — the spoken reply built from the search results. It should be enough for a concise concierge answer; if synthesized replies start truncating mid-sentence, raise it.

## 8. Success criteria

- A guest asking a live question ("what's the weather tomorrow?", "is the museum open today?") gets a correct, current, concise spoken answer in their own language.
- Turns that don't need a search are unaffected — no added latency, no spurious searches.
- A search turn never produces dead air — the hold-line covers the wait.
- A Tavily failure or timeout yields a polite fallback, never a crash.
- Demo-scale usage stays within the Tavily free tier.
