# On-Demand Bot Spawn — Design & Implementation Plan

- **Date:** 2026-05-05
- **Status:** Proposed (awaiting review before implementation)
- **Author:** Claude + Catalin
- **Related:** ADR-0004 (Daily.co transport)

---

## TL;DR

Today the Voxtera bot joins the Daily room as soon as the backend process starts and stays there forever — eating Daily participant-minutes 24/7 even when nobody is calling. We are switching to an **on-demand spawn** model: the bot is launched as a subprocess only when the user clicks **Start** in the browser, and exits cleanly after the user hangs up.

A small launcher inside `serve.py` orchestrates the spawn. The bot signals readiness back to the launcher via an HTTP callback that puts an event on a thread-safe `queue.Queue`. The browser is held on a "Joining…" state until the launcher confirms the bot is in the room.

This fixes three problems at once:

1. **Daily participant-minute waste** — the bot is only in the room while a Guest is.
2. **State leak between Guests** — each call gets a fresh pipeline (no leftover GreetingController debounce, no stale LLM context).
3. **Single-room multi-Guest confusion** — the dashboard never shows two participants when nobody is calling.

---

## Problem

### Evidence

Daily dashboard screenshot taken 2026-05-05 09:10 UTC showed two participants in `voxtera-demo`:

| Participant | user_name | Joined (UTC)        | Duration |
| ----------- | --------- | ------------------- | -------- |
| `d9bbf1b0…` | Voxtera   | 2026-05-05 09:03:07 | 7 min    |
| `c201dba0…` | Guest     | 2026-05-05 09:04:49 | 3 min    |

The Voxtera bot joined **1 minute 42 seconds before** the Guest and **stayed in the room after the Guest left**. This is the design today, not a bug — but it is the root cause of the participant-minute burn we observed at the 3000-minute mark.

### Root cause in code

`bot.py:159` calls `runner.run(task)`, which runs `DailyTransport.join()` against a fixed room from `settings.daily_room_name` regardless of whether anyone is on the other end. There is no "wait for a Guest to arrive" gate.

`_eject_stale_bots(settings)` in `pipeline.py:300` only runs at startup, so a stuck bot from a crashed previous process is cleared — but a bot from a *successfully exited* user session is never cleaned up because it never exits.

### Secondary symptom — greeting bug

Because the same bot pipeline is reused across Guests:

- `GreetingController._last_greeting_at` (controllers.py:608) persists between calls.
- The 3-second debounce can swallow a fresh Guest's greeting if their `voxtera-ready` arrives within 3 s of the previous Guest's.
- LLM context aggregator state also persists.

Fixing the spawn model fixes this for free: every Guest gets a fresh process with fresh state.

---

## Goals

1. The Voxtera bot is **only** in the Daily room while a Guest is actively calling.
2. The user-perceived "Start" latency stays under ~7 seconds end-to-end.
3. The IPC channel between launcher and bot is a real queue — not stdout scraping.
4. Failure modes (bot crash, slow startup, double-click on Start) are handled explicitly, not by accident.
5. No new infrastructure on the Droplet. No Redis, no Docker per call, no message broker. Reuse the HTTP server that's already running.

## Non-Goals

- **Multi-tenant / multi-room support.** This plan locks the demo to one concurrent session. The second Guest gets a `409 Busy`. Multi-room is a future change that swaps the queue backend and adds Daily REST room creation.
- **Pipecat Cloud migration.** Out of scope. The same architectural shape will port cleanly when we go to Pipecat Cloud later.
- **Pre-warmed bot pool.** A pool would shave ~3 s off the cold-start by keeping warm processes ready. Worth doing if cold-start UX becomes a complaint, not before.

---

## Architecture

