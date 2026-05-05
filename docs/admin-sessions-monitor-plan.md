# Admin Sessions Monitor — Plan

- **Date:** 2026-05-05
- **Status:** Proposed (awaiting review before implementation)
- **Author:** Claude + Catalin
- **Related:** `docs/ON_DEMAND_BOT_SPAWN.md`, ADR-0004 (Daily.co transport)

---

## TL;DR

Add a small, password-protected admin page at `/admin.html` (served by `demo-hotel/serve.py`) that lets the operator:

1. See who is currently in the configured Daily room — guest and bot, with join time, duration, user_name, and participant id.
2. See an aggregate count of active sessions (today: 0 or 1 — multi-room comes later).
3. Click **Kick** on any participant to eject them via Daily's REST API.
4. Optionally **End session** — eject everyone in the room in one shot.

The page polls a single backend endpoint every few seconds. There is no new database, no new service, no Redis. We reuse the HTTP server already running in `demo-hotel/serve.py` and the `DAILY_API_KEY` already in `.env`.

---

## Goals

1. Operator can answer the question "is anyone using the demo right now?" without opening the Daily dashboard.
2. Operator can forcibly remove a stuck or abusive participant in one click.
3. The page works against the **current** single-fixed-room model, and extends cleanly to the multi-session model proposed in `ON_DEMAND_BOT_SPAWN.md` without rewrite.
4. Zero new infrastructure: same port, same process, same `.env`.

## Non-Goals

- **Recording playback / transcript browsing.** Out of scope — that goes through `conversation_logger.py` and a separate viewer later.
- **Historical analytics.** No "calls per day" charts. The page shows *live* state only; the source of truth is Daily's API in real time.
- **Multi-tenant admin.** One shared admin token for the demo. RBAC waits for a real product surface.
- **Cross-room overview.** Today there is exactly one room (`DAILY_ROOM_NAME`). When multi-room lands, the page becomes a list of rooms that drills into per-room participants — the per-room view is what we're building now.

---

## Current state of the code (verified 2026-05-05)

- `src/voxtera/pipeline.py:_eject_stale_bots()` already calls
  - `GET https://api.daily.co/v1/presence` — returns a dict keyed by room name with a list of participants `[{id, user_name, joined_at, duration, …}]`.
  - `POST https://api.daily.co/v1/rooms/{room_name}/eject` with body `{"ids": [...]}` — returns `{"ejectedIds": [...]}`.
- Auth is `Authorization: Bearer {DAILY_API_KEY}` — same key the bot already holds.
- `demo-hotel/serve.py` is a `socketserver.ThreadingTCPServer` with a `BaseHTTPRequestHandler` that already exposes `POST /api/tts-test` and `POST /api/chat`. It serves static files from its own directory. We add new routes and an `admin.html` next to `demo.html`.
- The on-demand launcher in `docs/ON_DEMAND_BOT_SPAWN.md` is proposed but not implemented: `launcher_client.py` exists on the bot side, but `serve.py` does not yet hold a `_sessions` registry. The plan below is **independent of the launcher landing first** — when the launcher does land, the admin page gets richer data from the same registry.

## Architecture

```
┌─────────────┐     GET /admin.html        ┌────────────────┐     GET /v1/presence
│  Operator   │ ─────────────────────────► │   serve.py     │ ─────────────────────► Daily REST
│  browser    │                            │  (admin routes)│                        api.daily.co
│  /admin.html│ ◄───────── JSON ─────────  │                │ ◄─── JSON ──────────
└─────────────┘     GET /api/admin/sessions└────────────────┘
       │                                          │
       │  POST /api/admin/eject {room, ids}       │   POST /v1/rooms/{room}/eject
       └─────────────────────────────────────────►├──────────────────────────────► Daily REST
```

No new processes. The admin page's only state is `localStorage` for the admin token and the chosen poll interval.

## Endpoints — backend (in `serve.py`)

All admin endpoints require header `X-Admin-Token: $VOXTERA_ADMIN_TOKEN`. Wrong/missing token returns `401`. The token is read once at server startup; absence at startup logs a `WARN` and disables the admin routes (returns `503` so the page can render a clear "admin disabled" state).

### `GET /api/admin/sessions`

Returns the live snapshot of who is in the configured Daily room.

Response shape:

