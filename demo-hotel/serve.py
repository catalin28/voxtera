"""Simple HTTP server for the demo frontend with TTS test and chat endpoints.

Serves static files for the demo page and exposes:

- ``POST /api/tts-test`` — real OpenAI / Google TTS so the browser can
  play the bot's greeting in the selected voice and language.
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
import uuid
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

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_id is not None

    def active_session(self) -> str | None:
        with self._lock:
            return self._active_id


REGISTRY = BotSessionRegistry()


def _spawn_bot(session_id: str, callback_url: str) -> subprocess.Popen:
    """Spawn ``python -m voxtera.bot`` as a subprocess for this session.

    The subprocess inherits the launcher's environment plus the two new vars
    the bot's ``launcher_client`` reads at import time. stdout/stderr are
    inherited so bot logs land in the launcher's terminal — keeps debugging
    simple. For production, swap to ``subprocess.PIPE`` and tee to a file.
    """
    env = os.environ.copy()
    env["VOXTERA_SESSION_ID"] = session_id
    env["VOXTERA_LAUNCHER_URL"] = callback_url

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

# Language code → full name map for the LLM translation prompt.
_LANG_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ro": "Romanian",
    "tr": "Turkish",
    "nl": "Dutch",
    "ja": "Japanese",
    "hi": "Hindi",
    "ru": "Russian",
    "ar": "Arabic",
    "zh": "Chinese",
    "ko": "Korean",
    "pl": "Polish",
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "el": "Greek",
    "fi": "Finnish",
    "he": "Hebrew",
    "hu": "Hungarian",
    "id": "Indonesian",
    "no": "Norwegian",
    "sv": "Swedish",
    "th": "Thai",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "az": "Azerbaijani",
}


def _translate_greeting(text: str, lang: str, model: str) -> str:
    """Use an OpenAI LLM to translate the greeting into the target language."""
    import os

    import openai

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    lang_name = _LANG_NAMES.get(lang, lang)
    response = client.chat.completions.create(
        model=model,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Translate the following greeting into {lang_name}. "
                    "Return ONLY the translated text, nothing else.\n\n"
                    f"{text}"
                ),
            }
        ],
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


def _tts_google(text: str, voice: str, language: str) -> bytes:
    """Generate speech via Google Chirp 3 HD and return raw MP3 bytes."""
    import os

    from google.cloud import texttospeech

    os.environ.setdefault(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
    )
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language if "-" in language else f"{language}-US",
        name=voice,
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
        if self.path == "/api/admin/health":
            return self._handle_admin_health()
        if self.path == "/api/admin/sessions":
            return self._handle_admin_sessions()
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
        # Phase 3 — on-demand bot launcher
        if self.path == "/api/start-session":
            return self._handle_start_session()
        if self.path == "/api/bot-event":
            return self._handle_bot_event()
        self.send_error(404)
        return None

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
          1. If a session is already in flight → 409 Busy.
          2. Reserve a session slot, spawn ``python -m voxtera.bot``,
             attach a reaper thread.
          3. Block on the session's queue (``q.get(timeout=15)``) until the
             bot POSTs ``{type:"ready"}`` to ``/api/bot-event`` from inside
             its ``on_joined`` Daily handler.
          4. Return ``{room_url, session_id}`` so the browser can join.
        """
        if REGISTRY.is_busy():
            active = REGISTRY.active_session()
            print(f"[launcher] /api/start-session rejected — session {active} is active")
            self._send_json(409, {"error": "busy", "active_session": active})
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

        try:
            proc = _spawn_bot(session_id, callback_url)
        except Exception as exc:
            print(f"[launcher] spawn failed: {exc}")
            REGISTRY.reap(session_id)
            self._send_json(500, {"error": f"spawn failed: {exc}"})
            return

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

        Body: ``{"session_id": "...", "type": "ready"|"error"|"exiting", ...}``.
        We do not validate the event shape strictly — extra fields are kept
        and propagated through the queue so future event types Just Work.
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

        REGISTRY.deliver(session_id, body)
        # 204 No Content — nothing to return; the bot doesn't care.
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

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
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