```
┌──────────┐                    ┌──────────────┐                ┌──────────────┐
│ Browser  │                    │  serve.py    │                │ voxtera.bot  │
│ demo.html│                    │  (launcher + │                │ (subprocess  │
│          │                    │  static srv) │                │  spawned per │
│          │                    │              │                │  Start click)│
└────┬─────┘                    └──────┬───────┘                └──────┬───────┘
     │                                 │                               │
     │ POST /api/start-session         │                               │
     ├────────────────────────────────►│                               │
     │                                 │ create q = queue.Queue()      │
     │                                 │ registry[sid] = q             │
     │                                 │                               │
     │                                 │ subprocess.Popen(             │
     │                                 │   ["python", "-m",            │
     │                                 │    "voxtera.bot"],            │
     │                                 │   env={SESSION_ID, …})        │
     │                                 ├──────────────────────────────►│
     │                                 │                               │ warm models
     │                                 │                               │ join Daily as
     │                                 │                               │  "Voxtera"
     │                                 │                               │ on_joined_meeting
     │                                 │ POST /api/bot-event           │
     │                                 │ {sid, type:"ready"}           │
     │                                 │◄──────────────────────────────┤
     │                                 │                               │
     │                                 │ q.put(event)                  │
     │                                 │ blocked q.get() unblocks      │
     │                                 │                               │
     │ 200 {room_url, session_id}      │                               │
     │◄────────────────────────────────┤                               │
     │                                 │                               │
     │ callObject.join(room_url)       │                               │
     │ as "Guest"                      │                               │
     │═════════════════════════════════════════════════════════════════│
     │              Daily WebRTC room — voice flows                    │
     │═════════════════════════════════════════════════════════════════│
     │                                 │                               │
     │ user clicks End                 │                               │
     │ callObject.leave()              │                               │
     │ ───── Daily fires participant-left to the bot ────►             │
     │                                 │                               │ EndFrame
     │                                 │                               │ pipeline drains
     │                                 │ POST /api/bot-event           │
     │                                 │ {sid, type:"exiting"}         │
     │                                 │◄──────────────────────────────┤
     │                                 │ reaper thread:                │ process exits
     │                                 │   Popen.wait() returns        │
     │                                 │   registry.pop(sid)           │
     │                                 │                               │
```

A sequence diagram of the same flow with all four actors (Browser, launcher, bot, Daily room) is rendered in the design review chat — refer to that for the timeline view.

---

## IPC design — why HTTP callback + `queue.Queue`

### Requirements

- The launcher's `/api/start-session` handler must **block** until the bot has actually joined the Daily room (otherwise the browser would `callObject.join()` before the bot is there, and the user would hear silence).
- The bot must be able to send **multiple kinds of events** back: `ready`, `error`, `exiting`. This will grow over time — heartbeats, metrics, transcript fragments — so the channel must be extensible.
- The mechanism must work with `serve.py`'s existing `socketserver.ThreadingTCPServer` — i.e. blocking semantics, no asyncio refactor required.
- No new daemons on the Droplet.

### Choice: HTTP callback + `queue.Queue`

The bot does an HTTP `POST` to a localhost endpoint on the launcher whenever a state change happens. The launcher has a small `SessionRegistry` keyed by `session_id` whose values are `queue.Queue` instances. The `/api/start-session` request handler — running in a `ThreadingTCPServer` worker thread — calls `q.get(timeout=15)` and blocks until the bot's POST arrives and the `/api/bot-event` handler does `q.put(event)`.

Why this fits:

- **Real queue.** `queue.Queue` is thread-safe, has the same `put/get` interface as `asyncio.Queue`, and is the canonical Python tool for thread-to-thread message passing.
- **Decoupled.** The bot never talks to the start-session handler directly. It only talks to `/api/bot-event`. Swapping the queue backend (in-memory → Redis Streams → Kafka) is a 30-line refactor in `serve.py`; the bot is unaffected.
- **Debuggable.** Every event lands in the launcher's HTTP access log automatically. No special tracing.
- **Extensible.** Add new event types by extending the JSON schema, not by adding new sockets.
- **Zero new infrastructure.** Reuses the HTTP server already serving the demo page.

### Alternatives considered

| Mechanism                      | Why rejected                                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------------------------- |
| stdout marker (`[bot] READY`)  | Mixes with logs. Breaks if anyone adds a `print()`. Hard to extend to multiple event types.          |
| Unix domain socket             | Fine, but ~150 LOC of socket framing for a benefit (microsecond latency) we don't need.              |
| `multiprocessing.Queue`        | Forces the bot to be a `multiprocessing.Process` of the launcher. Architectural lock-in we want to avoid. |
| Named pipe (FIFO)              | Survives the round trip, but blocking-read + restart semantics are fiddly. Socket complexity without socket benefits. |
| Redis pub/sub or Streams       | Real broker, but adds Redis as a Droplet dependency. Worth doing once we have N>1 bot hosts; overkill for now. |
| Filesystem watch (status file) | Works for one-shot signals; ugly for ongoing event streams.                                          |

The intent is to start with HTTP + `queue.Queue` and **keep the queue interface narrow** so swapping it for Redis later is mechanical.

---

## Detailed component changes

### 1. `src/voxtera/launcher_client.py` (new, ~40 LOC)

Tiny HTTP client used by the bot to post events back to the launcher. Lives in the bot's process. Reads `VOXTERA_SESSION_ID` and `VOXTERA_LAUNCHER_URL` from env at import time.

