# External Information Sources (Phase 4a)

Voxtera's voice bot draws from three optional external sources to answer
guest questions the hotel knowledge base cannot:

| Source            | LLM tool name        | Provider               | What it answers                                           |
| ----------------- | -------------------- | ---------------------- | --------------------------------------------------------- |
| Web search        | `web_search`         | Tavily                 | Live/time-sensitive lookups (weather, events, hours)      |
| YouTube videos    | `find_hotel_videos`  | YouTube Data API v3    | "Do you have a video of this hotel?"                      |
| Hotel reviews     | `find_hotel_reviews` | Google Places API New  | "What do people say about this hotel?" (rating + reviews) |

Each source is independent — enabling one does not require the others.

## Feature flags

Two env vars per source: an `*_ENABLED` master flag and an API key. **Both
must be truthy** for the corresponding LLM tool to be registered:

```env
WEB_SEARCH_ENABLED=true
TAVILY_API_KEY=...

YOUTUBE_SEARCH_ENABLED=true
YOUTUBE_API_KEY=...

PLACES_SEARCH_ENABLED=true
GOOGLE_PLACES_API_KEY=...
```

If either is missing/false, the tool is not registered with the LLM at all,
so the model cannot call it — preventing hallucinated invocations. See
`src/voxtera/external_flags.py` for the gating logic.

| `*_ENABLED` | API key set | Result                                                |
| ----------- | ----------- | ----------------------------------------------------- |
| `true`      | yes         | Tool registered, model can call it                    |
| `true`      | no          | Tool **not** registered, warning logged with key name |
| `false`     | yes         | Tool **not** registered, warning logged with flag     |
| `false`     | no          | Tool **not** registered                               |

The `false` switch is for cost control / A-B testing — leave the key in
place but flip the flag off to silence the source without changing keys.

## Provider setup

### Tavily (web search)
- Sign up: <https://app.tavily.com>
- Free tier: 1,000 searches/month
- Already wired in earlier phases.

### YouTube Data API v3
1. Open <https://console.cloud.google.com> → create or pick a project.
2. APIs & Services → Library → enable **YouTube Data API v3**.
3. APIs & Services → Credentials → Create credentials → API key.
4. Restrict the key to "YouTube Data API v3" only (recommended).
5. Paste into `.env` as `YOUTUBE_API_KEY=`.
- Quota: 10,000 units/day free. Each `search.list` costs 100 units → 100
  searches/day free, then $5 per extra 1k queries.

### Google Places API (New)
1. Same Google Cloud project (reuses billing/quota).
2. APIs & Services → Library → enable **Places API (New)**.
3. APIs & Services → Credentials → reuse the YouTube key or create a new
   one; restrict to Places.
4. Paste into `.env` as `GOOGLE_PLACES_API_KEY=`.
- Pricing: ~$17 per 1k Place Details calls; first $200/mo free
  (~11k calls). Field masks in `google_places.py` keep us at the
  Place Details Essentials SKU tier.

## Code layout

```
src/voxtera/
  external_flags.py          ← env-driven gating (one source of truth)
  search.py                  ← Tavily HTTP client (existing)
  youtube.py                 ← YouTube Data v3 HTTP client (new)
  google_places.py           ← Places API New HTTP client (new)
  actions/
    integration.py           ← wire_web_search / wire_find_videos /
                               wire_find_reviews — each gated by flag+key
    web_search_handler.py    ← Tavily handler
    find_videos_handler.py   ← YouTube handler
    find_reviews_handler.py  ← Places handler
    tool.py                  ← FunctionSchema builders + tool name consts

config/tools/
  web_search.json
  find_hotel_videos.json
  find_hotel_reviews.json

tests/
  test_external_sources.py   ← flag gating + handler edge cases
```

## Wiring into the bot

`bot.py` calls each `wire_*` function it wants enabled. Each call is a
no-op when the source is disabled, so this is safe even before keys are
provisioned:

```python
from voxtera.actions.integration import (
    wire_actions, wire_web_search, wire_find_videos, wire_find_reviews,
)

wire_actions(llm=llm, context=ctx, hotel_config=cfg, sink=sink)
wire_web_search(llm=llm, context=ctx)
wire_find_videos(llm=llm, context=ctx)
wire_find_reviews(llm=llm, context=ctx)
```

> **Phase 4a status:** the YouTube and Places tools are coded, tested,
> and ready, but the `bot.py` wiring lines above are **not yet added**.
> That happens in Phase 3d (voice integration), when all the external
> tools land in `bot.py` in a single coherent pass.

## Operational notes

- **Failure mode:** every handler catches the client's typed error
  (`YouTubeSearchError`, `PlacesError`, `WebSearchError`) and returns
  `{"status": "unavailable", "guidance": "...do NOT make up an answer."}`
  to the model. The model is explicitly told to apologise rather than
  invent content.
- **Timeouts:** all clients use a 6-second HTTP timeout to keep the
  voice loop responsive. A slow YouTube/Places API call won't stall a
  guest mid-conversation.
- **Logging:** every tool call logs query, hit count, and elapsed_ms via
  loguru at `INFO`. Failures log at `WARNING` with the upstream reason.
- **Cost guardrails:** field masks (Places) and `maxResults` caps
  (YouTube) keep per-call cost predictable.
