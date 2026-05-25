"""Simple HTTP server for the demo frontend with TTS test and chat endpoints.

Serves static files for the demo page and exposes:

- ``POST /api/tts-test`` — real OpenAI / Google / Cartesia / ElevenLabs TTS so
  the browser can play the bot's greeting in the selected voice and language.
- ``POST /api/chat`` — full conversational endpoint that uses the Voxtera
  system prompt, RAG retrieval over hotel knowledge, and OpenAI GPT for
  the LLM response.  Returns JSON with ``text`` and optional base64 TTS
  audio so chat mode works entirely over HTTP (no Daily / daily-python).
- ``GET  /api/admin/health`` — admin probe; reports whether the server has
  the env vars it needs to support the admin page.
- ``GET  /api/admin/sessions`` — live snapshot of who is in the configured
  Daily room (powered by Daily's ``/v1/presence`` endpoint).
- ``POST /api/admin/eject`` — eject one or more participants by id.
- ``POST /api/admin/end-session`` — eject every participant in the room.

The admin endpoints require the ``X-Admin-Token`` header to match
``VOXTERA_ADMIN_TOKEN``. When that env var is unset the admin endpoints
return ``503`` so the page can render a clear "admin disabled" state.
"""

import asyncio
import base64
import concurrent.futures as _futures
import contextlib
import http.server
import json
import os
import queue as _queue
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

# Ensure the voxtera package is importable when running from demo-hotel/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import audit_log as _audit  # noqa: E402  — local module, writes logs/audit/

from voxtera.actions import (  # noqa: E402
    build_openai_tools,
    compose_system_prompt,
    load_hotel_config,
)
from voxtera.actions.logging_sink import LoggingSink  # noqa: E402
from voxtera.actions.ticket import Category, Ticket  # noqa: E402
from voxtera.admin import (  # noqa: E402
    DailyAPIError,
    create_room,
    delete_room,
    eject_participants,
    list_room_participants,
)
from voxtera.lang_config import (  # noqa: E402
    LANG_CONFIG,
    google_locale_for,
    translation_name_for,
)
from voxtera.prompts.greetings import GREETINGS  # noqa: E402
from voxtera.prompts.system_prompt import SYSTEM_PROMPT  # noqa: E402

# Thread pool for non-blocking TTS synthesis — allows LLM streaming and TTS
# to run in parallel so sentence audio overlaps with token generation.
_tts_executor = _futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="demo-tts")

# Module-level OpenAI client — reuses HTTP connection pool across requests,
# avoiding TCP+TLS handshake overhead (~200-400ms) on every /api/chat call.
import openai as _openai_mod  # noqa: E402

_oai_client: "_openai_mod.OpenAI | None" = None


def _get_oai_client() -> "_openai_mod.OpenAI":
    global _oai_client
    if _oai_client is None:
        _oai_client = _openai_mod.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    return _oai_client


# ---------------------------------------------------------------------------
# Admin config — read once at import time. Empty values disable the admin
# endpoints (they return 503 with a structured error so the page can render a
# clear "admin disabled" state instead of a generic failure).
# ---------------------------------------------------------------------------
_ADMIN_TOKEN: str | None = os.environ.get("VOXTERA_ADMIN_TOKEN") or None
_DAILY_API_KEY: str | None = os.environ.get("DAILY_API_KEY") or None
_DAILY_ROOM_NAME: str | None = os.environ.get("DAILY_ROOM_NAME") or None
_DAILY_DOMAIN: str | None = os.environ.get("DAILY_DOMAIN") or None
_BOT_NAME: str = os.environ.get("BOT_NAME") or "Voxtera"
_DAILY_DYNAMIC_ROOMS: bool = os.environ.get("DAILY_DYNAMIC_ROOMS", "true").lower() not in (
    "0",
    "false",
    "no",
)
_DAILY_ROOM_MAX_PARTICIPANTS: int = int(os.environ.get("DAILY_ROOM_MAX_PARTICIPANTS", "2"))
_MAX_CONCURRENT_SESSIONS: int = int(os.environ.get("DAILY_MAX_CONCURRENT_SESSIONS", "50"))
_SESSION_TIMEOUT_SECS: int = int(os.environ.get("VOXTERA_SESSION_TIMEOUT_SECS", "180"))

# Tiny per-process cache so two browsers polling at 3 s don't double the load
# on Daily REST. ``_PRESENCE_CACHE_TTL_SECS`` is short enough to stay live
# but long enough to absorb the 1 s polling burst of two operators looking
# at the same page.
_PRESENCE_CACHE_TTL_SECS: float = 0.5
_presence_cache: dict[str, object] = {"fetched_at": 0.0, "value": None}

# ---------------------------------------------------------------------------
# Trace plane — buffers events streamed from the bot subprocess and fans
# them out to /trace.html dashboard subscribers via Server-Sent Events.
# ---------------------------------------------------------------------------

# Default bot tune-server port for ``make run`` / always-on legacy mode.
# Overridable via env so multiple bots on one host don't collide.
_DEFAULT_BOT_PORT: int = int(os.environ.get("VOXTERA_BOT_PORT_BASE", "9091"))


class TraceEventBuffer:
    """Ring buffer of trace events plus thread-safe SSE fan-out.

    Each subscriber gets a per-subscriber ``queue.Queue``. Slow subscribers
    drop their oldest event rather than blocking the producer (the bot's
    HTTP POST handler thread). Late subscribers receive a small tail of the
    ring buffer on connect so the dashboard is populated immediately.
    """

    def __init__(self, *, buffer_size: int = 5000, subscriber_queue_size: int = 1000) -> None:
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        self._subscribers: list[_queue.Queue] = []
        self._subscriber_queue_size = subscriber_queue_size
        self._lock = threading.Lock()

    def add(self, event: dict) -> None:
        """Append an event from the bot. Fan out to subscribers."""
        with self._lock:
            self._buffer.append(event)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except _queue.Full:
                with contextlib.suppress(_queue.Empty):
                    q.get_nowait()
                with contextlib.suppress(_queue.Full):
                    q.put_nowait(event)

    def add_many(self, events: list[dict]) -> None:
        """Bulk append (the bot batches POSTs)."""
        for e in events:
            self.add(e)

    def subscribe(self) -> _queue.Queue:
        q: _queue.Queue = _queue.Queue(maxsize=self._subscriber_queue_size)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: _queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            if limit >= len(self._buffer):
                return list(self._buffer)
            return list(self._buffer)[-limit:]

    def stats(self) -> dict:
        with self._lock:
            return {
                "buffered": len(self._buffer),
                "subscribers": len(self._subscribers),
            }


_TRACE_BUFFER = TraceEventBuffer()


# ---------------------------------------------------------------------------
# Session history — persist trace events to disk per session so past
# conversations can be replayed in the dashboard. NDJSON per session is the
# simplest durable format: append-only, line-by-line parseable, no schema
# migration risk. Meta sidecar (`{id}.meta.json`) holds derived summary fields
# so the listing endpoint doesn't need to parse the full event log.
# ---------------------------------------------------------------------------

# Explicit None/empty check — Path("").expanduser() returns Path(".") which
# is truthy, so `or` fallback never runs and files end up in cwd.
_env_trace_dir = os.environ.get("VOXTERA_TRACE_DIR") or ""
_TRACE_DIR = (
    Path(_env_trace_dir).expanduser().resolve()
    if _env_trace_dir
    else (Path(__file__).resolve().parent / "traces")
)