```json
{
  "room": "voxtera-demo",
  "domain": "voxtera.daily.co",
  "fetched_at": "2026-05-05T09:33:11Z",
  "participants": [
    {
      "id": "d9bbf1b0-…",
      "user_name": "Voxtera",
      "joined_at": "2026-05-05T09:31:02Z",
      "duration_secs": 129,
      "is_bot": true
    },
    {
      "id": "c201dba0-…",
      "user_name": "Guest",
      "joined_at": "2026-05-05T09:32:15Z",
      "duration_secs": 56,
      "is_bot": false
    }
  ],
  "session_count": 1
}
```

Implementation notes:

- Backend hits `GET https://api.daily.co/v1/presence`, filters to `room_name == settings.daily_room_name`, and reshapes each entry.
- `is_bot` is computed from `user_name == settings.bot_name` (the same comparison `pipeline.py` already uses around line 377 to detect the bot's own `participant_left`). Document this in code as a known coupling so a future rename of `BOT_NAME` updates both call sites.
- `session_count` is derived: `1 if any non-bot participant else 0`. When the on-demand launcher lands, this becomes `len(launcher.registry)` and the response gains a top-level `sessions: [{session_id, started_at, …}]` array — additive, no breakage.
- Daily REST has no published per-second rate limit but we cap polling at ~1 req / 3 s (default) and 1 req / 1 s (max). Backend de-duplicates: if two browsers poll within 500 ms, we return a 500 ms cached response.

### `POST /api/admin/eject`

Body: `{"ids": ["<participant_id>", ...]}` — one or many.

- Backend forwards to `POST https://api.daily.co/v1/rooms/{daily_room_name}/eject`.
- Returns `{"ejected_ids": [...], "requested_ids": [...]}`. Any IDs Daily refused are surfaced so the UI can flag them.
- Logs every eject at INFO with the operator's IP and the participant id, so we have an audit trail in `logs/`.

### `POST /api/admin/end-session`

Convenience: ejects **all** participants in the room (bot + guest).

- Backend calls `GET /v1/presence`, takes every id, and calls the eject endpoint with the full list.
- Same shape as `/eject`. Same audit logging.

### `GET /api/admin/health`

Returns `{ "ok": true, "daily_room": "...", "daily_domain": "...", "admin_enabled": true }`. Lets the page render a clear error state when `DAILY_API_KEY` is missing or wrong.

## Frontend — `demo-hotel/admin.html`

Single self-contained HTML file. No build step. Vanilla JS + a small CSS block. Deliberately NOT a React app — keeps it shippable with `make run` and matches the rest of `demo-hotel/`.

Layout, top to bottom:

1. **Header** — "Voxtera Admin", room name, last-refreshed timestamp, "Refresh now" button, poll-interval dropdown (1 s / 3 s / 10 s / paused).
2. **Token gate** — first load asks for the admin token, stores it in `localStorage` under `voxtera_admin_token`. "Forget token" button clears it.
3. **Summary strip** — three cards: "Active sessions: N", "Bot present: yes/no", "Total participants: N".
4. **Participant table** — columns: User name, Role (Bot / Guest), Joined at, Duration, Participant ID (truncated, click to copy), Action (Kick button).
5. **Danger zone** — a single "End session (eject everyone)" button with a confirm dialog. Disabled when participant count is 0.
6. **Status line** — last error from the backend (token wrong, Daily down, etc.), with a dismiss button.

Polling:

- `setInterval` driven by the dropdown. Default 3 s.
- Pauses automatically when the tab is hidden (`document.visibilityState`) — no point burning Daily API calls against a backgrounded tab.
- One in-flight request at a time. If the previous fetch hasn't returned, the next tick is skipped.

Eject UX:

- Per-row Kick button → `confirm("Kick <user_name>?")` → `POST /api/admin/eject` with that one id → optimistic remove from the table, reverted if the response says the id wasn't ejected.
- "End session" → `confirm("Eject ALL participants from <room>? This will end any active call.")` → `POST /api/admin/end-session`.

## Auth model

- One static token: `VOXTERA_ADMIN_TOKEN` in `.env`. Long random string, generated with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Sent via `X-Admin-Token` header on every admin request.
- Stored in browser `localStorage`. Cleared by the "Forget token" button.
- HTTPS in production (the Droplet already terminates TLS at Nginx per `README.md`'s deploy section). On `localhost` the token is fine in cleartext.
- This is intentionally lightweight. It is correct security for a single-operator demo, and wrong security for a real product — when we have real users we move to OAuth + per-user roles. That's a separate project.

## Error handling

| Failure                       | Backend response                  | Frontend behavior                                                                       |
| ----------------------------- | --------------------------------- | --------------------------------------------------------------------------------------- |
| `DAILY_API_KEY` not set       | 503 from `/api/admin/health`      | Page renders a banner: "Admin endpoints disabled — `DAILY_API_KEY` missing on server."  |
| `VOXTERA_ADMIN_TOKEN` not set | 503 from every `/api/admin/*`     | Same banner pattern, different message.                                                 |
| Wrong `X-Admin-Token`         | 401                               | Token gate re-shown, "Token rejected by server" message.                                |
| Daily REST 4xx / 5xx          | 502 with `{"error": "<message>"}` | Status line shows the error, table keeps last-known data, polling continues.            |
| Daily REST timeout (>5 s)     | 504                               | Same as above.                                                                          |
| Network failure to backend    | (no response)                     | "Server unreachable — retrying in 3 s." Frontend keeps trying at the chosen interval.   |

## File-by-file change list

| File                                          | Change                                                                                                                                                                                                                       |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `demo-hotel/serve.py`                         | Add `_handle_admin_sessions`, `_handle_admin_eject`, `_handle_admin_end_session`, `_handle_admin_health`. Add `_check_admin_token(self)`. Add path dispatch in `do_GET` and `do_POST`. Read `VOXTERA_ADMIN_TOKEN` at startup. |
| `demo-hotel/admin.html` (new)                 | The page described above.                                                                                                                                                                                                    |
| `src/voxtera/admin/daily_client.py` (new)     | Small async-friendly wrapper around Daily REST: `list_participants(room)`, `eject(room, ids)`. Pure functions, no global state. Reuses logic from `pipeline._eject_stale_bots` and replaces it as a follow-up cleanup.       |
| `src/voxtera/pipeline.py`                     | (Cleanup, optional, second PR.) Replace inline `urlopen` in `_eject_stale_bots` with `voxtera.admin.daily_client` so there's one place that knows how to talk to Daily.                                                      |
| `.env.example`                                | Add `VOXTERA_ADMIN_TOKEN=` (empty, with a comment showing the `secrets.token_urlsafe(32)` recipe).                                                                                                                           |
| `docs/setup.md`                               | One paragraph: "Generate an admin token with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`, set `VOXTERA_ADMIN_TOKEN`, open `http://localhost:8080/admin.html`."                                            |
| `tests/test_admin_endpoints.py` (new)         | Token-gate tests, presence-shape tests with a mocked Daily REST, eject happy-path and Daily-error path.                                                                                                                      |

Total: 2 new files of significance (`admin.html`, `daily_client.py`), surgical additions to `serve.py`, one env var, one test file.

## Implementation order

1. **Backend skeleton.** Add `daily_client.py`, wire `/api/admin/health` and `/api/admin/sessions`. Verify against the live Daily room with `curl`.
2. **Frontend skeleton.** Token gate + table + 3 s polling. No actions yet.
3. **Eject.** Backend `/api/admin/eject`, then per-row Kick button.
4. **End session.** Backend + button + confirm dialog.
5. **Tests.** Token-gate, presence shape, eject happy-path, Daily-error path.
6. **Cleanup PR.** Refactor `pipeline._eject_stale_bots` to use `daily_client`.

Stop after step 4 if the launcher (`ON_DEMAND_BOT_SPAWN.md`) lands first — at that point step 1's `/api/admin/sessions` gains a `sessions` array, the page grows a "Sessions" section above the participant table, and we're done.

## Open questions

1. **Should "End session" also kill the bot subprocess?** Today the bot is always-on, so ejecting it just means it rejoins (or stays ejected until the process is restarted). When the on-demand launcher lands, "End session" should additionally `SIGTERM` the bot subprocess via the launcher registry. Until then, "End session" is functionally "kick everyone" and the bot will rejoin within a few seconds — flag this in the UI.
2. **Multiple Daily rooms in the future.** Do we list rooms by env var (`DAILY_ROOMS=demo-hotel,demo-airport`), or call `GET /v1/rooms` and show all of them? Lean toward listing via env var so the admin page only shows rooms the operator owns.
3. **Audit log persistence.** Loguru rolls logs in `logs/`. Is that enough, or do we want eject events in a structured file (`logs/admin-audit.jsonl`)? Recommendation: loguru is enough for the demo; structured audit log is a one-line change to add later.
