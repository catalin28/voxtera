# Dynamic Daily Rooms — Implementation Plan

- **Date:** 2026-05-24
- **Status:** ready for execution
- **Related:** `docs/ON_DEMAND_BOT_SPAWN.md`, `src/voxtera/admin/daily_client.py`, `demo-hotel/serve.py`

---

## TL;DR

The app today uses a single pre-created Daily room (`DAILY_ROOM_NAME=voxtera-demo`) shared by
every call. `BotSessionRegistry` enforces a single-slot constraint so only one call is ever in
flight at a time. The fix: call `POST /v1/rooms` at session start to create an ephemeral room
per call, pass its name to the bot subprocess via env var, then `DELETE /v1/rooms/{name}` when
the session ends.

A new `DAILY_ROOM_MAX_PARTICIPANTS` env var (default `2`) controls room capacity and can be
raised to `3+` to allow a supervisor to join and silently listen for quality-assurance testing
without disrupting the bot or guest.

---

## Current state (what exists today)

### Env vars
| Variable | Purpose |
|---|---|
| `DAILY_API_KEY` | Bearer token for all Daily REST calls |
| `DAILY_DOMAIN` | Your Daily subdomain, e.g. `voxtera.daily.co` |
| `DAILY_ROOM_NAME` | The single shared room name, e.g. `voxtera-demo` |

### Room URL construction — two places
- **`pipeline.py` line 420:** `room_url = f"https://{settings.daily_domain}/{settings.daily_room_name}"`
- **`serve.py` line 1788:** `room_url = f"https://{_DAILY_DOMAIN}/{_DAILY_ROOM_NAME}"`

### Daily REST calls that exist today (`src/voxtera/admin/daily_client.py`)
| Function | HTTP | Endpoint |
|---|---|---|
| `list_room_participants()` | `GET` | `/v1/presence` |
| `eject_participants()` | `POST` | `/v1/rooms/{room}/eject` |

> **There is no room-creation or room-deletion logic anywhere in the codebase today.** The room
> `voxtera-demo` is assumed to exist and is pre-created manually in the Daily dashboard.

### Single-slot registry (`BotSessionRegistry`)
`BotSessionRegistry` holds a single `_active_id` and raises `BotSessionBusyError` on any second
`start()` call. This is the primary concurrency gate.

---

## New env vars to add

```bash
# Whether to create a fresh Daily room for each session (default: true).
# Set to false to keep legacy single-room behaviour for existing deployments.
DAILY_DYNAMIC_ROOMS=true

# Maximum number of participants allowed in each session room (default: 2 = bot + guest).
# Raise to 3 or more to allow a QA supervisor to join the room URL and listen
# to a live bot-guest conversation for quality testing — without the bot or
# guest being aware.
DAILY_ROOM_MAX_PARTICIPANTS=2

# Maximum number of concurrent sessions the launcher will accept (default: 50).
# Lower this on resource-constrained hosts.
DAILY_MAX_CONCURRENT_SESSIONS=50
```

Add all three to `.env.example` with the comments above.

---

## Implementation steps

### Step 1 — `daily_client.py`: add `create_room` and `delete_room`

**File:** `src/voxtera/admin/daily_client.py`

Add two new functions after `eject_participants`:

```python
def create_room(
    *,
    api_key: str,
    room_name: str,
    expiry_secs: int = 600,
    max_participants: int = 2,
) -> dict[str, Any]:
    """Create an ephemeral Daily room for one session.

    ``expiry_secs`` sets a hard Daily-side time-to-live so orphan rooms are
    cleaned up even if the launcher crashes before calling delete_room.
    ``max_participants`` controls who can join:
      2 = bot + guest (default, private call)
      3+ = bot + guest + supervisor(s) for quality-monitoring use-cases
    """
    import time

    if not api_key:
        raise DailyAPIError("DAILY_API_KEY is not set")
    if not room_name:
        raise DailyAPIError("room_name is required")

    body: dict[str, Any] = {
        "name": room_name,
        "properties": {
            "exp": int(time.time()) + expiry_secs,
            "enable_prejoin_ui": False,
            "max_participants": max_participants,
        },
    }
    result = _request_json(
        f"{_DAILY_REST_BASE}/rooms",
        api_key=api_key,
        method="POST",
        body=body,
    )
    logger.info("[daily] created room {} (max_participants={})", room_name, max_participants)
    return result


def delete_room(*, api_key: str, room_name: str) -> bool:
    """Delete a Daily room.

    Returns True whether the room existed or not (idempotent — 404 is not an
    error; the room is gone either way).
    """
    if not api_key:
        raise DailyAPIError("DAILY_API_KEY is not set")
    if not room_name:
        raise DailyAPIError("room_name is required")

    try:
        _request_json(
            f"{_DAILY_REST_BASE}/rooms/{room_name}",
            api_key=api_key,
            method="DELETE",
        )
    except DailyAPIError as exc:
        if exc.status == 404:
            logger.debug("[daily] delete_room {}: already gone (404)", room_name)
            return True
        raise
    logger.info("[daily] deleted room {}", room_name)
    return True
```

