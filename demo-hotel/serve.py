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

from voxtera.actions import (  # noqa: E402
    build_openai_tools,
    compose_system_prompt,
    load_hotel_config,
)
from voxtera.actions.logging_sink import LoggingSink  # noqa: E402
from voxtera.actions.ticket import Category, Ticket  # noqa: E402
from voxtera.admin import (  # noqa: E402
    DailyAPIError,
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

# Tracks the bot tune-server port for the live session. Single-slot for v1
# (matches BotSessionRegistry's single-session constraint). Set by
# /api/start-session at spawn time and read by /api/admin/tune.
_BOT_TUNE_PORT: int | None = None
_BOT_TUNE_LOCK = threading.Lock()


def _set_bot_tune_port(port: int | None) -> None:
    global _BOT_TUNE_PORT
    with _BOT_TUNE_LOCK:
        _BOT_TUNE_PORT = port


def _get_bot_tune_port() -> int | None:
    with _BOT_TUNE_LOCK:
        return _BOT_TUNE_PORT


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
    """Thread-safe single-slot registry for the in-flight bot session.

    The launcher accepts at most one concurrent session. Each session is keyed
    by a UUID and owns a ``queue.Queue`` for events flowing back from the bot
    subprocess. The start-session HTTP handler thread blocks on ``q.get()``
    until the bot-event handler thread does ``q.put()``; this is the queue
    described in ``docs/ON_DEMAND_BOT_SPAWN.md`` (option 1: HTTP + queue.Queue).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_id: str | None = None
        self._sessions: dict[str, dict] = {}

    def start(self, session_id: str) -> "_queue.Queue":
        """Reserve the slot and return a fresh queue for this session."""
        q: _queue.Queue = _queue.Queue()
        with self._lock:
            if self._active_id is not None:
                raise BotSessionBusyError(self._active_id)
            self._active_id = session_id
            self._sessions[session_id] = {"queue": q, "process": None}
        return q

    def attach_process(self, session_id: str, proc: subprocess.Popen) -> None:
        """Stash the Popen handle so the reaper can find it."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["process"] = proc

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
            if self._active_id == session_id:
                self._active_id = None
        # Wake any thread still blocked on q.get() so it can return cleanly
        # rather than hitting the timeout.
        if sess is not None:
            with contextlib.suppress(Exception):
                sess["queue"].put({"type": "_reaped"})
        # Close the session's NDJSON handle and write the meta sidecar so the
        # dashboard's session picker shows the right summary fields. Safe to
        # call for sessions that never produced trace events (no-op).
        with contextlib.suppress(Exception):
            _SESSION_STORE.finalize(session_id)

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_id is not None

    def active_session(self) -> str | None:
        with self._lock:
            return self._active_id


REGISTRY = BotSessionRegistry()


def _spawn_bot(
    session_id: str, callback_url: str, tune_port: int, llm_model: str | None = None
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
        _set_bot_tune_port(None)

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
# Chat sessions — simple in-memory conversation history keyed by session id
# ---------------------------------------------------------------------------
_sessions: dict[str, list[dict[str, str]]] = {}


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

    import openai

    if session_id not in _sessions:
        _sessions[session_id] = [{"role": "system", "content": _ACTIONS_SYSTEM_PROMPT}]

    messages = _sessions[session_id]

    # Inject RAG context before the user message.
    rag_ctx = _rag_context(user_text)
    if rag_ctx:
        messages.append({"role": "system", "content": rag_ctx})

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

    def do_GET(self):  # noqa: N802
        # Admin endpoints first; everything else falls through to the static
        # file handler in SimpleHTTPRequestHandler.
        if self.path == "/api/languages":
            return self._handle_languages()
        if self.path == "/api/admin/health":
            return self._handle_admin_health()
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
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path == "/api/tts-test":
            return self._handle_tts_test()
        if self.path == "/api/chat":
            return self._handle_chat()
        if self.path == "/api/admin/eject":
            return self._handle_admin_eject()
        if self.path == "/api/admin/end-session":
            return self._handle_admin_end_session()
        if self.path == "/api/admin/tune":
            return self._handle_admin_tune()
        # Phase 3 — on-demand bot launcher
        if self.path == "/api/start-session":
            return self._handle_start_session()
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
        if not _DAILY_API_KEY or not _DAILY_ROOM_NAME:
            self._send_json(
                503,
                {
                    "error": "daily_unconfigured",
                    "detail": "DAILY_API_KEY and DAILY_ROOM_NAME must be set on the server.",
                },
            )
            return False, {}
        provided = self.headers.get("X-Admin-Token", "")
        if provided != _ADMIN_TOKEN:
            self._send_json(401, {"error": "unauthorized"})
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
                "daily_configured": bool(_DAILY_API_KEY and _DAILY_ROOM_NAME),
                "daily_room": _DAILY_ROOM_NAME or "",
                "daily_domain": _DAILY_DOMAIN or "",
                "bot_name": _BOT_NAME,
            },
        )

    # ------------------------------------------------------------------
    # /api/admin/sessions — live snapshot from Daily REST
    # ------------------------------------------------------------------

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

        assert _DAILY_API_KEY and _DAILY_ROOM_NAME  # checked by _admin_auth
        participants = list_room_participants(
            api_key=_DAILY_API_KEY,
            room_name=_DAILY_ROOM_NAME,
        )
        _presence_cache["fetched_at"] = now
        _presence_cache["value"] = participants
        return participants

    def _handle_admin_sessions(self) -> None:
        ok, _ = self._admin_auth()
        if not ok:
            return
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

    def _do_eject(self, ids: list[str]) -> None:
        assert _DAILY_API_KEY and _DAILY_ROOM_NAME  # checked by _admin_auth
        try:
            ejected = eject_participants(
                api_key=_DAILY_API_KEY,
                room_name=_DAILY_ROOM_NAME,
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
        # the eject immediately instead of showing the just-kicked participant
        # for up to 500 ms.
        _presence_cache["fetched_at"] = 0.0
        _presence_cache["value"] = None

        # Audit trail. Loguru already routes to logs/, this gives us a
        # grep-able line per eject with the operator's IP.
        operator_ip = self.address_string()
        for pid in ejected:
            sys.stderr.write(f"[admin] eject room={_DAILY_ROOM_NAME} ip={operator_ip} id={pid}\n")

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
        self._do_eject(ids)

    # ------------------------------------------------------------------
    # /api/admin/end-session — eject everyone
    # ------------------------------------------------------------------

    def _handle_admin_end_session(self) -> None:
        ok, _ = self._admin_auth()
        if not ok:
            return
        # Re-fetch fresh (bypass the 500 ms cache) so we don't miss someone
        # who joined within the cache window.
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
        freed only when the bot subprocess exits. That slot leaks whenever
        the bot hangs on shutdown, which made Start reject every subsequent
        click until the launcher was restarted — even though the Daily room
        was empty. Asking Daily directly removes that whole failure mode.
        """
        # ------------------------------------------------------------------
        # Busy gate — Daily presence is the source of truth.
        # ``live`` semantics:
        #   list  → Daily answered; empty means the room is free.
        #   None  → Daily REST was unreachable; fall back to local registry
        #           so we don't spawn unlimited bots during a Daily outage.
        # ------------------------------------------------------------------
        live: list | None
        try:
            if _DAILY_API_KEY and _DAILY_ROOM_NAME:
                live = list_room_participants(
                    api_key=_DAILY_API_KEY,
                    room_name=_DAILY_ROOM_NAME,
                )
            else:
                # No Daily creds configured — skip the remote check and
                # rely on the in-memory registry below.
                print("[launcher] DAILY_API_KEY/ROOM not set — skipping presence check")
                live = None
        except DailyAPIError as exc:
            print(f"[launcher] daily presence check failed: {exc}")
            live = None

        # Split the participants into humans and orphan bots. The bot's own
        # display name (``BOT_NAME``, default "Voxtera") joining the room
        # does NOT count as "the demo is in use" — that's just our process
        # from a previous run that never left cleanly. Only a real guest
        # constitutes an active session.
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

            # Only the bot is there. Eject it so the freshly spawned bot
            # doesn't share the room with its own ghost — Daily would bill
            # for both, and the old process is no longer wired to anything.
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
                    # Eject failed — refuse to spawn rather than risk
                    # double-bot in the room. Operator can clear it from
                    # the admin page.
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

        # Daily says nobody real is in the room. If the in-memory registry
        # still holds a slot, it's stale (bot subprocess hung on shutdown);
        # reap it so the spawn below can grab a fresh slot.
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

        # Daily was unreachable — fall back to the legacy in-memory gate so a
        # Daily blip doesn't allow concurrent spawns.
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

        # Pick a tune port for this bot. v1 reuses the default base port for
        # every session because we only ever have one live session at a time.
        # When multi-session lands, allocate a unique port per session.
        tune_port = _DEFAULT_BOT_PORT
        try:
            proc = _spawn_bot(session_id, callback_url, tune_port, llm_model=llm_model)
        except Exception as exc:
            print(f"[launcher] spawn failed: {exc}")
            REGISTRY.reap(session_id)
            self._send_json(500, {"error": f"spawn failed: {exc}"})
            return
        _set_bot_tune_port(tune_port)

        REGISTRY.attach_process(session_id, proc)
        _start_reaper_thread(session_id, proc)

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
            print(f"[launcher] session {session_id}: bot exited before ready " f"(rc={rc})")
            self._send_json(
                500,
                {"error": f"bot exited before ready (rc={rc})"},
            )
            return

        if event_type != "ready":
            print(f"[launcher] session {session_id}: unexpected first event " f"{event_type!r}")
            with contextlib.suppress(Exception):
                proc.kill()
            self._send_json(500, {"error": f"unexpected first event: {event_type}"})
            return

        # Bot is in the room. Build the room URL and hand back to the browser.
        if not _DAILY_DOMAIN or not _DAILY_ROOM_NAME:
            print("[launcher] DAILY_DOMAIN / DAILY_ROOM_NAME missing — killing bot")
            with contextlib.suppress(Exception):
                proc.kill()
            self._send_json(
                500,
                {"error": "DAILY_DOMAIN or DAILY_ROOM_NAME not set on launcher"},
            )
            return

        room_url = f"https://{_DAILY_DOMAIN}/{_DAILY_ROOM_NAME}"
        print(f"[launcher] session {session_id} ready — returning room_url to browser")
        self._send_json(
            200,
            {"session_id": session_id, "room_url": room_url, "bot_name": _BOT_NAME},
        )

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
        """POST /api/chat — LLM chat with RAG + TTS audio response."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            text = (body.get("text") or "").strip()
            session_id = body.get("session_id") or str(uuid.uuid4())
            model = body.get("model") or "gpt-4o-mini"
            language = body.get("language") or "en"
            tts_provider = body.get("tts_provider") or "openai"
            voice = body.get("voice") or "nova"

            if not text:
                resp = json.dumps({"error": "text is required"}).encode()
                self.send_response(400)
                self._cors_headers()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return

            # LLM chat with RAG context injection.
            reply = _chat_completion(session_id, text, model, language)

            # Generate TTS audio for the reply.
            audio_b64 = ""
            try:
                if tts_provider == "google":
                    audio = _tts_google(reply, voice, language)
                elif tts_provider == "cartesia":
                    audio = _tts_cartesia(reply, voice, language)
                elif tts_provider == "elevenlabs":
                    audio = _tts_elevenlabs(reply, voice, language)
                else:
                    audio = _tts_openai(reply, voice)
                audio_b64 = base64.b64encode(audio).decode("ascii")
            except Exception as tts_exc:
                print(f"[chat] TTS failed ({tts_exc}), returning text only")

            resp = json.dumps(
                {
                    "text": reply,
                    "audio": audio_b64,
                    "session_id": session_id,
                }
            ).encode()
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as exc:
            error_msg = json.dumps({"error": str(exc)}).encode()
            self.send_response(500)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_msg)))
            self.end_headers()
            self.wfile.write(error_msg)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    # Resolve the launcher base URL once we know the actual port. Spawned bot
    # subprocesses receive this as ``VOXTERA_LAUNCHER_URL`` and POST events to
    # ``{LAUNCHER_BASE_URL}/api/bot-event`` via ``launcher_client.post_event``.
    LAUNCHER_BASE_URL = f"http://127.0.0.1:{port}"
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", port), DemoHandler) as httpd:
        print(f"Serving demo on http://localhost:{port}/demo.html")
        print(
            f"On-demand launcher ready — bot callback URL: "
            f"{LAUNCHER_BASE_URL}/api/bot-event "
            f"(spawn timeout: {_SPAWN_TIMEOUT_SECS}s)"
        )
        if _ADMIN_TOKEN and _DAILY_API_KEY and _DAILY_ROOM_NAME:
            print(f"Admin page on http://localhost:{port}/admin.html")
        else:
            missing = []
            if not _ADMIN_TOKEN:
                missing.append("VOXTERA_ADMIN_TOKEN")
            if not _DAILY_API_KEY:
                missing.append("DAILY_API_KEY")
            if not _DAILY_ROOM_NAME:
                missing.append("DAILY_ROOM_NAME")
            print(f"Admin page disabled — missing env: {', '.join(missing)}")
        if _ADMIN_TOKEN:
            print(f"Trace page on http://localhost:{port}/trace.html")
        else:
            print("Trace page disabled — set VOXTERA_ADMIN_TOKEN to enable")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