Public API:

```python
async def post_event(event_type: str, **payload) -> None: ...
```

Failure handling: on connection error, log a warning and continue. The bot must not crash because the launcher is unreachable — that would break local-mode runs (`TRANSPORT_MODE=local`) where there is no launcher.

When `VOXTERA_LAUNCHER_URL` is unset, `post_event` is a no-op. This preserves backward compatibility with `make run` for local development.

### 2. `src/voxtera/bot.py` (additions, ~20 LOC)

Two new event handlers on the Daily transport:

```python
@transport.event_handler("on_joined_meeting")
async def _on_joined(transport):
    await launcher_client.post_event("ready")

@transport.event_handler("on_participant_left")
async def _on_participant_left(transport, participant, reason):
    if participant.get("info", {}).get("userName") == "Guest":
        await launcher_client.post_event("exiting", reason="guest_left")
        await task.queue_frame(EndFrame())
```

The `participant_left` handler is the **fast-exit path**. Without it, the bot would only exit after `PIPELINE_IDLE_TIMEOUT_SECS` (60 s by default). With it, the bot exits within ~1 s of the Guest leaving — cleaner Daily participant-minute accounting, faster session-slot release.

`EndFrame` then propagates through the pipeline; in the existing `finally` block in `run_bot()` the runner returns and the process exits cleanly with `rc=0`.

### 3. `demo-hotel/serve.py` (additions, ~120 LOC)

New module-level state:

```python
class SessionRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}  # session_id -> {queue, process, state}

    def create(self, session_id: str) -> queue.Queue: ...
    def deliver(self, session_id: str, event: dict) -> None: ...
    def reap(self, session_id: str) -> None: ...
    def is_busy(self) -> bool: ...

REGISTRY = SessionRegistry()
```

Two new endpoints:

#### `POST /api/start-session`

1. If `REGISTRY.is_busy()` → return `409 Busy`.
2. Generate `session_id = uuid4().hex`.
3. Create queue: `q = REGISTRY.create(session_id)`.
4. Spawn subprocess:

   ```python
   env = {
       **os.environ,
       "VOXTERA_SESSION_ID": session_id,
       "VOXTERA_LAUNCHER_URL": "http://localhost:8080/api/bot-event",
       "BOT_AUTO_JOIN": "true",  # subprocess does join immediately
       # forwarded user choices
       "GREETING_LANGUAGE": body.get("language", "auto"),
       "DEFAULT_TTS_VOICE": body.get("voice", "nova"),
       # …etc
   }
   proc = subprocess.Popen([sys.executable, "-m", "voxtera.bot"], env=env, ...)
   REGISTRY.attach_process(session_id, proc)
   ```
5. Start a reaper thread:

   ```python
   threading.Thread(target=lambda: (proc.wait(), REGISTRY.reap(session_id)), daemon=True).start()
   ```
6. Block on the queue:

   ```python
   try:
       event = q.get(timeout=15)
   except queue.Empty:
       proc.kill()
       REGISTRY.reap(session_id)
       return 504, {"error": "bot startup timeout"}
   ```
7. If event is `ready` → return `200 {room_url, session_id}`. If `error` → return `500 {error}`.

#### `POST /api/bot-event`

```python
{
  "session_id": "abc123…",
  "type": "ready" | "error" | "exiting",
  "reason": "...",            # optional
  "error": "..."              # optional, when type == "error"
}
```

Handler:

```python
def _handle_bot_event(self):
    body = json.loads(self.rfile.read(...))
    REGISTRY.deliver(body["session_id"], body)
    self.send_response(204)
    self.end_headers()
```

### 4. `demo-hotel/demo.html` (modifications, ~50 LOC)

Replace the current `btn-start.onclick` body. New flow:

```javascript
btn.onclick = async function() {
  setButtonState('spawning', '⏳ Starting bot...');
  try {
    const resp = await fetch('/api/start-session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        language: langSelect.value,
        llm: llmSelect.value,
        stt: sttSelect.value,
        ttsProvider: ttsProviderSelect.value,
        voice: voiceSelect.value,
      }),
    });
    if (resp.status === 409) {
      showError('Demo is busy. Please try again in a minute.');
      return setButtonState('idle');
    }
    if (!resp.ok) {
      const j = await resp.json().catch(() => ({}));
      showError('Failed to start bot: ' + (j.error || resp.status));
      return setButtonState('idle');
    }
    const {room_url, session_id} = await resp.json();
    setButtonState('joining', '⏳ Joining session...');
    await joinDailyRoom(room_url);  // existing callObject.join code, refactored
    setButtonState('live');
  } catch (e) {
    showError('Error: ' + e.message);
    setButtonState('idle');
  }
};
```