class SessionStore:
    """Append trace events to per-session NDJSON files and manage their lifecycle.

    File layout under ``_TRACE_DIR``::

        {session_id}.ndjson       # one JSON event per line, append-only
        {session_id}.meta.json    # summary written when session is finalized

    Thread-safe: every public method takes the per-store lock. File handles
    are kept open for the active session(s) to avoid open/close per event,
    and flushed after every batch so a crashed launcher loses at most one
    pending batch (the in-memory ring buffer is still authoritative for
    live viewing).
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory.resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # session_id -> open file handle for fast append
        self._handles: dict[str, object] = {}
        # session_id -> {started_at, turn_ids, transcript_first, ...} accumulator
        # for cheap meta generation at finalize time without re-scanning NDJSON.
        self._accum: dict[str, dict] = {}
        # Print on startup so the operator always knows where files go.
        # Uses print rather than logger because serve.py doesn't configure
        # loguru itself (the bot does); print lands in the launcher's stdout.
        print(f"[trace-store] persisting trace events to {self._dir}", flush=True)

    def _ndjson_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.ndjson"

    def _meta_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.meta.json"

    def append(self, session_id: str, events: list[dict]) -> None:
        """Append a batch of events for one session. Creates the file on first
        call. Updates the in-memory accumulator used by :meth:`finalize`.
        """
        if not session_id or not events:
            return
        with self._lock:
            fh = self._handles.get(session_id)
            if fh is None:
                fh = open(self._ndjson_path(session_id), "a", encoding="utf-8")  # noqa: SIM115
                self._handles[session_id] = fh
                self._accum[session_id] = {
                    "started_at": events[0].get("ts_ms"),
                    "ended_at": None,
                    "turn_ids": set(),
                    "providers": None,
                    "transcript_first": None,
                    "transcript_last": None,
                    "event_count": 0,
                }
            acc = self._accum[session_id]
            for ev in events:
                fh.write(json.dumps(ev, separators=(",", ":")) + "\n")
                acc["event_count"] += 1
                if ev.get("ts_ms"):
                    acc["ended_at"] = ev["ts_ms"]
                if ev.get("turn_id"):
                    acc["turn_ids"].add(ev["turn_id"])
                data = ev.get("data") or {}
                if data.get("event") == "session_providers":
                    acc["providers"] = {k: v for k, v in data.items() if k != "event"}
                if data.get("event") == "transcript":
                    text = (data.get("text") or "").strip()
                    if text:
                        if acc["transcript_first"] is None:
                            acc["transcript_first"] = text
                        acc["transcript_last"] = text
            fh.flush()

    def finalize(self, session_id: str) -> None:
        """Close the NDJSON handle and write the meta sidecar.

        Called from :class:`BotSessionRegistry.reap` when the bot subprocess
        exits. Idempotent: a finalize on a session with no events is a no-op.
        """
        with self._lock:
            fh = self._handles.pop(session_id, None)
            acc = self._accum.pop(session_id, None)
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()
            if acc is None or acc.get("event_count", 0) == 0:
                # Nothing was written — clean up an empty file if it exists.
                with contextlib.suppress(FileNotFoundError):
                    self._ndjson_path(session_id).unlink()
                return
            meta = {
                "session_id": session_id,
                "started_at": acc["started_at"],
                "ended_at": acc["ended_at"],
                "turn_count": len(acc["turn_ids"]),
                "event_count": acc["event_count"],
                "providers": acc["providers"],
                "transcript_first": acc["transcript_first"],
                "transcript_last": acc["transcript_last"],
            }
            self._meta_path(session_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def list_sessions(self) -> list[dict]:
        """Return all stored sessions sorted by ``started_at`` descending.

        Includes both finalized sessions (meta file exists) and in-progress
        sessions (NDJSON exists, meta does not — synthesize meta on the fly).
        """
        with self._lock:
            # Snapshot in-memory accumulators for in-progress sessions.
            in_progress = {sid: dict(acc) for sid, acc in self._accum.items()}

        sessions: list[dict] = []
        for ndjson_file in self._dir.glob("*.ndjson"):
            session_id = ndjson_file.stem
            meta_file = self._meta_path(session_id)
            if meta_file.exists():
                try:
                    sessions.append(json.loads(meta_file.read_text()))
                    continue
                except Exception:
                    pass  # fall through to synthesized meta
            # In-progress or meta-less session: synthesize a meta-like dict.
            acc = in_progress.get(session_id)
            if acc:
                sessions.append(
                    {
                        "session_id": session_id,
                        "started_at": acc["started_at"],
                        "ended_at": acc["ended_at"],
                        "turn_count": len(acc["turn_ids"]),
                        "event_count": acc["event_count"],
                        "providers": acc["providers"],
                        "transcript_first": acc["transcript_first"],
                        "transcript_last": acc["transcript_last"],
                        "in_progress": True,
                    }
                )
            else:
                # Orphan ndjson with no accumulator (left from a previous launcher
                # process that crashed before finalize). Mark as such.
                sessions.append(
                    {
                        "session_id": session_id,
                        "started_at": None,
                        "ended_at": None,
                        "turn_count": None,
                        "event_count": None,
                        "providers": None,
                        "transcript_first": None,
                        "transcript_last": None,
                        "orphan": True,
                    }
                )
        sessions.sort(key=lambda s: s.get("started_at") or 0, reverse=True)
        return sessions

    def read_events(self, session_id: str) -> list[dict] | None:
        """Return the full event list for a session, or None if not found.

        Reads the NDJSON line-by-line; tolerant of trailing partial lines
        (which can happen on an in-progress session).
        """
        path = self._ndjson_path(session_id)
        if not path.exists():
            return None
        events: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    # Partial trailing write on an active session — stop here.
                    break
        return events

    def delete(self, session_id: str) -> bool:
        """Delete both files for a session. Returns True if anything was removed.

        Refuses to delete the currently-active session (one with an open file
        handle); the operator should end the session first.
        """
        with self._lock:
            if session_id in self._handles:
                return False
            removed = False
            for p in (self._ndjson_path(session_id), self._meta_path(session_id)):
                if p.exists():
                    with contextlib.suppress(Exception):
                        p.unlink()
                        removed = True
            return removed


_SESSION_STORE = SessionStore(_TRACE_DIR)


def _get_bot_tune_port(session_id: str | None = None) -> int | None:
    """Return the tune port for a session, or the first active session if None."""
    with REGISTRY._lock:
        if session_id:
            return REGISTRY._sessions.get(session_id, {}).get("tune_port")
        # Backward compat: admin tune endpoint doesn't always know session_id.
        for sess in REGISTRY._sessions.values():
            port = sess.get("tune_port")
            if port is not None:
                return port
    return None


# ---------------------------------------------------------------------------
# Phase 3 — On-demand bot launcher
#
# The launcher spawns a fresh ``python -m voxtera.bot`` subprocess each time
# the browser hits ``/api/start-session``. The subprocess joins the Daily
# room, then POSTs a ``{type:"ready"}`` event back to ``/api/bot-event``;
# the start-session handler is blocked on a ``queue.Queue.get()`` and unblocks
# the moment the event arrives, then returns the room URL to the browser so
# the Guest can join.
#
# Concurrency: one in-flight session at a time. Second Start clicks while a
# session is live get a 409 Busy. Multi-tenant is out of scope; rationale
# in ``docs/ON_DEMAND_BOT_SPAWN.md``.
# ---------------------------------------------------------------------------

# Spawn timeout: if the bot doesn't post ``{type:"ready"}`` within this many
# seconds, the launcher kills it and returns 504. Embedding model warmup +
# Daily join can take ~20 s on a cold start (1 vCPU droplet), so 30 s leaves
# comfortable headroom.
_SPAWN_TIMEOUT_SECS: float = float(os.environ.get("VOXTERA_SPAWN_TIMEOUT_SECS", "30"))

# Path to the Voxtera project root (parent of demo-hotel/). The bot subprocess
# runs from here so its ``load_dotenv()`` call finds the project's ``.env``.
_VOXTERA_ROOT: Path = Path(__file__).resolve().parent.parent

# Resolved at startup once we know the actual port — see the ``__main__``
# block. The bot subprocess receives this as ``VOXTERA_LAUNCHER_URL`` env var
# and ``launcher_client.post_event`` POSTs to ``{LAUNCHER_BASE_URL}/api/bot-event``.
LAUNCHER_BASE_URL: str = ""


class BotSessionBusyError(Exception):
    """Raised when ``BotSessionRegistry.start`` is called while a session is live."""

    def __init__(self, active_session: str) -> None:
        super().__init__(f"Another session is active: {active_session}")
        self.active_session = active_session


class BotSessionRegistry:
    """Thread-safe multi-slot registry for in-flight bot sessions.

    Supports up to ``_MAX_CONCURRENT_SESSIONS`` concurrent sessions. Each
    session is keyed by a UUID and owns a ``queue.Queue`` for events flowing
    back from the bot subprocess.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def start(self, session_id: str) -> "_queue.Queue":
        """Reserve a slot and return a fresh queue for this session."""
        q: _queue.Queue = _queue.Queue()
        with self._lock:
            if len(self._sessions) >= _MAX_CONCURRENT_SESSIONS:
                raise BotSessionBusyError(f"{len(self._sessions)} sessions active")
            self._sessions[session_id] = {"queue": q, "process": None}
        return q

    def attach_process(self, session_id: str, proc: subprocess.Popen) -> None:
        """Stash the Popen handle so the reaper can find it."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["process"] = proc

    def attach_watchdog(self, session_id: str, timer: threading.Timer) -> None:
        """Stash the watchdog Timer so reap() can cancel it on clean exit."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["watchdog"] = timer

    def attach_watchdog_warn(self, session_id: str, timer: threading.Timer) -> None:
        """Stash the warning Timer so reap() can cancel it on clean hang-up."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["watchdog_warn"] = timer

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

    def deliver(self, session_id: str, event: dict) -> None:
        """Push an event from the bot onto its session's queue.

        Stale events (bot posting after the session was already reaped) are
        silently dropped; this happens during the small race between bot exit
        and reaper cleanup.
        """
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return
            q = sess["queue"]
        q.put(event)

    def reap(self, session_id: str) -> None:
        """Free the slot. Always called from the reaper thread on Popen.wait()."""
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        # Cancel the watchdog timers so they don't fire after a clean hang-up.
        if sess is not None:
            for key in ("watchdog", "watchdog_warn"):
                wd = sess.get(key)
                if wd is not None:
                    with contextlib.suppress(Exception):
                        wd.cancel()
        # Wake any thread still blocked on q.get() so it can return cleanly
        # rather than hitting the timeout.
        if sess is not None:
            with contextlib.suppress(Exception):
                sess["queue"].put({"type": "_reaped"})
        # Best-effort: delete the Daily room if it was dynamically created.
        if _DAILY_DYNAMIC_ROOMS and sess is not None:
            room_name = sess.get("room_name")
            if room_name and _DAILY_API_KEY:
                with contextlib.suppress(Exception):
                    delete_room(api_key=_DAILY_API_KEY, room_name=room_name)
        # Close the session's NDJSON handle and write the meta sidecar so the
        # dashboard's session picker shows the right summary fields. Safe to
        # call for sessions that never produced trace events (no-op).
        with contextlib.suppress(Exception):
            _SESSION_STORE.finalize(session_id)

    def is_busy(self) -> bool:
        with self._lock:
            return len(self._sessions) >= _MAX_CONCURRENT_SESSIONS

    def active_sessions(self) -> list[str]:
        """Return all active session IDs."""
        with self._lock:
            return list(self._sessions.keys())

    def active_session(self) -> str | None:
        """Return the first active session ID (backward compat)."""
        with self._lock:
            if self._sessions:
                return next(iter(self._sessions))
            return None


REGISTRY = BotSessionRegistry()


def _spawn_bot(
    session_id: str,
    callback_url: str,
    tune_port: int,
    llm_model: str | None = None,
    room_name: str | None = None,
) -> subprocess.Popen:
    """Spawn ``python -m voxtera.bot`` as a subprocess for this session.

    The subprocess inherits the launcher's environment plus the env vars the
    bot's ``launcher_client`` and ``trace_server`` read at import time.
    ``tune_port`` is the localhost port the bot's TuneServer binds to; the
    launcher uses it to forward live-tune commands from the trace dashboard.
    """
    env = os.environ.copy()
    env["VOXTERA_SESSION_ID"] = session_id
    env["VOXTERA_LAUNCHER_URL"] = callback_url
    env["VOXTERA_BOT_PORT"] = str(tune_port)
    if llm_model:
        env["LLM_MODEL_OVERRIDE"] = llm_model
    if room_name:
        env["DAILY_ROOM_NAME"] = room_name

    proc = subprocess.Popen(
        [sys.executable, "-m", "voxtera.bot"],
        cwd=str(_VOXTERA_ROOT),
        env=env,
    )
    return proc


def _start_reaper_thread(session_id: str, proc: subprocess.Popen) -> None:
    """Background thread: wait for the bot subprocess to exit, then reap.

    This guarantees the slot is freed regardless of how the bot exits — clean
    EndFrame drain, crash, kill from spawn timeout, OOM. Without it the
    launcher would leak the slot and reject all subsequent Start clicks.
    """

    def _reap() -> None:
        rc = proc.wait()
        print(f"[launcher] bot session {session_id} exited (rc={rc})")
        REGISTRY.reap(session_id)

    t = threading.Thread(
        target=_reap,
        daemon=True,
        name=f"reaper-{session_id[:8]}",
    )
    t.start()


# ---------------------------------------------------------------------------
# Actions: OpenAI function-calling tool definition for create_ticket
# ---------------------------------------------------------------------------
_hotel_config = load_hotel_config("demo")
_ACTIONS_SYSTEM_PROMPT = compose_system_prompt(SYSTEM_PROMPT, _hotel_config)
_logging_sink = LoggingSink()

# ---------------------------------------------------------------------------
# Load tool definitions from one source (`voxtera.actions.tool`) and allow
# JSON no-code overrides from config/tools/*.json.
# ---------------------------------------------------------------------------
_TOOLS = build_openai_tools(_hotel_config)

# NOTE: language-code maps used to live here (_GOOGLE_LOCALE_MAP and
# _LANG_NAMES). They've moved to ``config/languages.json`` and are now
# accessed via :mod:`voxtera.lang_config`. Adding a new language is a
# one-line JSON edit instead of touching five Python dicts.


def _translate_greeting(text: str, lang: str, model: str) -> str:
    """Translate the greeting into ``lang`` using whichever provider owns ``model``.

    Routes by model-name prefix:
    * ``claude-*`` → Anthropic SDK
    * everything else (``gpt-*``, ``o1-*``, ``o3-*``) → OpenAI SDK

    Previously this was hardcoded to the OpenAI client, which 404'd whenever
    the demo's LLM dropdown was set to a Claude model and the target language
    wasn't in the hardcoded ``GREETINGS`` dict (the translation path is only
    hit on cache miss).
    """
    import os

    lang_name = translation_name_for(lang)
    prompt = (
        f"Translate the following greeting into {lang_name}. "
        "Return ONLY the translated text, nothing else.\n\n"
        f"{text}"
    )

    if model.lower().startswith("claude"):
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic returns content as a list of content blocks; we want the
        # text from the first block.
        return response.content[0].text.strip()

    import openai

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# RAG retriever (shared across requests, initialised lazily)
# ---------------------------------------------------------------------------
_retriever = None
_rag_ready = False


def _init_rag():
    """Initialise the RAG retriever once (thread-safe via GIL for first call)."""
    global _retriever, _rag_ready
    if _rag_ready:
        return
    try:
        from voxtera.rag.embeddings import embed_sync
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        embed_sync(["warmup"])  # warm up embedding model

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        import os

        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        if db_path.exists():
            store = ChunksStore(db_path)
            store.init_schema()
            _retriever = Retriever(store)
            print(f"[chat] RAG retriever ready (db={db_path})")
        else:
            print(f"[chat] RAG database not found at {db_path}, running without RAG")
    except Exception as exc:
        print(f"[chat] RAG init failed ({exc}), running without RAG")
    _rag_ready = True


def _rag_context(query: str, hotel_id: str = "demo") -> str:
    """Retrieve RAG chunks for a query and return formatted context string."""
    if _retriever is None:
        return ""
    try:
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(_retriever.retrieve(hotel_id=hotel_id, query=query))
        loop.close()
        if not results:
            return ""
        excerpts = "\n\n".join(f"[{r.doc_id}] {r.text}" for r in results)
        return (
            "Here are relevant excerpts from the hotel's information. Use them when "
            "answering, but only if they're relevant to the user's most recent "
            "question. If they don't answer that question, ignore them.\n\n" + excerpts
        )
    except Exception as exc:
        print(f"[rag] retrieval error: {exc}")
        return ""


# Initialise RAG at startup so the first chat request is fast.
_init_rag()

# ---------------------------------------------------------------------------
# Product knowledge-base RAG (powers /api/product-chat on landing pages)
# ---------------------------------------------------------------------------
_product_retriever = None
_product_rag_ready = False

_PRODUCT_SYSTEM_PROMPT = """\
You are a knowledgeable assistant for Voxtera — a real-time multilingual voice
agent platform for the tourism and hospitality industry.