Re-export both from `src/voxtera/admin/__init__.py`:

```python
from voxtera.admin.daily_client import (
    DailyAPIError,
    DailyParticipant,
    create_room,
    delete_room,
    eject_participants,
    list_room_participants,
)

__all__ = [
    "DailyAPIError",
    "DailyParticipant",
    "create_room",
    "delete_room",
    "eject_participants",
    "list_room_participants",
]
```

---

### Step 2 — `serve.py`: new module-level globals

Add these after the existing Daily globals (around line 92):

```python
_DAILY_DYNAMIC_ROOMS: bool = os.environ.get("DAILY_DYNAMIC_ROOMS", "true").lower() not in (
    "0", "false", "no"
)
_DAILY_ROOM_MAX_PARTICIPANTS: int = int(os.environ.get("DAILY_ROOM_MAX_PARTICIPANTS", "2"))
_MAX_CONCURRENT_SESSIONS: int = int(os.environ.get("DAILY_MAX_CONCURRENT_SESSIONS", "50"))
```

Update the import from `voxtera.admin` to include `create_room` and `delete_room`.

---

### Step 3 — `BotSessionRegistry`: remove single-slot constraint

**Current `start()` guard:**
```python
if self._active_id is not None:
    raise BotSessionBusyError(self._active_id)
self._active_id = session_id
```

**Replace with:**
```python
if len(self._sessions) >= _MAX_CONCURRENT_SESSIONS:
    raise BotSessionBusyError(f"{len(self._sessions)} sessions active")
```

Note: `BotSessionBusyError.active_session` will now contain a count string (e.g.
`"50 sessions active"`) instead of a UUID. The 409 JSON response sent to the browser
changes from `{"active_session": "<uuid>"}` to `{"active_session": "50 sessions active"}`.
This is acceptable — the browser only displays the value; no client-side code parses it
as a UUID.

Remove `self._active_id: str | None = None` from `__init__`.

Add helper methods:

```python
def attach_room_name(self, session_id: str, room_name: str) -> None:
    """Store the Daily room name owned by this session for later cleanup."""
    with self._lock:
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess["room_name"] = room_name

def get_room_name(self, session_id: str) -> str | None:
    """Return the Daily room name for a session, or None if unknown."""
    with self._lock:
        return self._sessions.get(session_id, {}).get("room_name")

def active_sessions(self) -> list[str]:
    """Return all active session IDs (replaces the old single active_session())."""
    with self._lock:
        return list(self._sessions.keys())
```

Update `reap()` — add best-effort room deletion as last-resort cleanup:

```python
# Best-effort: delete the Daily room if it was dynamically created.
# This fires when the bot crashes before end-session is called.
if _DAILY_DYNAMIC_ROOMS and sess is not None:
    room_name = sess.get("room_name")
    if room_name and _DAILY_API_KEY:
        import contextlib as _ctx
        with _ctx.suppress(Exception):
            delete_room(api_key=_DAILY_API_KEY, room_name=room_name)
```

Update `is_busy()` to use session count:

```python
def is_busy(self) -> bool:
    with self._lock:
        return len(self._sessions) >= _MAX_CONCURRENT_SESSIONS
```

---

### Step 4 — `_spawn_bot()`: accept per-session room name

```python
def _spawn_bot(
    session_id: str,
    callback_url: str,
    tune_port: int,
    llm_model: str | None = None,
    room_name: str | None = None,       # NEW
) -> subprocess.Popen:
    env = os.environ.copy()
    env["VOXTERA_SESSION_ID"] = session_id
    env["VOXTERA_LAUNCHER_URL"] = callback_url
    env["VOXTERA_BOT_PORT"] = str(tune_port)
    if llm_model:
        env["LLM_MODEL_OVERRIDE"] = llm_model
    if room_name:                         # NEW: override the static env var
        env["DAILY_ROOM_NAME"] = room_name
    ...
```

`pipeline.py` already reads `settings.daily_room_name` from `os.environ` via `load_settings()`.
**No changes needed in `pipeline.py` or `config.py`.**

---

### Step 5 — Port allocation for concurrent bots

With multiple concurrent bots on one host, each needs a distinct `VOXTERA_BOT_PORT`.

**Important:** Port selection AND storage must happen atomically inside the same lock
acquisition to prevent two concurrent `_handle_start_session()` threads from picking
the same port (the server uses `ThreadingHTTPServer`).