Four UI states drive button text and `display` toggles:

| State    | Button text          | Visible elements                  |
| -------- | -------------------- | --------------------------------- |
| idle     | 🎙️ Start             | welcome panel, lang/model pickers |
| spawning | ⏳ Starting bot…     | welcome panel (disabled)          |
| joining  | ⏳ Joining session…  | welcome panel (disabled)          |
| live     | (Start hidden)       | transcript, orb, End/Mute buttons |

### 5. `src/voxtera/config.py` (small addition)

```python
@dataclass
class Settings:
    ...
    bot_auto_join: bool = True  # subprocess sets this to true; legacy mode also true
```

The existing always-on `make run` flow continues to work (it sets `BOT_AUTO_JOIN=true` by default). The launcher path also sets it to `true`. We keep the field so a future "warm pool" mode can set it to `false` and have the bot wait for an explicit signal before joining.

---

## Failure modes

| Scenario                                          | Detection                                       | Recovery                                                                   |
| ------------------------------------------------- | ----------------------------------------------- | -------------------------------------------------------------------------- |
| Bot crashes during model warmup                   | Reaper thread sees `proc.wait()` returns ≠ 0 before `ready` event | Reaper puts `{type: "error"}` on the queue. Start-session returns 500. |
| Bot starts but never joins Daily (network hiccup) | `q.get(timeout=15)` raises `queue.Empty`        | Launcher kills subprocess, returns 504, browser shows error.               |
| User double-clicks Start                          | Frontend disables button on first click; backend `is_busy()` would also reject the second | Second click no-ops on the frontend; backend is the safety net.            |
| Two browsers, one Droplet                         | `is_busy()` true on second start                | Second browser gets 409 Busy. UI shows "Demo is busy."                     |
| User closes tab without clicking End              | Daily fires `participant-left` for Guest        | Bot's `on_participant_left` handler queues `EndFrame`, process exits.      |
| Bot exits cleanly mid-call (segfault, OOM)        | Reaper thread sees exit                         | Browser stays in `live` state until next user action; will see Daily disconnect within seconds and trigger reconnect overlay (existing flow). |
| Launcher restarts mid-call                        | Bot's `post_event` to launcher fails            | Bot logs warning, continues. Browser keeps working until session naturally ends. |
| Daily room hits participant-minute cap            | `callObject.join` errors                        | Existing error handler in `demo.html` shows error box. Out of scope for this change. |

---

## Implementation phases

The plan is intentionally split into small, individually verifiable phases. **Each phase is a stop-and-review checkpoint.**

### Phase 1 — Bot-side ready signal (1-2h)

**Files:** `src/voxtera/launcher_client.py` (new), `src/voxtera/bot.py` (additions)

Add the `launcher_client.py` module and the `on_joined_meeting` event handler. With `VOXTERA_LAUNCHER_URL` unset, this is a no-op for the existing `make run` flow.

**Verification:** Start the bot manually with `VOXTERA_LAUNCHER_URL=http://localhost:9999/dummy`. The bot should log a warning that the POST failed (because nothing is listening on 9999) but otherwise behave normally. Live voice call still works.

### Phase 2 — Bot-side fast exit on Guest leave (30min)

**Files:** `src/voxtera/bot.py` (one event handler)

Add `on_participant_left` handler. Verify with a manual run: open the demo, join, then leave from the browser. The bot logs should show `participant_left` and the process should exit within ~1 s.

### Phase 3 — Launcher endpoints (2-3h)

**Files:** `demo-hotel/serve.py` (SessionRegistry, /api/start-session, /api/bot-event)

Implement both new endpoints and the `SessionRegistry`. Reaper thread, timeout, busy-rejection.

**Verification:** With the legacy frontend untouched, manually `curl POST /api/start-session` and observe:

1. Bot subprocess starts, joins Daily.
2. `POST /api/bot-event {type:"ready"}` arrives at the launcher.
3. `curl` response returns `{room_url, session_id}`.
4. `curl POST /api/start-session` a second time returns `409`.
5. Kill the bot subprocess by hand. Reaper logs the cleanup. `curl POST /api/start-session` works again.

### Phase 4 — Frontend wiring (1-2h)

**Files:** `demo-hotel/demo.html`

Replace `btn-start.onclick`. Add the four UI states. Refactor existing `callObject.join` block into a `joinDailyRoom(roomUrl)` function so the new flow can call it.