Your role is to answer questions from hotel operators, potential customers,
investors, partners, and anyone curious about what Voxtera does, how it works,
and what value it delivers.

Behavioral rules:
- Answer only using the context provided and your knowledge of Voxtera from
  the conversation. Do not invent features or make claims not supported by the
  context.
- Be concise and clear — one to three short paragraphs maximum.
- Speak in first person as a Voxtera representative ("Voxtera does X", not
  "according to the document").
- If the question is not about Voxtera at all, politely say you can only help
  with Voxtera-related questions.
- Never reveal the raw context chunks or internal system details.
- If asked about pricing, direct the user to contact dan@voxtera.io.
"""


def _init_product_rag() -> None:
    """Initialise the product KB retriever once."""
    global _product_retriever, _product_rag_ready
    if _product_rag_ready:
        return
    try:
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        default_db = str(Path.home() / ".voxtera" / "voxtera-product.db")
        import os as _os

        db_path = Path(_os.environ.get("VOXTERA_PRODUCT_DB_PATH", default_db))
        if db_path.exists():
            store = ChunksStore(db_path)
            store.init_schema()
            _product_retriever = Retriever(store, top_k=5, min_score=0.20)
            print(f"[product-chat] Product RAG ready (db={db_path})")
        else:
            print(
                f"[product-chat] Product KB database not found at {db_path}. "
                "Run: uv run python scripts/ingest_product_kb.py"
            )
    except Exception as exc:
        print(f"[product-chat] Product RAG init failed ({exc}), running without RAG")
    _product_rag_ready = True


def _product_rag_context(query: str) -> str:
    """Retrieve product KB chunks relevant to *query*."""
    if _product_retriever is None:
        return ""
    try:
        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(
            _product_retriever.retrieve(hotel_id="product", query=query)
        )
        loop.close()
        if not results:
            return ""
        excerpts = "\n\n".join(f"[{r.category or r.doc_id}]\n{r.text}" for r in results)
        return "Relevant context from the Voxtera knowledge base:\n\n" + excerpts
    except Exception as exc:
        print(f"[product-rag] retrieval error: {exc}")
        return ""


# Product chat sessions — separate from hotel sessions
_product_sessions: dict[str, list[dict[str, str]]] = {}


_init_product_rag()

# ---------------------------------------------------------------------------
# Chat sessions — simple in-memory conversation history keyed by session id
# ---------------------------------------------------------------------------
_sessions: dict[str, list[dict[str, str]]] = {}
# Per-session turn counter for audit log (turn_number field).
_session_turn_counters: dict[str, int] = {}


def _handle_tool_call(tool_call, session_id: str) -> str:
    """Dispatch tool calls by function name."""
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    print(f"[actions] LLM called {name} with: {args}")

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"status": "error", "reason": f"Unknown tool: {name}"})

    return handler(args, session_id)


def _handle_create_ticket(args: dict, session_id: str) -> str:
    """Execute a create_ticket tool call via LoggingSink and return result JSON."""
    import asyncio

    try:
        category = Category(args["category"])
    except (ValueError, KeyError):
        return json.dumps(
            {"status": "rejected", "reason": f"Invalid category: {args.get('category')}"}
        )

    ticket = Ticket(
        category=category,
        summary=args.get("summary", ""),
        room_number=args.get("room_number", ""),
        original_quote=args.get("original_quote", ""),
        language_detected=args.get("language_detected", ""),
    )

    loop = asyncio.new_event_loop()
    ok = loop.run_until_complete(_logging_sink.send(ticket))
    loop.close()

    if ok:
        return json.dumps({"status": "filed", "category": category.value, "session_id": session_id})
    return json.dumps({"status": "failed"})


# Tool execution registry for the HTTP OpenAI function-calling path.
_TOOL_HANDLERS = {
    "create_ticket": _handle_create_ticket,
}


def _chat_completion(session_id: str, user_text: str, model: str, language: str) -> str:
    """Run one chat turn: RAG retrieval → OpenAI chat completion → reply text."""
    import os
    import time as _time
    from datetime import datetime

    import openai

    if session_id not in _sessions:
        _sessions[session_id] = [{"role": "system", "content": _ACTIONS_SYSTEM_PROMPT}]

    messages = _sessions[session_id]

    # Always inject current date/time so the bot can answer date/time questions.
    now_str = datetime.now(UTC).strftime("%A, %d %B %Y — %H:%M UTC")
    messages.append({"role": "system", "content": f"Current date and time: {now_str}."})

    # Inject RAG context before the user message.
    t0_rag = _time.monotonic()
    rag_ctx = _rag_context(user_text)
    print(f"[timing] rag={(_time.monotonic() - t0_rag) * 1000:.0f}ms  query={user_text[:60]!r}")
    if rag_ctx:
        messages.append({"role": "system", "content": rag_ctx})

    # Enforce reply language regardless of what the guest typed.
    if language and language not in ("auto", "en"):
        messages.append(
            {
                "role": "system",
                "content": (
                    f"IMPORTANT: You MUST reply in language code '{language}'. "
                    f"Do not switch to English even if the guest wrote in English. "
                    f"Reply only in '{language}'."
                ),
            }
        )

    messages.append({"role": "user", "content": user_text})

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=messages,
        tools=_TOOLS or None,
        tool_choice="auto" if _TOOLS else None,
    )

    msg = response.choices[0].message

    # Handle tool calls: execute the function, feed result back, get final reply.
    if msg.tool_calls:
        # Append the assistant message with tool_calls.
        messages.append(msg.model_dump())
        for tc in msg.tool_calls:
            result = _handle_tool_call(tc, session_id)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )
        # Second LLM call to get the final spoken reply.
        response2 = client.chat.completions.create(
            model=model,
            max_tokens=512,
            messages=messages,
            tools=_TOOLS or None,
            tool_choice="auto" if _TOOLS else None,
        )
        reply = response2.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": reply})
    else:
        reply = msg.content or ""
        messages.append({"role": "assistant", "content": reply})

    # Keep history bounded (system + last 40 turns).
    if len(messages) > 42:
        _sessions[session_id] = [messages[0]] + messages[-40:]

    return reply.strip()


def _tts_openai(text: str, voice: str) -> bytes:
    """Generate speech via OpenAI tts-1 and return raw MP3 bytes."""
    import os

    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = client.audio.speech.create(model="tts-1", voice=voice, input=text)
    return response.content


def _tts_cartesia(text: str, voice: str, language: str = "en") -> bytes:
    """Generate speech via Cartesia Sonic-3 HTTP API and return raw MP3 bytes.

    This is the synchronous /tts/bytes endpoint used by the demo's
    "Test Speaker" button and /api/chat fallback. The live voice bot
    uses Pipecat's WebSocket-streaming ``CartesiaTTSService`` for
    sub-100 ms TTFA; this HTTP path waits for the full audio buffer
    before returning, which is fine for a one-shot smoke test but would
    add ~1 s of latency in a real voice loop.
    """
    api_key = os.environ.get("CARTESIA_API_KEY")
    if not api_key:
        raise RuntimeError("CARTESIA_API_KEY is not set — cannot synthesize Cartesia speech")
    model = os.environ.get("CARTESIA_MODEL", "sonic-3")

    # Cartesia expects a 2-letter ISO 639-1 language code; strip any locale
    # suffix (e.g. "en-US" → "en") so callers can pass either form.
    lang_code = language.split("-")[0] if "-" in language else language

    payload = json.dumps(
        {
            "model_id": model,
            "voice": {"mode": "id", "id": voice},
            "language": lang_code,
            "transcript": text,
            "output_format": {
                "container": "mp3",
                "encoding": "mp3",
                "sample_rate": 22050,
            },
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.cartesia.ai/tts/bytes",
        data=payload,
        headers={
            "X-API-Key": api_key,
            "Cartesia-Version": "2025-04-16",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # Surface Cartesia's error body to the demo UI so voice-ID typos
        # and quota issues are debuggable from the browser console.
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cartesia TTS error {exc.code}: {body}") from exc


def _tts_elevenlabs(text: str, voice: str, language: str = "en") -> bytes:
    """Generate speech via the ElevenLabs HTTP TTS API and return raw MP3 bytes.

    This is the synchronous one-shot endpoint used by the demo's "Test
    Speaker" button and the /api/chat fallback. The live voice bot uses
    Pipecat's WebSocket-streaming ``ElevenLabsTTSService`` for ~75 ms TTFA;
    this HTTP path waits for the full audio buffer before returning, which
    is fine for a smoke test but would add latency in a real voice loop.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set — cannot synthesize ElevenLabs speech")
    model = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")

    # Fall back to the default voice (Rachel) if the caller sent an empty
    # voice — e.g. when the demo's voice dropdown hasn't populated yet
    # because the server is serving a stale in-memory language config.
    if not voice:
        voice = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

    # ElevenLabs expects a 2-letter ISO 639-1 code; strip any locale suffix
    # (e.g. "en-US" → "en"). Only the *_v2_5 models accept language_code —
    # passing it to older models (multilingual_v2, v3) returns a 400, so
    # gate the field on the model name.
    lang_code = language.split("-")[0] if "-" in language else language
    body_obj: dict = {"text": text, "model_id": model}
    if model.endswith("_v2_5"):
        body_obj["language_code"] = lang_code

    payload = json.dumps(body_obj).encode()
    # output_format=mp3_22050_32 works on the free tier and matches the
    # Cartesia test path's 22.05 kHz; higher MP3 bitrates need a paid plan.
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_22050_32",
        data=payload,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        # Surface ElevenLabs' error body to the demo UI so voice-ID typos,
        # unsupported-language errors, and quota issues are debuggable
        # from the browser console.
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs TTS error {exc.code}: {body}") from exc