```python
# In _handle_start_session(), replace:
tune_port = _DEFAULT_BOT_PORT

# With (atomic select + store):
with REGISTRY._lock:
    used_ports = {
        s.get("tune_port")
        for s in REGISTRY._sessions.values()
        if s.get("tune_port")
    }
    tune_port = next(
        p for p in range(_DEFAULT_BOT_PORT, _DEFAULT_BOT_PORT + 200)
        if p not in used_ports
    )
    # Store immediately inside the lock so the next thread sees it.
    sess = REGISTRY._sessions.get(session_id)
    if sess is not None:
        sess["tune_port"] = tune_port
```

Also update `_start_reaper_thread` — the reaper currently calls `_set_bot_tune_port(None)`
which would clear the port for ALL sessions. Replace the global `_BOT_TUNE_PORT` /
`_set_bot_tune_port` / `_get_bot_tune_port` trio with per-session port storage:

```python
# Remove the module-level globals:
#   _BOT_TUNE_PORT: int | None = None
#   _BOT_TUNE_LOCK = threading.Lock()
#   def _set_bot_tune_port / _get_bot_tune_port

# Replace with a registry helper:
def _get_bot_tune_port(session_id: str | None = None) -> int | None:
    """Return the tune port for a session, or the first active session if None."""
    with REGISTRY._lock:
        if session_id:
            return REGISTRY._sessions.get(session_id, {}).get("tune_port")
        # Backward compat: admin tune endpoint doesn't know session_id yet.
        for sess in REGISTRY._sessions.values():
            port = sess.get("tune_port")
            if port is not None:
                return port
    return None

# In _start_reaper_thread's _reap() closure, remove:
#   _set_bot_tune_port(None)
# The port entry is already cleaned up when REGISTRY.reap(session_id)
# pops the session dict.
```

---

### Step 6 — `_handle_start_session()`: create room per call

Inside `_handle_start_session()`, after extracting `session_id` and `llm_model`:

```python
# --- Dynamic room creation ---
if _DAILY_DYNAMIC_ROOMS and _DAILY_API_KEY:
    room_name = f"vox-{session_id[:12]}"
    try:
        create_room(
            api_key=_DAILY_API_KEY,
            room_name=room_name,
            expiry_secs=600,
            max_participants=_DAILY_ROOM_MAX_PARTICIPANTS,
        )
        print(f"[launcher] created Daily room {room_name} "
              f"(max_participants={_DAILY_ROOM_MAX_PARTICIPANTS})")
    except DailyAPIError as exc:
        print(f"[launcher] failed to create Daily room: {exc}")
        REGISTRY.reap(session_id)
        self._send_json(502, {"error": f"Daily room creation failed: {exc}"})
        return
else:
    room_name = _DAILY_ROOM_NAME or ""
```

**Watchdog note:** `_session_kill` and `_session_warn` are defined as nested closures
inside `_handle_start_session()`. The local `room_name` variable is captured by the
closure automatically — use it directly in `_session_kill` rather than calling
`REGISTRY.get_room_name(sid)` at fire time. This is more robust against races where
`reap()` might pop the session dict before the watchdog fires.

Replace all uses of `_DAILY_ROOM_NAME` inside `_handle_start_session()` (orphan-bot eject,
watchdog eject) with the local `room_name` variable.

Call `REGISTRY.attach_room_name(session_id, room_name)` after `REGISTRY.start()`.

Pass `room_name` to `_spawn_bot`:

```python
proc = _spawn_bot(
    session_id, callback_url, tune_port,
    llm_model=llm_model,
    room_name=room_name,           # NEW
)
```

Build the room URL from the local variable (not the module global):

```python
room_url = f"https://{_DAILY_DOMAIN}/{room_name}"
```

**Busy check:** In dynamic mode the shared-room presence check is meaningless. Replace:

```python
# OLD: check _DAILY_ROOM_NAME presence
live = list_room_participants(api_key=_DAILY_API_KEY, room_name=_DAILY_ROOM_NAME)
```

With:

```python
# NEW: in dynamic mode, gate on session count only
if _DAILY_DYNAMIC_ROOMS:
    if REGISTRY.is_busy():
        self._send_json(409, {"error": "max_sessions_reached"})
        return
else:
    # Legacy path — check shared room presence as before
    ...
```

---

### Step 7 — `_handle_end_session()` and watchdog: delete room

In `_handle_end_session()`, after ejecting participants:

```python
if _DAILY_DYNAMIC_ROOMS and _DAILY_API_KEY and session_id:
    room_name = REGISTRY.get_room_name(session_id)
    if room_name:
        try:
            delete_room(api_key=_DAILY_API_KEY, room_name=room_name)
            print(f"[end-session] deleted Daily room {room_name}")
        except DailyAPIError as exc:
            print(f"[end-session] Daily room delete error: {exc}")
```