**Verification:** Click Start. Observe button text transitions: 🎙️ → ⏳ Starting bot… → ⏳ Joining session… → (hidden) → live UI. Greeting plays. End the session. Watch Daily dashboard show the bot leave within 1 s of the Guest.

### Phase 5 — Kill the always-on systemd / docker-compose entry (15min)

**Files:** Droplet's `docker-compose.yml` or systemd unit (whichever is being used)

Once Phases 1-4 are verified, remove the `voxtera-bot` service from `docker-compose.yml`. The `serve.py` HTTP server stays — it both serves the demo and spawns bots on demand. `_eject_stale_bots(settings)` is still useful as a safety net (kills any leftover from a crashed previous spawn).

### Phase 6 — Cleanup (30min)

- Remove the unused `TTSSpeakFrame` queue at startup in `bot.py:135-142` for the non-Daily path? **Keep it** — local mode still works that way. Only the Daily path is changing.
- Update `docs/runbook.md` with the new operational model.
- Update `docs/architecture.md` to reflect on-demand spawn.

---

## Testing plan

### Unit tests

- `tests/test_launcher_client.py` — `post_event()` with mock `aiohttp` session.
- `tests/test_session_registry.py` — `create`, `deliver`, `reap`, `is_busy`. Concurrency test with two threads.

### Integration test (manual)

Documented as a checklist in `docs/runbook.md`:

1. Cold start: backend up, no bot running. Daily dashboard shows zero participants.
2. Click Start. Within 7 s, dashboard shows one Guest + one Voxtera. Greeting plays.
3. Click End. Within 1 s, dashboard shows zero participants.
4. Repeat 5×. State is fresh each time (greeting always plays).
5. Two browsers race-click Start. Second one gets a clean "busy" message.
6. Kill the bot subprocess from the shell mid-call. Browser sees Daily disconnect, reconnect overlay shows.
7. Stop the launcher mid-call. Bot logs a warning, voice call keeps working until user hangs up.

### Daily participant-minute audit

Run the demo through Phase 4. Verify Daily dashboard total monthly participant-minutes is consistent with `(call_count × call_duration × 2 participants)` — no idle bot consumption between calls.

---

## Future migration paths

The narrowness of the queue interface is the point. Anything we want to change later is a local refactor:

- **Multi-tenant / multi-room.** Swap `SessionRegistry` from "one session at a time" to "session_id → room URL." Add Daily REST API calls to create rooms and tokens per call. The bot, the queue contract, and the frontend handshake stay the same.
- **Pre-warmed pool.** Spawn N idle bots at startup with `BOT_AUTO_JOIN=false`. On `/api/start-session`, the launcher tells one of the warm bots to join via a new event type `{type: "join_now", room_url}`. Cold-start drops to ~500 ms.
- **Redis-backed queue.** Replace `queue.Queue` with a Redis Streams consumer group. `SessionRegistry.deliver()` `XADD`s; `SessionRegistry._wait_for()` `XREAD`s. The launcher becomes horizontally scalable.
- **Pipecat Cloud.** Replace the local subprocess spawn with an HTTPS call to Pipecat Cloud's session API. The frontend doesn't change. The launcher shrinks to ~30 lines.

---

## Open questions

These are deliberately left open for the user to decide before implementation:

1. **Is `BOT_AUTO_JOIN=false` mode worth keeping?** Argument for: lets us add a pre-warmed pool later without refactoring. Argument against: extra config surface area for a future feature. **Default plan:** keep the field but always set it to `true` for now.
2. **Should we wait for `EndFrame` to drain before letting the launcher reap?** Today the bot exits the process and the reaper runs `Popen.wait()`. If the bot exits before the final TTS audio reaches Daily, the user might miss the last word. **Default plan:** `bot.py`'s existing `finally` block in `run_bot()` already drains; trust that.
3. **What's the right `q.get(timeout=N)` value?** First-time RAG warmup can take ~5 s. Current proposal: 15 s. **Default plan:** 15 s, configurable via `VOXTERA_SPAWN_TIMEOUT_SECS`.
4. **Logging level on the new endpoints?** `INFO` for `/api/start-session`, `DEBUG` for `/api/bot-event` (high volume). **Default plan:** as stated.

---

## References

- Sequence diagram: rendered in design review chat (2026-05-05).
- Daily dashboard screenshot showing two-participant state: 2026-05-05 09:10 UTC.
- Existing greeting flow: `src/voxtera/controllers.py:586-634`, `src/voxtera/pipeline.py:654-663`, `demo-hotel/demo.html:540`.
- Existing transport setup: `src/voxtera/pipeline.py:299-336`.