def _tts_google(text: str, voice: str, language: str) -> bytes:
    """Generate speech via Google Chirp 3 HD and return raw MP3 bytes.

    Resolves the BCP-47 locale from the requested ISO 639-1 ``language``
    (e.g. ``ro`` → ``ro-RO``) and rewrites the voice ID's locale prefix
    so it matches — Google's API rejects requests where the voice locale
    differs from the requested locale. This mirrors what
    :class:`voxtera.controllers.AutoTTSLanguageSwitcher` does at runtime
    for the live bot.
    """
    import os

    from google.cloud import texttospeech

    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if creds and not os.path.isabs(creds):
        # Resolve relative paths from the project root (parent of demo-hotel/)
        creds = str(Path(__file__).resolve().parent.parent / creds)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds

    # Resolve BCP-47 locale via the shared language config. Falls back to
    # ``<code>-US`` for the rare case where the requested language isn't
    # registered — keeps Test Speaker functional even on unknown codes.
    if "-" in language:
        locale_code = language
    else:
        locale_code = google_locale_for(language.lower()) or f"{language}-US"

    # Rewrite the voice ID's locale prefix to match the requested locale.
    # Chirp 3 HD voice IDs follow the pattern "<locale>-Chirp3-HD-<character>";
    # we keep the character (Charon, Aoede, etc.) and swap the prefix.
    voice_id = voice
    if "Chirp3-HD-" in voice:
        character = voice.split("Chirp3-HD-")[-1]
        voice_id = f"{locale_code}-Chirp3-HD-{character}"

    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=locale_code,
        name=voice_id,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
    )
    response = client.synthesize_speech(
        input=synthesis_input, voice=voice_params, audio_config=audio_config
    )
    return response.audio_content


_SERVE_DIR = str(Path(__file__).resolve().parent)


