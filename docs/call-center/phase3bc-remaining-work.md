# Phase 3bc — Remaining Work

**Branch:** `feat/VOX-concierge-ui-timings`

---

## Deferred to follow-on phases (deliberate, scoped out of 3bc)

### Phase 3d — Voice on the concierge page

The concierge page currently has **no orb / no call button** — chat
only. Adding voice means lifting the orb + mic + Safari-mute UX from
`voxtera-demo.html` and pointing it at a concierge-flavoured Pipecat
pipeline (or a thin variant of the existing one that swaps the system
prompt for the concierge agent's render output). This is its own
phase because:

- Concierge currently does one decompose + one render LLM call per
  turn. Streaming the render into TTS sentence-by-sentence (the way
  `_handle_chat` does) is a meaningful pipeline change.
- The mic path needs an STT provider wired in, which means the
  per-visitor token gate (`_demo_anon_ok` / `_validate_demo_token`)
  has to be applied to `/api/concierge` too. Not in scope here.

### Phase 3e — i18n + dynamic region list

- UI copy is English-only. Add `data-i18n` keys + load
  `demo-hotel/i18n/` like the booking demo does.
- The region dropdown is currently a hard-coded list of six regions.
  Wire it to whatever region metadata Phase 2c indexes (likely a new
  `GET /api/concierge/regions` endpoint that reads from the hotels
  config / Qdrant).

### Phase 3a — Cross-encoder re-rank (still deferred from Phase 3)

Not touched by 3bc. Tracking the same backlog item.

## Small follow-ups (nice-to-have, not blocking)

- **Per-visitor rate limit on `/api/concierge`.** The endpoint
  currently has no gate — anyone hitting `demo-hotel/serve.py` can
  call it and burn Anthropic + Qdrant. Once the concierge becomes
  publicly linked, gate it behind the same demo-token / IP allowance
  logic as `/api/chat`.
- **Persistent visitor session.** Each Ask is independent. Add a
  `session_id` cookie + per-session transcript drawer so visitors can
  follow up ("show me only the ones with a pool").
- **`p95` baseline.** Run 20–50 live concierge requests against a
  warm Qdrant + warm Anthropic key and publish the `decompose_ms` /
  `retrieve_ms` / `render_ms` / `total_ms` percentiles in
  `docs/call-center/phase3bc-test-report.md` §4.
- **Live smoke refresh.** `scripts/smoke_concierge_live.py` should be
  re-run once and its output committed under `logs/audit/` so the
  timings format is captured at a known baseline.
- **Server.py twin route.** The Phase 3 `GET /call_center/api/concierge`
  endpoint on the aiohttp server is still wired and useful for
  back-channel debugging. Leave it as-is.

## Known limitations

- `_handle_concierge` spins up a **fresh event loop + aiohttp session
  per request**. This is fine for the public demo's traffic level but
  is wasteful at scale. If concierge traffic grows, move to a shared
  loop running in a worker thread (see how `serve.py` does it for
  bot launches at line ~1047).
- Hotel cards show at most 5 hotels. There is no "show more" — the
  rest of the matched set is silently dropped in the UI. The full
  list is still present in `result.retrieval.hotels` (visible in the
  debug drawer's reason line).
- The debug drawer exposes the raw decomposition JSON to anyone who
  loads the page. Acceptable for a demo surface; remove before any
  white-label embed.