Apply the same pattern in `_session_kill()` (the hard watchdog at `_MAX_SESSION_SECS`).

---

### Step 8 — Admin endpoints

All three admin handlers currently hardcode `_DAILY_ROOM_NAME`.

In dynamic mode, update them to iterate over active sessions:

```python
# GET /api/admin/sessions — return per-session room participant snapshots
sessions_data = []
for sid in REGISTRY.active_sessions():
    room_name = REGISTRY.get_room_name(sid) or _DAILY_ROOM_NAME or ""
    if not room_name:
        continue
    try:
        parts = list_room_participants(api_key=_DAILY_API_KEY, room_name=room_name)
        sessions_data.append({
            "session_id": sid,
            "room_name": room_name,
            "participant_count": len(parts),
            "participants": [p.__dict__ for p in parts],
        })
    except DailyAPIError as exc:
        sessions_data.append({"session_id": sid, "room_name": room_name, "error": str(exc)})
self._send_json(200, {"sessions": sessions_data})
```

---

### Step 9 — Presence cache

`_presence_cache` is a module-level dict tied to one room. Move it into the session dict:

```python
# In _handle_start_session(), after REGISTRY.start():
REGISTRY._sessions[session_id]["presence_cache"] = {"fetched_at": 0.0, "value": None}
```

Update `_fetch_participants_cached()` to accept a `session_id` argument and read the cache
from `REGISTRY._sessions[session_id]["presence_cache"]` instead of the module global.
Remove the module-level `_presence_cache` dict and `_PRESENCE_CACHE_TTL_SECS` usage inside
the dynamic-rooms code path (keep it for the legacy path).

---

## Verification checklist

| # | Test | Expected result |
|---|---|---|
| 1 | Start two browser sessions simultaneously | Each receives a distinct `room_url` (`vox-<id1>` vs `vox-<id2>`) |
| 2 | Daily dashboard during step 1 | Two separate rooms, each with exactly `DAILY_ROOM_MAX_PARTICIPANTS` capacity |
| 3 | End one session | Only that session's room is deleted; the other continues |
| 4 | Set `DAILY_ROOM_MAX_PARTICIPANTS=3`, restart, join room URL from a third tab | All three participants connect with audio (supervisor can listen) |
| 5 | Let a session hit the 3-minute watchdog | Room is ejected and deleted, not just the bot killed |
| 6 | Set `DAILY_DYNAMIC_ROOMS=false`, restart | Existing single-room behaviour unchanged; admin eject works; 409 on second Start |
| 7 | `pytest tests/test_admin_daily_client.py` | All existing tests pass; new `create_room` and `delete_room` tests pass |
| 8 | `GET /api/admin/sessions` with two active sessions | Returns a list with two entries, each with `session_id`, `room_name`, participants |
| 9 | Crash the launcher mid-session | Daily rooms are cleaned up by `reap()` best-effort path on next reaper fire |

---

## Decisions log

| Decision | Rationale |
|---|---|
| Room name: `vox-{session_id[:12]}` | Readable in Daily dashboard; globally unique per session; short enough for log scanning |
| Room expiry: 600 s | 10-min hard Daily-side TTL. Bot watchdog kills at 180 s, leaving 8 min of grace for hung shutdown before Daily itself reclaims the room |
| `enable_prejoin_ui: false` | Daily's lobby screen conflicts with Voxtera's own "Joining…" state in the browser |
| `DAILY_DYNAMIC_ROOMS=false` compat | Existing operators keep working with zero `.env` changes |
| `DAILY_ROOM_MAX_PARTICIPANTS` default 2 | One bot + one guest. Setting to 3+ enables silent supervisor join for QA without bot/guest awareness |
| `pipeline.py` unchanged | Already reads `DAILY_ROOM_NAME` from `os.environ` via `load_settings()`; subprocess env override is sufficient |
| `_eject_stale_bots()` in `pipeline.py` unchanged | With a freshly created room it always finds zero participants — fast no-op |

---

## Files touched summary

| File | Change |
|---|---|
| `src/voxtera/admin/daily_client.py` | Add `create_room()` and `delete_room()` |
| `src/voxtera/admin/__init__.py` | Re-export both new functions |
| `demo-hotel/serve.py` | New env globals; `BotSessionRegistry` multi-slot; `_spawn_bot()` room param; `_handle_start_session()` dynamic create; `_handle_end_session()` + watchdog delete; admin handlers; presence cache |
| `.env.example` | Document `DAILY_DYNAMIC_ROOMS`, `DAILY_ROOM_MAX_PARTICIPANTS`, `DAILY_MAX_CONCURRENT_SESSIONS` |
| `tests/test_admin_daily_client.py` | New tests for `create_room` and `delete_room` |
| `src/voxtera/pipeline.py` | **No changes needed** |
| `src/voxtera/config.py` | **No changes needed** |