class DemoHandler(http.server.SimpleHTTPRequestHandler):
    """Serves static files + the /api/tts-test endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=_SERVE_DIR, **kwargs)

    def handle_one_request(self):
        with contextlib.suppress(ConnectionResetError):
            super().handle_one_request()

    def log_message(self, format, *args):  # noqa: A002
        msg = format % args
        sys.stderr.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {msg}\n")

    def _client_ip(self) -> tuple[str, str | None]:
        """Return (direct_ip, x_forwarded_for) for audit logging.

        Uses X-Forwarded-For / X-Real-IP when running behind a reverse proxy
        or CDN so the logged IP is the real client address, not the proxy.
        """
        direct = self.client_address[0]
        fwd = (
            self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or ""
        ).strip() or None
        return direct, fwd

    def do_GET(self):  # noqa: N802
        # Admin endpoints first; everything else falls through to the static
        # file handler in SimpleHTTPRequestHandler.
        if self.path == "/api/languages":
            return self._handle_languages()
        if self.path == "/api/admin/health":
            return self._handle_admin_health()
        if self.path == "/api/admin/config":
            return self._handle_admin_config_get()
        if self.path == "/api/admin/sessions":
            return self._handle_admin_sessions()
        if self.path == "/api/trace/snapshot":
            return self._handle_trace_snapshot()
        if self.path == "/api/trace/stream":
            return self._handle_trace_stream()
        if self.path == "/api/trace/sessions":
            return self._handle_list_sessions()
        if self.path.startswith("/api/trace/sessions/") and self.path.endswith("/events"):
            sid = self.path[len("/api/trace/sessions/") : -len("/events")]
            return self._handle_session_events(sid)
        # For HTML files, always send no-store so browsers (especially Safari,
        # which aggressively disk-caches) never serve a stale version. JS/CSS
        # assets are unversioned so they get the same treatment.
        stripped = self.path.split("?")[0]
        # Root URL → redirect to voxtera.html (landing page).
        if stripped in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/voxtera.html")
            self.end_headers()
            return None
        # Security: only serve explicitly allowed file types. Everything else
        # (Python source, Markdown, JSON configs, .DS_Store, etc.) returns 404.
        allowed_extensions = (
            ".html",
            ".js",
            ".css",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".svg",
            ".ico",
            ".webp",
            ".woff",
            ".woff2",
        )
        if stripped.endswith((".html", ".js", ".css")):
            self._no_cache_get()
            return
        if stripped.endswith(allowed_extensions):
            return super().do_GET()
        self.send_error(404)
        return None

    def do_POST(self):  # noqa: N802
        if self.path == "/api/tts-test":
            return self._handle_tts_test()
        if self.path == "/api/chat":
            return self._handle_chat()
        if self.path == "/api/product-chat":
            return self._handle_product_chat()
        if self.path == "/api/admin/eject":
            return self._handle_admin_eject()
        if self.path == "/api/admin/end-session":
            return self._handle_admin_end_session()
        if self.path == "/api/admin/config":
            return self._handle_admin_config_post()
        if self.path == "/api/admin/tune":
            return self._handle_admin_tune()
        # Phase 3 — on-demand bot launcher
        if self.path == "/api/start-session":
            return self._handle_start_session()
        if self.path == "/api/end-session":
            return self._handle_end_session()
        if self.path == "/api/bot-event":
            return self._handle_bot_event()
        self.send_error(404)
        return None

    def do_DELETE(self):  # noqa: N802
        if self.path.startswith("/api/trace/sessions/"):
            sid = self.path[len("/api/trace/sessions/") :]
            return self._handle_delete_session(sid)
        self.send_error(404)
        return None

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        # X-Admin-Token must be in the allow-list or browsers will block
        # preflighted admin requests. Content-Type stays for /api/chat.
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")

    def list_directory(self, path):  # noqa: N802
        """Block directory listings — return 403 Forbidden."""
        self.send_error(403, "Directory listing is disabled")
        return None

    def _no_cache_get(self):
        """Serve an HTML/JS/CSS file with aggressive no-store headers.

        Safari (and other browsers) can disk-cache static files for hours even
        after a normal reload. Sending ``Cache-Control: no-store`` forces a
        fresh read from disk on every request, ensuring code changes in
        demo.html and other assets are always picked up immediately.
        """
        # Let SimpleHTTPRequestHandler do the actual file serving, then patch
        # the response headers before anything is sent. We do this by
        # temporarily monkey-patching send_response so we can inject our
        # Cache-Control header right after the status line.
        _orig_end_headers = self.end_headers

        def _patched_end_headers():
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            _orig_end_headers()

        self.end_headers = _patched_end_headers  # type: ignore[method-assign]
        try:
            super().do_GET()
        finally:
            self.end_headers = _orig_end_headers  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # JSON response helpers — used by every admin endpoint.
    # ------------------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Admin auth + helpers
    # ------------------------------------------------------------------

    def _admin_auth(self) -> tuple[bool, dict | None]:
        """Return (ok, error_response) for the current request.

        Centralised so every admin endpoint enforces the same gate. The
        precedence is intentional: if the *server* is misconfigured (no
        token, no Daily key) we report 503 — that's a deployment problem
        the operator needs to see, not "wrong password". Only when the
        server is healthy do we check the operator's token (401).
        """
        if not _ADMIN_TOKEN:
            self._send_json(
                503,
                {
                    "error": "admin_disabled",
                    "detail": "VOXTERA_ADMIN_TOKEN is not set on the server.",
                },
            )
            return False, {}
        if not _DAILY_API_KEY or (not _DAILY_ROOM_NAME and not _DAILY_DYNAMIC_ROOMS):
            self._send_json(
                503,
                {
                    "error": "daily_unconfigured",
                    "detail": (
                        "DAILY_API_KEY and DAILY_ROOM_NAME (or DAILY_DYNAMIC_ROOMS) must be set."
                    ),
                },
            )
            return False, {}
        provided = self.headers.get("X-Admin-Token", "")
        if provided != _ADMIN_TOKEN:
            self._send_json(401, {"error": "unauthorized"})
            _audit.write_failure(
                client_ip=self.client_address[0],
                forwarded_for=(
                    self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or ""
                ).strip()
                or None,
                user_agent=self.headers.get("User-Agent", ""),
                method=self.command,
                path=self.path,
                status_code=401,
                error="invalid_admin_token",
                request_id=str(uuid.uuid4()),
            )
            return False, {}
        return True, None

    def _is_bot(self, user_name: str) -> bool:
        # Same comparison ``pipeline.py`` uses for the participant_left hook
        # (around line 377). When BOT_NAME changes, both sites must update.
        return user_name == _BOT_NAME

    # ------------------------------------------------------------------
    # /api/admin/health — does NOT require the token. Lets the page
    # decide whether to even render the token gate.
    # ------------------------------------------------------------------

    def _handle_admin_health(self) -> None:
        self._send_json(
            200,
            {
                "ok": True,
                "admin_enabled": bool(_ADMIN_TOKEN),
                "daily_configured": bool(
                    _DAILY_API_KEY and (_DAILY_ROOM_NAME or _DAILY_DYNAMIC_ROOMS)
                ),
                "daily_room": _DAILY_ROOM_NAME or "(dynamic)",
                "daily_domain": _DAILY_DOMAIN or "",
                "bot_name": _BOT_NAME,
                "dynamic_rooms": _DAILY_DYNAMIC_ROOMS,
                "max_participants": _DAILY_ROOM_MAX_PARTICIPANTS,
                "active_sessions": len(REGISTRY.active_sessions()),
            },
        )

    # ------------------------------------------------------------------
    # /api/admin/config — hot-reconfigurable runtime settings
    # ------------------------------------------------------------------

    def _handle_admin_config_get(self) -> None:
        """GET /api/admin/config — return current runtime config values."""
        global \
            _DAILY_DYNAMIC_ROOMS, \
            _DAILY_ROOM_MAX_PARTICIPANTS, \
            _MAX_CONCURRENT_SESSIONS, \
            _SESSION_TIMEOUT_SECS
        ok, _ = self._admin_auth()
        if not ok:
            return
        self._send_json(
            200,
            {
                "dynamic_rooms": _DAILY_DYNAMIC_ROOMS,
                "max_participants": _DAILY_ROOM_MAX_PARTICIPANTS,
                "max_concurrent_sessions": _MAX_CONCURRENT_SESSIONS,
                "session_timeout_secs": _SESSION_TIMEOUT_SECS,
            },
        )

    def _handle_admin_config_post(self) -> None:
        """POST /api/admin/config — update runtime config values.

        Body (all fields optional):
          {"dynamic_rooms": true, "max_participants": 3, "max_concurrent_sessions": 20}
        Only provided fields are updated; omitted fields keep their current value.
        """
        global \
            _DAILY_DYNAMIC_ROOMS, \
            _DAILY_ROOM_MAX_PARTICIPANTS, \
            _MAX_CONCURRENT_SESSIONS, \
            _SESSION_TIMEOUT_SECS
        ok, _ = self._admin_auth()
        if not ok:
            return
        body = self._read_json_body()

        changed = {}
        if "dynamic_rooms" in body:
            val = body["dynamic_rooms"]
            if isinstance(val, bool):
                _DAILY_DYNAMIC_ROOMS = val
                changed["dynamic_rooms"] = val
            else:
                self._send_json(400, {"error": "dynamic_rooms must be a boolean"})
                return

        if "max_participants" in body:
            val = body["max_participants"]
            if isinstance(val, int) and 1 <= val <= 100:
                _DAILY_ROOM_MAX_PARTICIPANTS = val
                changed["max_participants"] = val
            else:
                self._send_json(400, {"error": "max_participants must be an int 1-100"})
                return

        if "max_concurrent_sessions" in body:
            val = body["max_concurrent_sessions"]
            if isinstance(val, int) and 1 <= val <= 500:
                _MAX_CONCURRENT_SESSIONS = val
                changed["max_concurrent_sessions"] = val
            else:
                self._send_json(400, {"error": "max_concurrent_sessions must be an int 1-500"})
                return

        if "session_timeout_secs" in body:
            val = body["session_timeout_secs"]
            if isinstance(val, int) and 30 <= val <= 3600:
                _SESSION_TIMEOUT_SECS = val
                changed["session_timeout_secs"] = val
            else:
                self._send_json(400, {"error": "session_timeout_secs must be an int 30-3600"})
                return

        print(f"[admin/config] updated: {changed}")
        self._send_json(
            200,
            {
                "applied": changed,
                "current": {
                    "dynamic_rooms": _DAILY_DYNAMIC_ROOMS,
                    "max_participants": _DAILY_ROOM_MAX_PARTICIPANTS,
                    "max_concurrent_sessions": _MAX_CONCURRENT_SESSIONS,
                    "session_timeout_secs": _SESSION_TIMEOUT_SECS,
                },
            },
        )

    # ------------------------------------------------------------------
    # /api/admin/sessions — live snapshot from Daily REST
    # ------------------------------------------------------------------

    def _fetch_participants_for_room(self, room_name: str) -> list:
        """Return participants for the given room."""
        if not _DAILY_API_KEY or not room_name:
            return []
        return list_room_participants(api_key=_DAILY_API_KEY, room_name=room_name)

    def _fetch_participants_cached(self) -> list:
        """Return participants for the configured room, with a tiny TTL cache.

        Two browsers polling at 1 s would otherwise double Daily REST load
        for no UX benefit. The cache is intentionally short (500 ms) so the
        view feels live.
        """
        now = time.monotonic()
        last = float(_presence_cache.get("fetched_at", 0.0) or 0.0)
        cached = _presence_cache.get("value")
        if cached is not None and (now - last) < _PRESENCE_CACHE_TTL_SECS:
            return list(cached)  # type: ignore[arg-type]

        if not _DAILY_API_KEY:
            return []
        room = _DAILY_ROOM_NAME or ""
        if not room:
            return []
        participants = list_room_participants(
            api_key=_DAILY_API_KEY,
            room_name=room,
        )
        _presence_cache["fetched_at"] = now
        _presence_cache["value"] = participants
        return participants

    def _handle_admin_sessions(self) -> None:
        ok, _ = self._admin_auth()
        if not ok:
            return

        if _DAILY_DYNAMIC_ROOMS:
            # Dynamic mode: return per-session room participant snapshots.
            sessions_data = []
            for sid in REGISTRY.active_sessions():
                rn = REGISTRY.get_room_name(sid) or ""
                if not rn:
                    continue
                try:
                    parts = self._fetch_participants_for_room(rn)
                    sessions_data.append(
                        {
                            "session_id": sid,
                            "room_name": rn,
                            "participant_count": len(parts),
                            "participants": [
                                {
                                    "id": p.id,
                                    "user_name": p.user_name,
                                    "joined_at": p.joined_at,
                                    "duration_secs": p.duration_secs,
                                    "is_bot": self._is_bot(p.user_name),
                                }
                                for p in parts
                            ],
                        }
                    )
                except DailyAPIError as exc:
                    sessions_data.append(
                        {
                            "session_id": sid,
                            "room_name": rn,
                            "error": str(exc),
                        }
                    )
            self._send_json(
                200,
                {
                    "dynamic_rooms": True,
                    "domain": _DAILY_DOMAIN or "",
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "sessions": sessions_data,
                    "session_count": len(sessions_data),
                },
            )
        else:
            # Legacy single-room mode.
            try:
                participants = self._fetch_participants_cached()
            except DailyAPIError as exc:
                self._send_json(
                    502,
                    {
                        "error": "daily_api_error",
                        "detail": str(exc),
                        "status": exc.status,
                    },
                )
                return

            rendered = [
                {
                    "id": p.id,
                    "user_name": p.user_name,
                    "joined_at": p.joined_at,
                    "duration_secs": p.duration_secs,
                    "is_bot": self._is_bot(p.user_name),
                }
                for p in participants
            ]
            non_bot = [p for p in rendered if not p["is_bot"]]
            self._send_json(
                200,
                {
                    "room": _DAILY_ROOM_NAME or "",
                    "domain": _DAILY_DOMAIN or "",
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "participants": rendered,
                    "session_count": 1 if non_bot else 0,
                },
            )

    # ------------------------------------------------------------------
    # /api/admin/eject — kick one or more participants
    # ------------------------------------------------------------------

    def _do_eject(self, ids: list[str], room_name: str | None = None) -> None:
        eject_room = room_name or _DAILY_ROOM_NAME
        if not _DAILY_API_KEY or not eject_room:
            self._send_json(503, {"error": "daily_not_configured"})
            return
        try:
            ejected = eject_participants(
                api_key=_DAILY_API_KEY,
                room_name=eject_room,
                participant_ids=ids,
            )
        except DailyAPIError as exc:
            self._send_json(
                502,
                {
                    "error": "daily_api_error",
                    "detail": str(exc),
                    "status": exc.status,
                },
            )
            return

        # Invalidate the presence cache so the next /sessions poll reflects
        # the eject immediately.
        _presence_cache["fetched_at"] = 0.0
        _presence_cache["value"] = None

        operator_ip = self.address_string()
        for pid in ejected:
            sys.stderr.write(f"[admin] eject room={eject_room} ip={operator_ip} id={pid}\n")

        self._send_json(
            200,
            {
                "ejected_ids": ejected,
                "requested_ids": ids,
            },
        )

    def _handle_admin_eject(self) -> None:
        ok, _ = self._admin_auth()
        if not ok:
            return
        body = self._read_json_body()
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(x, str) and x for x in ids):
            self._send_json(
                400,
                {"error": "invalid_body", "detail": "ids must be a non-empty list of strings"},
            )
            return
        # In dynamic mode, body can optionally specify the room to eject from.
        room_name = body.get("room_name") or None
        self._do_eject(ids, room_name=room_name)

    # ------------------------------------------------------------------
    # /api/admin/end-session — eject everyone
    # ------------------------------------------------------------------

    def _handle_admin_end_session(self) -> None:
        ok, _ = self._admin_auth()
        if not ok:
            return

        if _DAILY_DYNAMIC_ROOMS:
            # Dynamic mode: end all active sessions.
            ejected_all: list[str] = []
            for sid in REGISTRY.active_sessions():
                rn = REGISTRY.get_room_name(sid) or ""
                if not rn or not _DAILY_API_KEY:
                    continue
                try:
                    parts = self._fetch_participants_for_room(rn)
                    ids = [p.id for p in parts if p.id]
                    if ids:
                        ejected = eject_participants(
                            api_key=_DAILY_API_KEY,
                            room_name=rn,
                            participant_ids=ids,
                        )
                        ejected_all.extend(ejected)
                except DailyAPIError:
                    pass
                REGISTRY.reap(sid)
            self._send_json(200, {"ejected_ids": ejected_all, "requested_ids": ejected_all})
        else:
            # Legacy single-room mode.
            _presence_cache["fetched_at"] = 0.0
            try:
                participants = self._fetch_participants_cached()
            except DailyAPIError as exc:
                self._send_json(
                    502,
                    {
                        "error": "daily_api_error",
                        "detail": str(exc),
                        "status": exc.status,
                    },
                )
                return
            ids = [p.id for p in participants if p.id]
            if not ids:
                self._send_json(200, {"ejected_ids": [], "requested_ids": []})
                return
            self._do_eject(ids)

    # ------------------------------------------------------------------
    # Phase 3 — On-demand bot launcher endpoints
    # ------------------------------------------------------------------
    def _handle_start_session(self) -> None:
        """POST /api/start-session — spawn a bot and wait for its ready event.

        Flow:
          1. Ask Daily who is in the room right now (source of truth).
             If anyone is there → 409 Busy.
          2. Reserve a session slot, spawn ``python -m voxtera.bot``,
             attach a reaper thread.
          3. Block on the session's queue (``q.get(timeout=15)``) until the
             bot POSTs ``{type:"ready"}`` to ``/api/bot-event`` from inside
             its ``on_joined`` Daily handler.
          4. Return ``{room_url, session_id}`` so the browser can join.

        The busy check used to read ``REGISTRY.is_busy()``, an in-memory slot
        freed only when the bot subprocess exits. In dynamic-rooms mode, the
        shared-room presence check is replaced with a session-count gate.
        Legacy mode still checks Daily presence for the shared room.
        """
        # ------------------------------------------------------------------
        # Busy gate
        # ------------------------------------------------------------------
        if _DAILY_DYNAMIC_ROOMS:
            # Dynamic mode: gate on session count only.
            if REGISTRY.is_busy():
                self._send_json(
                    409,
                    {
                        "error": "max_sessions_reached",
                        "active_sessions": len(REGISTRY.active_sessions()),
                    },
                )
                return
        else:
            # Legacy single-room mode: Daily presence is the source of truth.
            live: list | None
            try:
                if _DAILY_API_KEY and _DAILY_ROOM_NAME:
                    live = list_room_participants(
                        api_key=_DAILY_API_KEY,
                        room_name=_DAILY_ROOM_NAME,
                    )
                else:
                    print("[launcher] DAILY_API_KEY/ROOM not set — skipping presence check")
                    live = None
            except DailyAPIError as exc:
                print(f"[launcher] daily presence check failed: {exc}")
                live = None

            if live:
                humans = [p for p in live if p.user_name != _BOT_NAME]
                orphan_bots = [p for p in live if p.user_name == _BOT_NAME]

                if humans:
                    label = ",".join(p.user_name or p.id for p in humans) or "unknown"
                    print(
                        f"[launcher] /api/start-session rejected — Daily room has "
                        f"{len(humans)} human participant(s): {label}"
                    )
                    self._send_json(
                        409,
                        {
                            "error": "busy",
                            "active_session": label,
                            "participant_count": len(humans),
                            "source": "daily",
                        },
                    )
                    return

                if orphan_bots:
                    ids = [p.id for p in orphan_bots if p.id]
                    print(
                        f"[launcher] ejecting {len(ids)} orphan bot(s) before spawn: "
                        f"{','.join(ids) or '(no ids)'}"
                    )
                    try:
                        eject_participants(
                            api_key=_DAILY_API_KEY,  # type: ignore[arg-type]
                            room_name=_DAILY_ROOM_NAME,  # type: ignore[arg-type]
                            participant_ids=ids,
                        )
                    except DailyAPIError as exc:
                        print(f"[launcher] orphan-bot eject failed: {exc}")
                        self._send_json(
                            502,
                            {
                                "error": "orphan_eject_failed",
                                "detail": str(exc),
                                "active_session": "Voxtera",
                                "source": "daily",
                            },
                        )
                        return

            # Daily says nobody real is in the room but registry thinks busy → stale.
            if (
                live == [] or (live and not [p for p in live if p.user_name != _BOT_NAME])
            ) and REGISTRY.is_busy():
                stale = REGISTRY.active_session()
                print(
                    f"[launcher] daily presence empty but slot {stale} still held — "
                    f"reaping stale slot"
                )
                if stale:
                    REGISTRY.reap(stale)

            if live is None and REGISTRY.is_busy():
                active = REGISTRY.active_session()
                print(
                    f"[launcher] daily unreachable — falling back to local registry, "
                    f"session {active} active"
                )
                self._send_json(
                    409,
                    {"error": "busy", "active_session": active, "source": "local"},
                )
                return

        session_id = uuid.uuid4().hex
        callback_url = f"{LAUNCHER_BASE_URL}/api/bot-event"

        # Reserve the slot. Race window between is_busy() above and start()
        # here is closed by start() raising BotSessionBusyError if someone got in.
        try:
            q = REGISTRY.start(session_id)
        except BotSessionBusyError as exc:
            self._send_json(409, {"error": "busy", "active_session": exc.active_session})
            return

        print(
            f"[launcher] spawning bot for session {session_id} "
            f"(callback={callback_url}, timeout={_SPAWN_TIMEOUT_SECS}s)"
        )

        # Read body to extract per-session params (llm model, etc.).
        body: dict = {}
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
        llm_model = body.get("llm") or None

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
                print(
                    f"[launcher] created Daily room {room_name} "
                    f"(max_participants={_DAILY_ROOM_MAX_PARTICIPANTS})"
                )
            except DailyAPIError as exc:
                print(f"[launcher] failed to create Daily room: {exc}")
                REGISTRY.reap(session_id)
                self._send_json(502, {"error": f"Daily room creation failed: {exc}"})
                return
        else:
            room_name = _DAILY_ROOM_NAME or ""

        REGISTRY.attach_room_name(session_id, room_name)

        # Pick a tune port for this bot. Allocate a unique port per session
        # so multiple concurrent bots don't collide.
        with REGISTRY._lock:
            used_ports = {
                s.get("tune_port") for s in REGISTRY._sessions.values() if s.get("tune_port")
            }
            tune_port = next(
                p for p in range(_DEFAULT_BOT_PORT, _DEFAULT_BOT_PORT + 200) if p not in used_ports
            )
            sess = REGISTRY._sessions.get(session_id)
            if sess is not None:
                sess["tune_port"] = tune_port
        try:
            proc = _spawn_bot(
                session_id,
                callback_url,
                tune_port,
                llm_model=llm_model,
                room_name=room_name,
            )
        except Exception as exc:
            print(f"[launcher] spawn failed: {exc}")
            REGISTRY.reap(session_id)
            self._send_json(500, {"error": f"spawn failed: {exc}"})
            return

        REGISTRY.attach_process(session_id, proc)
        _start_reaper_thread(session_id, proc)

        # ── Hard session timeout (3 minutes) ──────────────────────────────
        # If the browser never calls /api/end-session (tab closed, network
        # drop, user walked away) the bot would run and bill indefinitely.
        # Two-stage watchdog:
        #   T+165s (2:45) — bot speaks a warning via /speak on TuneServer
        #   T+180s (3:00) — force-kill the bot and eject from Daily
        _MAX_SESSION_SECS = _SESSION_TIMEOUT_SECS  # noqa: N806
        _WARN_SESSION_SECS = max(  # noqa: N806
            _MAX_SESSION_SECS - 15, 5
        )  # voice warning 15 s before kill
        _WARN_TEXT = (  # noqa: N806
            "I'm sorry, your session will end in 15 seconds due to the time limit. "
            "Please call again if you need further assistance. Goodbye!"
        )

        def _session_warn(sid: str) -> None:
            """Speak a warning via the bot's TuneServer /speak endpoint."""
            import contextlib as _ctx

            _tp = _get_bot_tune_port(sid)
            if _tp is None:
                return
            try:
                import urllib.request as _ur

                payload = json.dumps({"text": _WARN_TEXT}).encode()
                req = _ur.Request(
                    f"http://127.0.0.1:{_tp}/speak",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _ctx.suppress(Exception), _ur.urlopen(req, timeout=3):
                    pass
                print(f"[watchdog] sent session-timeout warning to bot (session={sid[:8]})")
            except Exception as exc:
                print(f"[watchdog] warn failed: {exc}")

        def _session_kill(sid: str) -> None:
            import contextlib as _ctx

            print(f"[watchdog] session {sid[:8]} hit {_MAX_SESSION_SECS}s limit — force-ending")
            with REGISTRY._lock:
                _proc = REGISTRY._sessions.get(sid, {}).get("process")
            if _proc is not None and _proc.poll() is None:
                with _ctx.suppress(Exception):
                    _proc.terminate()
            # Use room_name from the closure (captured at session creation time)
            if _DAILY_API_KEY and room_name:
                with _ctx.suppress(Exception):
                    participants = list_room_participants(
                        api_key=_DAILY_API_KEY, room_name=room_name
                    )
                    ids = [p.id for p in participants if p.id]
                    if ids:
                        eject_participants(
                            api_key=_DAILY_API_KEY,
                            room_name=room_name,
                            participant_ids=ids,
                        )
            REGISTRY.reap(sid)

        _wd_warn = threading.Timer(_WARN_SESSION_SECS, _session_warn, args=[session_id])
        _wd_warn.daemon = True
        _wd_warn.start()

        _wd = threading.Timer(_MAX_SESSION_SECS, _session_kill, args=[session_id])
        _wd.daemon = True
        _wd.start()
        # Store both timers so reap() can cancel them on a clean hang-up.
        REGISTRY.attach_watchdog(session_id, _wd)
        REGISTRY.attach_watchdog_warn(session_id, _wd_warn)

        # Block until either the bot posts {type:"ready"} or we hit the
        # spawn timeout. Reaper events ({"type":"_reaped"}) also unblock us
        # — that path means the bot exited before posting ready (crash).
        try:
            event = q.get(timeout=_SPAWN_TIMEOUT_SECS)
        except _queue.Empty:
            print(f"[launcher] session {session_id} timed out — killing bot")
            with contextlib.suppress(Exception):
                proc.kill()
            # Reaper will fire reap() once kill() is observed.
            self._send_json(504, {"error": "bot startup timeout", "session_id": session_id})
            return

        event_type = event.get("type")

        if event_type == "_reaped":
            rc = proc.returncode
            print(f"[launcher] session {session_id}: bot exited before ready (rc={rc})")
            self._send_json(
                500,
                {"error": f"bot exited before ready (rc={rc})"},
            )
            return

        if event_type != "ready":
            print(f"[launcher] session {session_id}: unexpected first event {event_type!r}")
            with contextlib.suppress(Exception):
                proc.kill()
            self._send_json(500, {"error": f"unexpected first event: {event_type}"})
            return

        # Bot is in the room. Build the room URL and hand back to the browser.
        if not _DAILY_DOMAIN or not room_name:
            print("[launcher] DAILY_DOMAIN / room_name missing — killing bot")
            with contextlib.suppress(Exception):
                proc.kill()
            self._send_json(
                500,
                {"error": "DAILY_DOMAIN or room_name not available on launcher"},
            )
            return

        room_url = f"https://{_DAILY_DOMAIN}/{room_name}"
        print(f"[launcher] session {session_id} ready — returning room_url to browser")
        self._send_json(
            200,
            {"session_id": session_id, "room_url": room_url, "bot_name": _BOT_NAME},
        )

    def _handle_end_session(self) -> None:
        """POST /api/end-session — browser asks the server to kill the bot and
        eject everyone from the Daily room.

        Called by the orb page when the user presses the phone hang-up button.
        Does NOT require an admin token — the session_id in the body is used
        as a lightweight proof of ownership (the browser received it from
        /api/start-session moments earlier).

        Steps:
          1. SIGTERM the bot subprocess (it will leave Daily gracefully).
          2. If DAILY_API_KEY is set, eject all participants via Daily REST
             so the room is empty even if the bot crashed before self-leaving.
          3. Free the REGISTRY slot so the next Start click works immediately.
        """
        body = self._read_json_body()
        session_id = (body.get("session_id") or "").strip()

        # Kill the bot subprocess for this session.
        proc = None
        session_room_name: str | None = None
        with REGISTRY._lock:
            if session_id and session_id in REGISTRY._sessions:
                proc = REGISTRY._sessions[session_id].get("process")
                session_room_name = REGISTRY._sessions[session_id].get("room_name")
            elif not session_id and REGISTRY._sessions:
                # Backward compat: pick the first active session.
                session_id = next(iter(REGISTRY._sessions))
                proc = REGISTRY._sessions[session_id].get("process")
                session_room_name = REGISTRY._sessions[session_id].get("room_name")

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                print(f"[end-session] sent SIGTERM to bot (session={session_id[:8]})")
            except Exception as e:
                print(f"[end-session] terminate error: {e}")

        # Eject all participants via Daily REST (belt-and-suspenders).
        eject_room = session_room_name or _DAILY_ROOM_NAME
        if _DAILY_API_KEY and eject_room:
            try:
                participants = list_room_participants(api_key=_DAILY_API_KEY, room_name=eject_room)
                ids = [p.id for p in participants if p.id]
                if ids:
                    eject_participants(
                        api_key=_DAILY_API_KEY,
                        room_name=eject_room,
                        participant_ids=ids,
                    )
                    print(f"[end-session] ejected {len(ids)} participant(s) from room {eject_room}")
            except Exception as e:
                print(f"[end-session] Daily eject error: {e}")

        # Delete the dynamic room if applicable.
        if _DAILY_DYNAMIC_ROOMS and _DAILY_API_KEY and session_room_name:
            try:
                delete_room(api_key=_DAILY_API_KEY, room_name=session_room_name)
                print(f"[end-session] deleted Daily room {session_room_name}")
            except DailyAPIError as exc:
                print(f"[end-session] Daily room delete error: {exc}")

        # Free the launcher slot so the next Start click isn't rejected as busy.
        if session_id:
            REGISTRY.reap(session_id)

        self._send_json(200, {"ok": True, "session_id": session_id})

    def _handle_bot_event(self) -> None:
        """POST /api/bot-event — receive an event from a bot subprocess.

        Body: ``{"session_id": "...", "type": "ready"|"error"|"exiting"|"trace", ...}``.
        Trace events are routed into the trace buffer for SSE fan-out;
        all other types go to the launcher's per-session queue as before.
        """
        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json(400, {"error": f"bad json: {exc}"})
            return

        session_id = body.get("session_id") or ""
        event_type = body.get("type") or ""
        if not session_id or not event_type:
            self._send_json(400, {"error": "session_id and type are required"})
            return

        if event_type == "trace":
            # Batched trace events from the bot's TraceForwarder. Each event
            # in the batch is a dict matching voxtera.trace.TraceEvent.
            events = body.get("events") or []
            if isinstance(events, list):
                # Stamp session_id onto each event for the dashboard's view.
                for ev in events:
                    if isinstance(ev, dict) and not ev.get("session_id"):
                        ev["session_id"] = session_id
                clean = [ev for ev in events if isinstance(ev, dict)]
                _TRACE_BUFFER.add_many(clean)
                # Durable copy on disk so this session can be replayed later
                # from /trace.html. The in-memory ring buffer is still the
                # source of truth for live SSE; this is purely for history.
                _SESSION_STORE.append(session_id, clean)
        else:
            REGISTRY.deliver(session_id, body)
        # 204 No Content — nothing to return; the bot doesn't care.
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ------------------------------------------------------------------
    # Trace endpoints — power /trace.html
    # ------------------------------------------------------------------

    def _trace_auth(self) -> bool:
        """Same gate as admin endpoints, with the same 401 / 503 semantics.

        Returns True iff auth passed. On failure, an error response has
        already been written.
        """
        if not _ADMIN_TOKEN:
            self._send_json(
                503,
                {
                    "error": "admin_disabled",
                    "detail": "VOXTERA_ADMIN_TOKEN is not set on the server.",
                },
            )
            return False
        provided = self.headers.get("X-Admin-Token", "")
        if provided != _ADMIN_TOKEN:
            self._send_json(401, {"error": "unauthorized"})
            return False
        return True

    def _bot_get_knobs(self) -> tuple[int, dict | None]:
        """Fetch /knobs from the bot's tune-server. Returns (status, body)."""
        port = _get_bot_tune_port()
        if port is None:
            return (502, {"error": "bot_unreachable", "detail": "no live bot session"})
        url = f"http://127.0.0.1:{port}/knobs"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read())
                return (resp.status, data)
        except urllib.error.HTTPError as exc:
            return (exc.code, {"error": "bot_http_error", "detail": str(exc)})
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return (502, {"error": "bot_unreachable", "detail": str(exc)})

    def _handle_trace_snapshot(self) -> None:
        """GET /api/trace/snapshot — current state in one JSON blob.

        Returns: bot connection status, knobs (from bot if available, else
        an empty list with a warning), recent events tail, and trace buffer
        stats. Used by the dashboard at page load.
        """
        if not self._trace_auth():
            return
        port = _get_bot_tune_port()
        bot_connected = port is not None
        knobs: list = []
        if bot_connected:
            status, body = self._bot_get_knobs()
            if status == 200 and isinstance(body, dict):
                knobs = body.get("knobs", []) or []
        snapshot = {
            "bot_connected": bot_connected,
            "tune_port": port,
            "knobs": knobs,
            "recent_events": _TRACE_BUFFER.recent(limit=200),
            "buffer_stats": _TRACE_BUFFER.stats(),
        }
        self._send_json(200, snapshot)

    def _handle_admin_tune(self) -> None:
        """POST /api/admin/tune — forward to the bot's TuneServer.

        Body: ``{"knob": "vad_stop_secs", "value": 0.3}``.
        Returns the bot's response shape (applied / error).
        """
        if not self._trace_auth():
            return
        body = self._read_json_body()
        knob = body.get("knob")
        if not isinstance(knob, str) or not knob:
            self._send_json(400, {"applied": False, "error": "missing_knob"})
            return
        port = _get_bot_tune_port()
        if port is None:
            self._send_json(
                502,
                {"applied": False, "error": "bot_unreachable"},
            )
            return
        url = f"http://127.0.0.1:{port}/tune"
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                resp_body = json.loads(resp.read() or b"{}")
                self._send_json(resp.status, resp_body)
        except urllib.error.HTTPError as exc:
            try:
                resp_body = json.loads(exc.read() or b"{}")
            except (json.JSONDecodeError, OSError):
                resp_body = {"applied": False, "error": "bot_http_error"}
            self._send_json(exc.code, resp_body)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            self._send_json(
                502,
                {"applied": False, "error": "bot_unreachable", "detail": str(exc)},
            )

    def _handle_trace_stream(self) -> None:
        """GET /api/trace/stream — Server-Sent Events.

        Sends the recent ring buffer as catch-up, then streams live events.
        The handler thread blocks on a per-subscriber queue; the client
        keeps the connection open until they navigate away.
        """
        if not self._trace_auth():
            return
        # SSE headers. ``Cache-Control: no-cache`` and the
        # ``Content-Type: text/event-stream`` are required by browsers.
        try:
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            # Disable proxy buffering so events flush immediately.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        sub = _TRACE_BUFFER.subscribe()
        # Send catch-up tail so the dashboard renders something immediately.
        try:
            for ev in _TRACE_BUFFER.recent(limit=200):
                self._sse_write(ev)
            # Live tail. The 15s heartbeat keeps proxies / load balancers
            # from killing the connection during quiet periods.
            last_heartbeat = time.monotonic()
            while True:
                try:
                    ev = sub.get(timeout=5.0)
                    self._sse_write(ev)
                except _queue.Empty:
                    pass
                now = time.monotonic()
                if (now - last_heartbeat) >= 15.0:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    last_heartbeat = now
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _TRACE_BUFFER.unsubscribe(sub)

    def _sse_write(self, event: dict) -> None:
        """Write a single SSE ``data:`` frame. Caller handles disconnects."""
        line = f"data: {json.dumps(event)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------------
    # Session history endpoints — power the session picker in trace.html
    # ------------------------------------------------------------------

    def _handle_list_sessions(self) -> None:
        """GET /api/trace/sessions — JSON list of stored sessions.

        Includes both finalized (meta-file-present) and in-progress sessions,
        sorted newest first. Each entry has ``session_id``, ``started_at``,
        ``ended_at``, ``turn_count``, ``providers``, and short transcript
        peeks. Used by the dashboard's session picker.
        """
        if not self._trace_auth():
            return
        try:
            sessions = _SESSION_STORE.list_sessions()
        except Exception as exc:
            self._send_json(500, {"error": "list_failed", "detail": str(exc)})
            return
        # Mark the currently-active session so the dashboard can highlight it.
        active = REGISTRY.active_session()
        for s in sessions:
            s["active"] = s.get("session_id") == active
        self._send_json(200, {"sessions": sessions})

    def _handle_session_events(self, session_id: str) -> None:
        """GET /api/trace/sessions/{id}/events — full event list for replay.

        Returns ``{events: [...]}`` (a JSON array, not NDJSON, so the browser
        can parse it in one shot). For long sessions this can be a few MB —
        acceptable for a debug tool; if it grows past that, paginate.
        """
        if not self._trace_auth():
            return
        if not session_id or "/" in session_id or ".." in session_id:
            self._send_json(400, {"error": "bad_session_id"})
            return
        events = _SESSION_STORE.read_events(session_id)
        if events is None:
            self._send_json(404, {"error": "session_not_found"})
            return
        self._send_json(200, {"session_id": session_id, "events": events})

    def _handle_delete_session(self, session_id: str) -> None:
        """DELETE /api/trace/sessions/{id} — remove NDJSON + meta for a session.

        Refuses to delete the currently-active session (409) — the operator
        must end the session first. This is the safety guard the operator
        controls; no automatic retention or rotation runs.
        """
        if not self._trace_auth():
            return
        if not session_id or "/" in session_id or ".." in session_id:
            self._send_json(400, {"error": "bad_session_id"})
            return
        if session_id == REGISTRY.active_session():
            self._send_json(
                409,
                {
                    "error": "session_active",
                    "detail": "End the session before deleting it.",
                },
            )
            return
        removed = _SESSION_STORE.delete(session_id)
        if not removed:
            self._send_json(404, {"error": "session_not_found"})
            return
        self._send_json(200, {"session_id": session_id, "deleted": True})

    def _handle_languages(self):
        """GET /api/languages — full language config for the demo UI.

        Returns the contents of ``config/languages.json`` as JSON so the
        frontend can build the language + voice dropdowns dynamically and
        validate language↔TTS compatibility before enabling the Start
        button.
        """
        body = json.dumps(LANG_CONFIG).encode("utf-8")
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Demo languages don't change between requests within a run;
        # cache aggressively to avoid the disk read on every page load.
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def _handle_tts_test(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            provider = body.get("provider", "openai")
            voice = body.get("voice", "nova")
            lang = body.get("language", "en")
            model = body.get("model", "gpt-4o-mini")
            if lang == "multi":
                lang = "en"

            # Use pre-built greeting if available, otherwise translate via LLM.
            text = GREETINGS.get(lang)
            if not text:
                base = GREETINGS["en"]
                text = _translate_greeting(base, lang, model)

            if provider == "google":
                audio = _tts_google(text, voice, lang)
            elif provider == "cartesia":
                audio = _tts_cartesia(text, voice, lang)
            elif provider == "elevenlabs":
                audio = _tts_elevenlabs(text, voice, lang)
            else:
                audio = _tts_openai(text, voice)

            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except Exception as exc:
            error_msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_msg)))
            self.end_headers()
            self.wfile.write(error_msg)

    def _handle_chat(self):
        """POST /api/chat — streaming NDJSON: text chunks + sentence-level TTS.

        Each line of the response is a JSON object:
          {"type": "text",  "chunk": "<token>"}          — LLM token
          {"type": "audio", "data": "<base64 mp3>"}   — first-sentence TTS
          {"type": "done",  "session_id": "...", "text": "<full reply>"}
          {"type": "error", "error": "..."}
        """
        import os

        import openai as _oai  # noqa — kept for local references

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        text = (body.get("text") or "").strip()
        session_id = body.get("session_id") or str(uuid.uuid4())
        model = body.get("model") or "gpt-4o-mini"
        language = body.get("language") or "en"
        tts_provider = body.get("tts_provider") or "openai"
        voice = body.get("voice") or "nova"

        request_id = str(uuid.uuid4())
        client_ip, forwarded_for = self._client_ip()
        user_agent = self.headers.get("User-Agent", "")
        rag_elapsed_ms: float | None = None
        llm_elapsed_ms: float | None = None

        # ── streaming response headers ──
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def push(obj: dict) -> None:
            line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
            self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
            self.wfile.flush()

        def finish() -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        if not text:
            push({"type": "error", "error": "text is required"})
            finish()
            _audit.write_failure(
                client_ip=client_ip,
                forwarded_for=forwarded_for,
                user_agent=user_agent,
                method="POST",
                path="/api/chat",
                status_code=400,
                error="empty_text",
                request_id=request_id,
                session_id=session_id,
            )
            return

        # ── build messages ──
        import time as _time
        from datetime import datetime

        if session_id not in _sessions:
            _sessions[session_id] = [{"role": "system", "content": _ACTIONS_SYSTEM_PROMPT}]
        messages = _sessions[session_id]

        # Always inject current date/time so the bot can answer "what day is today" etc.
        now_str = datetime.now(UTC).strftime("%A, %d %B %Y — %H:%M UTC")
        messages.append({"role": "system", "content": f"Current date and time: {now_str}."})

        t0_rag = _time.monotonic()
        rag_ctx = _rag_context(text)
        rag_elapsed_ms = (_time.monotonic() - t0_rag) * 1000
        print(f"[timing] rag={rag_elapsed_ms:.0f}ms  query={text[:60]!r}")
        if rag_ctx:
            messages.append({"role": "system", "content": rag_ctx})
        if language and language not in ("auto", "en"):
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"IMPORTANT: You MUST reply in language code '{language}'. "
                        f"Do not switch to English. Reply only in '{language}'."
                    ),
                }
            )
        messages.append({"role": "user", "content": text})

        # ── stream the LLM ──
        full_text = ""
        sent_buf = ""  # accumulates text until first sentence boundary
        audio_fired = False
        tts_futures: list[_futures.Future[bytes | None]] = []
        tool_chunks: dict = {}
        SENT_END = frozenset(  # noqa: N806
            "!?\u3002\uff01\uff1f\u2026"
        )  # period removed — caught by SENT_END_PERIOD below
        SENT_END_PERIOD = frozenset(".")  # noqa: N806
        MIN_LEN = 28  # raised: avoids cutting on "Good morning, Mr." (18 chars)  # noqa: N806
        t0_llm = _time.monotonic()
        t_first_tok = None

        def _do_tts(text: str) -> bytes | None:
            """Synthesise speech for *text* — runs in a thread pool worker."""
            t0 = _time.monotonic()
            try:
                if tts_provider == "google":
                    au = _tts_google(text, voice, language)
                elif tts_provider == "cartesia":
                    au = _tts_cartesia(text, voice, language)
                elif tts_provider == "elevenlabs":
                    au = _tts_elevenlabs(text, voice, language)
                else:
                    au = _tts_openai(text, voice)
                print(f"[timing] tts={(_time.monotonic() - t0) * 1000:.0f}ms  chars={len(text)}")
                return au
            except Exception as tts_err:
                print(f"[chat] TTS error: {tts_err}")
                return None

        def _drain_tts() -> None:
            """Push any TTS futures that completed, in submission order."""
            while tts_futures and tts_futures[0].done():
                fut = tts_futures.pop(0)
                try:
                    au = fut.result()
                    if au:
                        push({"type": "audio", "data": base64.b64encode(au).decode()})
                except Exception as _e:
                    print(f"[chat] TTS drain: {_e}")

        def _stream_tok(tok: str) -> None:
            nonlocal full_text, sent_buf, t_first_tok, audio_fired
            if t_first_tok is None:
                t_first_tok = _time.monotonic()
                print(f"[timing] llm_first_token={(_time.monotonic() - t0_llm) * 1000:.0f}ms")
            full_text += tok
            sent_buf += tok
            push({"type": "text", "chunk": tok})
            # Push any TTS that finished while we were streaming — every token
            _drain_tts()
            last_ch = sent_buf[-1]
            if len(sent_buf) < MIN_LEN or (
                last_ch not in SENT_END and last_ch not in SENT_END_PERIOD
            ):
                return
            # For period: skip abbreviations — word before "." is ≤3 chars (Mr., Dr., etc.)
            if last_ch in SENT_END_PERIOD:
                words = sent_buf.rstrip().rsplit(None, 1)
                if words and len(words[-1].rstrip(".")) <= 3:
                    return
            # Submit TTS to thread pool — does NOT block LLM stream consumption
            sentence = sent_buf.strip()
            sent_buf = ""
            audio_fired = True
            tts_futures.append(_tts_executor.submit(_do_tts, sentence))

        try:
            if model.startswith("claude"):
                import anthropic as _ant

                ant_client = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                system_parts, conv_msgs = [], []
                for m in messages:
                    if m["role"] == "system":
                        system_parts.append(m.get("content") or "")
                    elif m["role"] in ("user", "assistant") and m.get("content"):
                        conv_msgs.append({"role": m["role"], "content": m["content"]})
                print(f"[timing] model={model}  turns={len(conv_msgs)}")
                with ant_client.messages.stream(
                    model=model,
                    max_tokens=150,
                    system="\n\n".join(system_parts),
                    messages=conv_msgs,
                ) as stream:
                    for tok in stream.text_stream:
                        _stream_tok(tok)
            else:
                oai_client = _get_oai_client()
                t_api_call = _time.monotonic()
                oai_stream = oai_client.chat.completions.create(
                    model=model,
                    max_tokens=250,
                    messages=messages,
                    stream=True,
                    tools=_TOOLS or None,
                    tool_choice="auto" if _TOOLS else None,
                )
                print(
                    f"[timing] oai_stream_connected="
                    f"{(_time.monotonic() - t_api_call) * 1000:.0f}ms  "
                    f"input_msgs={len(messages)}"
                )
                finish_reason = None
                for chunk in oai_stream:
                    if not chunk.choices:
                        continue
                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_chunks:
                                tool_chunks[idx] = {
                                    "id": tc.id or "",
                                    "name": (tc.function.name or "") if tc.function else "",
                                    "args": "",
                                }
                            if tc.function and tc.function.arguments:
                                tool_chunks[idx]["args"] += tc.function.arguments
                        continue
                    if delta.content:
                        _stream_tok(delta.content)
                if finish_reason:
                    print(f"[timing] finish_reason={finish_reason}")
                    if finish_reason == "length":
                        print("[WARNING] reply truncated at max_tokens")

            print(
                f"[timing] llm_full={(_time.monotonic() - t0_llm) * 1000:.0f}ms  "
                f"tokens\u2248{len(full_text.split())}"
            )

        except Exception as llm_err:
            push({"type": "error", "error": str(llm_err)})
            finish()
            _audit.write_failure(
                client_ip=client_ip,
                forwarded_for=forwarded_for,
                user_agent=user_agent,
                method="POST",
                path="/api/chat",
                status_code=500,
                error=f"llm_error: {str(llm_err)[:200]}",
                request_id=request_id,
                session_id=session_id,
            )
            return

        llm_elapsed_ms = round((_time.monotonic() - t0_llm) * 1000, 1)

        # ── queue any trailing text (reply too short or last sentence fragment) ──
        if sent_buf.strip():
            tts_futures.append(_tts_executor.submit(_do_tts, sent_buf.strip()))
            audio_fired = True
            sent_buf = ""

        # ── execute tool calls → second non-streaming LLM pass ──
        if tool_chunks:
            tc_list = [
                {
                    "id": tool_chunks[i]["id"],
                    "type": "function",
                    "function": {
                        "name": tool_chunks[i]["name"],
                        "arguments": tool_chunks[i]["args"],
                    },
                }
                for i in sorted(tool_chunks)
            ]
            messages.append({"role": "assistant", "content": None, "tool_calls": tc_list})
            for tcd in tc_list:

                class _TC:  # minimal stand-in matching openai ToolCall interface
                    id = tcd["id"]
                    function = type(
                        "_F",
                        (),
                        {
                            "name": tcd["function"]["name"],
                            "arguments": tcd["function"]["arguments"],
                        },
                    )()

                result = _handle_tool_call(_TC(), session_id)
                messages.append({"role": "tool", "tool_call_id": tcd["id"], "content": result})
            try:
                t0_llm2 = _time.monotonic()
                r2 = client.chat.completions.create(  # noqa: F821
                    model=model, max_tokens=150, messages=messages, stream=False
                )
                print(f"[timing] llm_tool_followup={(_time.monotonic() - t0_llm2) * 1000:.0f}ms")
                full_text = (r2.choices[0].message.content or "").strip()
                messages.append({"role": "assistant", "content": full_text})
                push({"type": "text", "chunk": full_text})
            except Exception as e2:
                push({"type": "error", "error": str(e2)})
                finish()
                return
        else:
            messages.append({"role": "assistant", "content": full_text})

        # ── flush remaining TTS futures (any not yet drained during streaming) ──
        # _drain_tts() already pushed completed ones per-token; this catches
        # anything that was still in-flight when the LLM loop finished.
        for fut in list(tts_futures):
            try:
                au = fut.result(timeout=20)
                if au:
                    push({"type": "audio", "data": base64.b64encode(au).decode()})
            except Exception as tts_err:
                print(f"[chat] TTS future error: {tts_err}")
        tts_futures.clear()

        # ── fallback TTS: whole reply was too short to hit any sentence boundary ──
        if not audio_fired and full_text:
            au = _do_tts(full_text)
            if au:
                push({"type": "audio", "data": base64.b64encode(au).decode()})

        # ── bound history ──
        if len(messages) > 42:
            _sessions[session_id] = [messages[0]] + messages[-40:]

        _session_turn_counters[session_id] = _session_turn_counters.get(session_id, 0) + 1
        _audit.write_turn(
            session_id=session_id,
            turn_number=_session_turn_counters[session_id],
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            user_agent=user_agent,
            channel="chat_http",
            hotel_id="demo",
            user_query=text,
            bot_reply=full_text,
            model=model,
            language=language,
            tts_provider=tts_provider,
            llm_ms=llm_elapsed_ms,
            rag_ms=round(rag_elapsed_ms, 1) if rag_elapsed_ms is not None else None,
            status="answered",
            request_id=request_id,
        )
        push({"type": "done", "session_id": session_id, "text": full_text})
        finish()

    def _handle_product_chat(self):
        """POST /api/product-chat — streaming NDJSON: Voxtera product Q&A backed by the
        product knowledge base (docs/voxtera-rag-knowledge-base.md).

        Same NDJSON protocol as /api/chat:
          {"type": "text",  "chunk": "<token>"}
          {"type": "done",  "session_id": "...", "text": "<full reply>"}
          {"type": "error", "error": "..."}
        """
        import os as _os
        import time as _time

        import openai as _oai

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        text = (body.get("text") or "").strip()
        session_id = body.get("session_id") or str(uuid.uuid4())
        model = body.get("model") or "gpt-4o-mini"

        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def push(obj: dict) -> None:
            line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
            self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
            self.wfile.flush()

        def finish() -> None:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        if not text:
            push({"type": "error", "error": "text is required"})
            finish()
            return

        # Build / retrieve session history
        if session_id not in _product_sessions:
            _product_sessions[session_id] = [{"role": "system", "content": _PRODUCT_SYSTEM_PROMPT}]
        messages = _product_sessions[session_id]

        # RAG retrieval from product KB
        rag_ctx = _product_rag_context(text)

        # Inject RAG context + user message
        user_content = text
        if rag_ctx:
            user_content = rag_ctx + "\n\n---\n\nUser question: " + text
        messages.append({"role": "user", "content": user_content})

        api_key = _os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            push({"type": "error", "error": "OPENAI_API_KEY not configured on server."})
            finish()
            return

        client = _oai.OpenAI(api_key=api_key)
        full_text = ""

        try:
            t0 = _time.monotonic()
            stream = client.chat.completions.create(
                model=model,
                max_tokens=300,
                messages=messages,
                stream=True,
            )
            for event in stream:
                delta = event.choices[0].delta
                if delta.content:
                    full_text += delta.content
                    push({"type": "text", "chunk": delta.content})
            elapsed = (_time.monotonic() - t0) * 1000
            print(f"[product-chat] llm={elapsed:.0f}ms  chars={len(full_text)}")
        except Exception as llm_err:
            push({"type": "error", "error": str(llm_err)})
            finish()
            return

        messages.append({"role": "assistant", "content": full_text})

        # Bound history to last 20 turns + system prompt
        if len(messages) > 42:
            _product_sessions[session_id] = [messages[0]] + messages[-40:]

        push({"type": "done", "session_id": session_id, "text": full_text})
        finish()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    # Resolve the launcher base URL once we know the actual port. Spawned bot
    # subprocesses receive this as ``VOXTERA_LAUNCHER_URL`` and POST events to
    # ``{LAUNCHER_BASE_URL}/api/bot-event`` via ``launcher_client.post_event``.
    LAUNCHER_BASE_URL = f"http://127.0.0.1:{port}"

    # --- Embedding sidecar ---------------------------------------------------
    # Start the embedding server so bot subprocesses can get embeddings without
    # loading the ONNX model themselves (saves 3-8s per session cold-start).
    _EMBEDDING_PORT = 9400
    _EMBEDDING_URL = f"http://127.0.0.1:{_EMBEDDING_PORT}"
    _embedding_proc: subprocess.Popen | None = None
    _rag_enabled = os.environ.get("RAG_ENABLED", "false").lower() in ("1", "true", "yes")

    if _rag_enabled:
        _embedding_script = str(
            Path(__file__).resolve().parent.parent / "scripts" / "embedding_server.py"
        )
        _embedding_proc = subprocess.Popen(
            [sys.executable, _embedding_script, "--port", str(_EMBEDDING_PORT)],
            cwd=str(_VOXTERA_ROOT),
        )
        # Expose the URL so bot subprocesses (which inherit os.environ) use it.
        os.environ["VOXTERA_EMBEDDING_URL"] = _EMBEDDING_URL
        print(f"Embedding sidecar starting on {_EMBEDDING_URL} (pid={_embedding_proc.pid})")
    # --------------------------------------------------------------------------
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", port), DemoHandler) as httpd:
        print(f"Serving demo on http://localhost:{port}/demo.html")
        print(
            f"On-demand launcher ready — bot callback URL: "
            f"{LAUNCHER_BASE_URL}/api/bot-event "
            f"(spawn timeout: {_SPAWN_TIMEOUT_SECS}s)"
        )
        if _ADMIN_TOKEN and _DAILY_API_KEY and (_DAILY_ROOM_NAME or _DAILY_DYNAMIC_ROOMS):
            print(f"Admin page on http://localhost:{port}/admin.html")
            if _DAILY_DYNAMIC_ROOMS:
                print(f"  Dynamic rooms enabled (max_participants={_DAILY_ROOM_MAX_PARTICIPANTS})")
        else:
            missing = []
            if not _ADMIN_TOKEN:
                missing.append("VOXTERA_ADMIN_TOKEN")
            if not _DAILY_API_KEY:
                missing.append("DAILY_API_KEY")
            if not _DAILY_ROOM_NAME and not _DAILY_DYNAMIC_ROOMS:
                missing.append("DAILY_ROOM_NAME (or DAILY_DYNAMIC_ROOMS=1)")
            print(f"Admin page disabled — missing env: {', '.join(missing)}")
        if _ADMIN_TOKEN:
            print(f"Trace page on http://localhost:{port}/trace.html")
        else:
            print("Trace page disabled — set VOXTERA_ADMIN_TOKEN to enable")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
        finally:
            if _embedding_proc is not None:
                _embedding_proc.terminate()
                _embedding_proc.wait(timeout=5)
