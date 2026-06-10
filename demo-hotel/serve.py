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
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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

# ── Admin-editable prompt registry ──────────────────────────────────────────
# Every LLM prompt of the call-center concierge, with a plain-language
# explanation shown in the admin editor. Files live in
# src/voxtera/call_center/prompts/ and HOT-RELOAD on save (mtime cache in the
# prompt loader; escalation_stems.json has its own mtime reload).
_PROMPT_REGISTRY: dict[str, dict] = {
    "concierge_persona": {
        "file": "concierge_persona.md",
        "title": "Persona (ONE place for tone & character)",
        "description": (
            "Who the assistant IS — tone, no stock openers, spoken format, "
            "language behaviour. Automatically prepended to ALL THREE answer "
            "writers (hotel, web, conversational), so the persona is changed "
            "here ONCE and applies everywhere. The task prompts below contain "
            "only their task-specific rules."
        ),
    },
    "query_decomposer": {
        "file": "query_decomposer.md",
        "title": "Query decomposer (routing brain)",
        "description": (
            "Converts each guest message into the structured plan that drives "
            "everything else: query type (hotel / destination / web / hybrid / "
            "escalate / conversational), hotel mention, region, and the "
            "requirements used for retrieval. Edit this to change how questions "
            "are classified and what gets extracted."
        ),
    },
    "concierge_render": {
        "file": "concierge_render.md",
        "title": "Hotel answer writer",
        "description": (
            "Writes the spoken answer from hotel knowledge-base results. Carries "
            "the concierge persona, the grounding rules (never invent amenities, "
            "locations or landmarks; admit region mismatches), and the "
            "offer-to-check-online behaviour when a detail is missing."
        ),
    },
    "concierge_web_synth": {
        "file": "concierge_web_synth.md",
        "title": "Web answer writer",
        "description": (
            "Writes the spoken answer from live web results, and the combined "
            "hotel+web answer on hybrid turns. Controls persona, the on-site vs "
            "arranged-nearby accuracy rule, the destination-question format "
            "(top options + one clear recommendation), and the natural 'worth "
            "confirming' note."
        ),
    },
    "concierge_converse": {
        "file": "concierge_converse.md",
        "title": "Conversational turns",
        "description": (
            "Handles chitchat and meta turns answered from the conversation "
            "history — greetings, thanks, 'what did I ask you?', 'where are we "
            "with the itinerary?'. Explicitly forbidden from promising actions "
            "it cannot perform (searching, booking, sending)."
        ),
    },
    "concierge_web_query": {
        "file": "concierge_web_query.md",
        "title": "Web search query builder",
        "description": (
            "Rewrites the conversation into ONE self-contained web search query "
            "— resolves 'there'/'they' to the actual hotel and place from the "
            "dialog, so the web search is anchored even when the guest uses "
            "pronouns."
        ),
    },
    "escalation_classifier": {
        "file": "escalation_classifier.md",
        "title": "Escalation judge (LLM)",
        "description": (
            "Decides whether a turn must be handed to a human: booking, "
            "cancellation/changes, live complaint, medical, urgency. Only runs "
            "when the fast-gate word list (below) finds a trigger word."
        ),
    },
    "escalation_stems": {
        "file": "escalation_stems.json",
        "title": "Escalation fast-gate word list (JSON)",
        "description": (
            "Stem list checked before the escalation judge. If a message "
            "contains NONE of these stems, the LLM judge is skipped (saves "
            "~1.5s on normal turns). Stems, not words: 'rezerv' matches "
            "rezervasyon/rezervasyonumu. JSON must stay valid — saves are "
            "validated. Hot-reloads instantly."
        ),
    },
    "triage_questions": {
        "file": "triage_questions.md",
        "title": "Triage clarification questions",
        "description": (
            "The localised clarification questions triage can ask when a "
            "request is missing a critical slot (per-locale '## <locale>' "
            "sections with '- slot: text' lines)."
        ),
    },
    "concierge_decompose_legacy": {
        "file": "concierge_decompose_legacy.md",
        "title": "Legacy decomposer (old endpoint)",
        "description": (
            "Used only by the older single-shot concierge endpoint, NOT by the "
            "main pipeline. Kept for backwards compatibility."
        ),
    },
}


def _prompts_dir() -> Path:
    from voxtera.call_center import prompts as _p

    return Path(_p.__file__).resolve().parent


# ── Voice-concierge prompt registry (Voice Concierge Prompts admin page) ────
# Prompts of the HOTEL VOICE CONCIERGE — the real-time voice agent with the
# "Her"-inspired persona. Unlike the call-center prompts above, these do NOT
# hot-reload: the voice bot runs as a separate subprocess spawned per call and
# imports its prompts at startup, so saves apply from the NEXT CALL.
# Entries with "readonly": True are shown in the editor for transparency but
# cannot be saved (Python modules / latency-critical code).
_VOICE_PROMPT_REGISTRY: dict[str, dict] = {
    "system_prompt": {
        "file": "system_prompt.md",
        "title": "Voice concierge system prompt (the 'Her' persona)",
        "description": (
            "THE main prompt of the voice agent. Three jobs in tension: "
            "PRESENCE (the 'Her'-inspired persona — warm, polished, attentive, "
            "never robotic), BREVITY (every word is ~330ms of TTS playback "
            "during which the guest's mic is silenced — keep replies short), "
            "and LANGUAGE CONSISTENCY (reply in the guest's language for the "
            "whole call). ⚠️ This text is also embedded in audio.py as a "
            "semantic fingerprint that calibrates the STT noise filter — tone "
            "edits are fine, but do NOT strip the hotel/travel vocabulary "
            "wholesale or the filter's baseline drifts. Applies from the NEXT "
            "CALL (the bot reads it once at startup)."
        ),
    },
    "greetings": {
        "file": "greetings.json",
        "title": "Startup greetings — 31 languages (JSON)",
        "description": (
            "What the bot says the moment a call starts, before the guest "
            "speaks — spoken instantly with no LLM round-trip. Two sections: "
            "'greetings' (one time-neutral greeting per language — the safe "
            "default) and 'timed_greetings' (morning/afternoon/evening "
            "variants, used when the browser reports the guest's timezone). "
            "'en' is required; timed variants may repeat where a language "
            "doesn't daypart (French afternoon, Korean, Hindi — intentional). "
            "The optional {hotel_name} placeholder becomes the hotel's name at "
            "bot startup ('Welcome to Casa Dell Arte'), or the 'generic_hotel' "
            "phrase when no hotel is configured. Saves are validated. Applies "
            "from the NEXT CALL; the TTS-test admin endpoint keeps the old "
            "text until a server restart."
        ),
    },
    "fillers": {
        "file": "fillers.py",
        "readonly": True,
        "title": "Instant-ack fillers (read-only, Python)",
        "description": (
            "Short backchannels ('One moment.') spoken within ~100ms of the "
            "guest finishing, masking STT→LLM→TTS latency. Read-only: "
            "latency-critical and deliberately hardcoded — no file read or "
            "validation may sit on this path. Edit in code if needed."
        ),
    },
    "actions_fragment": {
        "file": "../actions/prompt.py",
        "readonly": True,
        "title": "Action-taking fragment (read-only, Python)",
        "description": (
            "Teaches Claude the create_ticket flow (confirm before filing, "
            "summary in the hotel's staff language). Read-only: this is "
            "Python that GENERATES the prompt at bot startup, parameterised "
            "by each hotel's config — there is no single text to edit."
        ),
    },
}


def _voice_prompts_dir() -> Path:
    from voxtera import prompts as _vp

    return Path(_vp.__file__).resolve().parent


def _validate_greetings_json(content: str) -> str | None:
    """Validate a greetings.json save. Returns an error string or None if OK.

    The bot import fails loudly on malformed data — this keeps a bad save from
    ever reaching disk, since a broken greetings.json would stop the voice bot
    from starting at all.
    """
    try:
        data = json.loads(content)
    except ValueError as e:
        return f"invalid JSON — {e}"
    if not isinstance(data, dict):
        return "top level must be an object"
    for key in ("greetings", "timed_greetings"):
        if not isinstance(data.get(key), dict):
            return f"missing or invalid '{key}' object"
    if "en" not in data["greetings"]:
        return "'greetings' must contain an 'en' entry (the universal fallback)"
    for lang, text in data["greetings"].items():
        if not isinstance(text, str) or not text.strip():
            return f"greetings['{lang}'] must be a non-empty string"
    dayparts = {"morning", "afternoon", "evening"}
    for lang, variants in data["timed_greetings"].items():
        if not isinstance(variants, dict):
            return f"timed_greetings['{lang}'] must be an object"
        for part, text in variants.items():
            if part not in dayparts:
                return (
                    f"timed_greetings['{lang}']: unknown daypart '{part}' "
                    "(use morning/afternoon/evening)"
                )
            if not isinstance(text, str) or not text.strip():
                return f"timed_greetings['{lang}']['{part}'] must be a non-empty string"
    generic = data.get("generic_hotel", {})
    if not isinstance(generic, dict):
        return "'generic_hotel' must be an object of language → phrase"
    for lang, text in generic.items():
        if not isinstance(text, str) or not text.strip():
            return f"generic_hotel['{lang}'] must be a non-empty string"
    return None


# ── Persistent concierge runtime ────────────────────────────────────────────
# ONE background event loop + shared connections for ALL /api/concierge
# requests. The previous per-request `asyncio.new_event_loop()` threw away
# every connection pool after each turn, so each turn paid fresh TLS handshakes
# to Anthropic/OpenAI, a Redis reconnect (the ~390ms "session_load"), and new
# aiohttp connections to the remote ES/Qdrant box. One long-lived loop keeps
# all of them warm; handlers submit work via run_coroutine_threadsafe.
_concierge_rt: dict = {"loop": None, "deps": None}
_concierge_rt_lock = threading.Lock()


def _concierge_loop() -> asyncio.AbstractEventLoop:
    with _concierge_rt_lock:
        loop = _concierge_rt["loop"]
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="concierge-loop", daemon=True).start()
            _concierge_rt["loop"] = loop
            _concierge_rt["deps"] = None
        return loop


async def _concierge_deps() -> dict:
    """Heavy shared deps, created ONCE on the persistent loop and reused.

    Holds the warm aiohttp session (ES/Qdrant), the Redis-backed SessionStore,
    and the LLM fn closures (whose Anthropic/OpenAI clients are module
    singletons that now stay bound to this one loop). Per-request pipeline
    objects are wired around these — cheap, and keeps per-run state isolated.
    """
    if _concierge_rt["deps"] is None:
        import aiohttp as _aio

        from voxtera.call_center.classifier import EscalationClassifier
        from voxtera.call_center.concierge import (
            _build_anthropic_converse,
            _build_anthropic_render,
            _build_anthropic_web_query,
            _build_anthropic_web_synth,
        )
        from voxtera.call_center.decompose import QueryDecomposer
        from voxtera.call_center.session import SessionStore

        model = os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001")
        _concierge_rt["deps"] = {
            "http": _aio.ClientSession(),
            "store": SessionStore(),
            "classifier": EscalationClassifier(),
            "decomposer": QueryDecomposer(),
            "render_fn": _build_anthropic_render(model),
            "web_synth_fn": _build_anthropic_web_synth(model),
            "converse_fn": _build_anthropic_converse(model),
            "web_query_fn": _build_anthropic_web_query(model),
        }
    return _concierge_rt["deps"]


from voxtera.actions import (  # noqa: E402
    build_openai_tools,
    compose_system_prompt,
    load_hotel_config,
)
from voxtera.actions.logging_sink import LoggingSink  # noqa: E402
from voxtera.actions.telegram_sink import TelegramSink  # noqa: E402
from voxtera.actions.ticket import Category, Ticket  # noqa: E402
from voxtera.admin import (  # noqa: E402
    DailyAPIError,
    create_room,
    delete_room,
    eject_participants,
    list_room_participants,
    list_rooms,
    list_rooms_with_presence,
)
from voxtera.call_center.embeddings import PREFIX_QUERY, embed_texts  # noqa: E402
from voxtera.call_center.index_config import ES_INDEX  # noqa: E402
from voxtera.call_center.kb_config import QDRANT_COLLECTION  # noqa: E402
from voxtera.lang_config import (  # noqa: E402
    LANG_CONFIG,
    google_locale_for,
    translation_name_for,
)
from voxtera.prompts.greetings import GREETINGS  # noqa: E402
from voxtera.prompts.system_prompt import SYSTEM_PROMPT  # noqa: E402
from voxtera.pstn_auth import verify_pinless_signature  # noqa: E402

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


def _es_base_url() -> str:
    return os.environ.get("ELASTICSEARCH_URL", "http://138.197.142.222:9200").rstrip("/")


def _qdrant_base_url() -> str:
    return os.environ.get("QDRANT_URL", "http://138.197.142.222:6333").rstrip("/")


def _json_http_request(
    url: str,
    method: str,
    *,
    payload: dict | list | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, object]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return 502, {"error": "invalid_json", "detail": raw[:1000]}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        if not raw:
            return exc.code, {}
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw[:1000]}
    except urllib.error.URLError as exc:
        return 502, {"error": "upstream_unreachable", "detail": str(exc)}


def _es_json_request(
    method: str,
    path: str,
    *,
    payload: dict | list | None = None,
) -> tuple[int, object]:
    user = os.environ.get("ELASTICSEARCH_USER", "elastic")
    password = os.environ.get("ELASTICSEARCH_PASSWORD", "")
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return _json_http_request(
        f"{_es_base_url()}{path}",
        method,
        payload=payload,
        headers={"Authorization": f"Basic {token}"},
    )


def _qdrant_json_request(
    method: str,
    path: str,
    *,
    payload: dict | list | None = None,
) -> tuple[int, object]:
    headers = {}
    api_key = os.environ.get("QDRANT_API_KEY", "")
    if api_key:
        headers["api-key"] = api_key
    return _json_http_request(
        f"{_qdrant_base_url()}{path}",
        method,
        payload=payload,
        headers=headers,
    )


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

# ---------------------------------------------------------------------------
# PSTN Telephony config
# ---------------------------------------------------------------------------
_PSTN_ENABLED: bool = os.environ.get("PSTN_ENABLED", "false").lower() in ("1", "true", "yes")
_PSTN_MODE: str = os.environ.get("PSTN_MODE", "pinless")
_PSTN_PHONE_NUMBER: str = os.environ.get("PSTN_PHONE_NUMBER", "")
_PSTN_MAX_DURATION_MIN: int = int(os.environ.get("PSTN_MAX_DURATION_MIN", "4"))
_PSTN_WEBHOOK_HMAC: str = os.environ.get("PSTN_WEBHOOK_HMAC", "")
# Verify Daily's webhook HMAC signature. Defaults ON. Only set false for local
# testing where you POST to /pstn/webhook by hand without a signature.
_PSTN_HMAC_VERIFY: bool = os.environ.get("PSTN_HMAC_VERIFY", "true").lower() in (
    "1",
    "true",
    "yes",
)
# Reject webhooks whose X-Pinless-Timestamp is more than this many seconds from
# now (replay protection). Default 5 minutes.
_PSTN_WEBHOOK_TOLERANCE_SECS: int = int(os.environ.get("PSTN_WEBHOOK_TOLERANCE_SECS", "300"))
_PSTN_MAX_CONCURRENT: int = int(os.environ.get("PSTN_MAX_CONCURRENT_CALLS", "10"))
# Per-number rate limit: max calls per number within the sliding window.
_PSTN_RATE_LIMIT_PER_NUMBER: int = int(os.environ.get("PSTN_RATE_LIMIT_PER_NUMBER", "3"))
_PSTN_RATE_LIMIT_WINDOW_SECS: int = int(os.environ.get("PSTN_RATE_LIMIT_WINDOW_SECS", "300"))
# In-memory rate limit tracker: {phone_number: [timestamp, ...]}
_pstn_call_log: dict[str, list[float]] = {}
_pstn_call_log_lock = threading.Lock()

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

    def cleanup_all_rooms(self) -> None:
        """Delete all tracked Daily rooms. Called on server shutdown."""
        with self._lock:
            sessions = dict(self._sessions)
        for sid, sess in sessions.items():
            room_name = sess.get("room_name")
            if room_name and _DAILY_API_KEY:
                try:
                    delete_room(api_key=_DAILY_API_KEY, room_name=room_name)
                    print(f"[shutdown] deleted room {room_name} (session={sid[:8]})")
                except Exception as exc:
                    print(f"[shutdown] failed to delete room {room_name}: {exc}")
            proc = sess.get("process")
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.terminate()

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

    def mark_pstn_session(self, session_id: str, *, caller_number: str, call_id: str) -> None:
        """Mark a session as PSTN and attach call metadata."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["pstn_call"] = True
                sess["caller_number"] = caller_number
                sess["call_id"] = call_id

    def count_pstn_sessions(self) -> int:
        """Return the number of active PSTN sessions."""
        with self._lock:
            return sum(1 for sess in self._sessions.values() if sess.get("pstn_call"))

    def allocate_tune_port(self, session_id: str, *, base_port: int, span: int = 200) -> int | None:
        """Allocate a free tune port for ``session_id`` within ``[base_port, base_port+span)``."""
        with self._lock:
            used_ports = {
                sess.get("tune_port")
                for sess in self._sessions.values()
                if sess.get("tune_port") is not None
            }
            for port in range(base_port, base_port + span):
                if port not in used_ports:
                    sess = self._sessions.get(session_id)
                    if sess is None:
                        return None
                    sess["tune_port"] = port
                    return port
        return None

    def get_process(self, session_id: str) -> subprocess.Popen | None:
        """Return the subprocess handle for a session, if still tracked."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            return sess.get("process")


REGISTRY = BotSessionRegistry()


def _safe_delete_room(room_name: str, *, reason: str) -> None:
    """Best-effort Daily room deletion with explicit logging."""
    if not room_name or not _DAILY_API_KEY:
        return
    try:
        delete_room(api_key=_DAILY_API_KEY, room_name=room_name)
        print(f"[room-cleanup] deleted {room_name} ({reason})")
    except Exception as exc:
        print(f"[room-cleanup] failed deleting {room_name} ({reason}): {exc}")


def _prune_pstn_call_log(now_ts: float) -> None:
    """Prune expired timestamps and empty caller buckets from the PSTN limiter map."""
    window_start = now_ts - _PSTN_RATE_LIMIT_WINDOW_SECS
    stale_callers: list[str] = []
    for caller, timestamps in _pstn_call_log.items():
        kept = [ts for ts in timestamps if ts > window_start]
        if kept:
            _pstn_call_log[caller] = kept
        else:
            stale_callers.append(caller)
    for caller in stale_callers:
        _pstn_call_log.pop(caller, None)


def _cleanup_orphaned_rooms() -> None:
    """Delete any leftover vox-* rooms from previous server runs.

    Called once on startup. These rooms are orphaned when the server is
    killed without graceful shutdown (SIGKILL, crash, power loss).
    """
    if not _DAILY_DYNAMIC_ROOMS or not _DAILY_API_KEY:
        return
    try:
        rooms = list_rooms(api_key=_DAILY_API_KEY, prefix="vox-")
    except Exception as exc:
        print(f"[startup] could not list Daily rooms for cleanup: {exc}")
        return
    if not rooms:
        return
    print(f"[startup] found {len(rooms)} orphaned vox-* room(s), deleting...")
    for room_name in rooms:
        try:
            delete_room(api_key=_DAILY_API_KEY, room_name=room_name)
            print(f"[startup] deleted orphaned room {room_name}")
        except Exception as exc:
            print(f"[startup] failed to delete {room_name}: {exc}")


def _log_pstn_call(
    session_id: str,
    caller_number: str,
    called_number: str,
    call_id: str,
    room_name: str,
) -> None:
    """Write a PSTN call record to logs/calls/<session_id>/pstn_call.json."""
    calls_dir = Path(__file__).resolve().parent.parent / "logs" / "calls"
    session_dir = calls_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "type": "pstn_inbound",
        "session_id": session_id,
        "caller_number": caller_number,
        "called_number": called_number,
        "call_id": call_id,
        "room_name": room_name,
        "max_duration_min": _PSTN_MAX_DURATION_MIN,
    }
    out_path = session_dir / "pstn_call.json"
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def _spawn_bot(
    session_id: str,
    callback_url: str,
    tune_port: int,
    llm_model: str | None = None,
    room_name: str | None = None,
    stt_provider: str | None = None,
    dialin_call_id: str | None = None,
    dialin_call_domain: str | None = None,
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
    if stt_provider:
        env["STT_PROVIDER"] = stt_provider
    if dialin_call_id:
        env["DIALIN_CALL_ID"] = dialin_call_id
    if dialin_call_domain:
        env["DIALIN_CALL_DOMAIN"] = dialin_call_domain
    if dialin_call_id:
        # Timestamp when webhook received the call — used by the bot to
        # compute hold time (caller waiting for bot to be ready).
        env["PSTN_WEBHOOK_TS"] = f"{time.time():.3f}"

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

# Ticket sink for the HTTP chat path (/api/chat). Post to the same Telegram
# channel the voice bot uses when the bot token is configured; otherwise fall
# back to the terminal-only LoggingSink so the demo still works without creds.
try:
    _ticket_sink = TelegramSink(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        channel_id=os.environ.get("TELEGRAM_CHANNEL_ID", "") or _hotel_config.telegram_channel_id,
    )
    print(f"[actions] /api/chat tickets → Telegram channel {_hotel_config.telegram_channel_id}")
except ValueError:
    _ticket_sink = LoggingSink()
    print("[actions] TELEGRAM_BOT_TOKEN not set — /api/chat tickets go to the terminal only")

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
_product_turn_counters: dict[str, int] = {}


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
    """Execute a create_ticket tool call via the ticket sink and return result JSON.

    OpenAI function-calling does not hard-enforce the schema enum, so invented
    categories ("Room Service") can arrive here. No alias lists: anything that
    isn't an allowed category (matched case-insensitively against the enum's
    own labels) is rejected WITH the valid list, and the LLM maps its invented
    label to the closest valid one itself on the follow-up call.
    """
    import asyncio

    raw_cat = str(args.get("category", "")).strip()
    category = next((c for c in Category if c.value.lower() == raw_cat.lower()), None)
    if category is not None and category not in _hotel_config.allowed_categories:
        category = None
    if category is None:
        valid = ", ".join(c.value for c in _hotel_config.allowed_categories)
        return json.dumps(
            {
                "status": "rejected",
                "reason": (
                    f"Invalid category: {raw_cat!r}. Valid categories: {valid}. "
                    "Call create_ticket again once with the closest valid category."
                ),
            }
        )

    ticket = Ticket(
        category=category,
        summary=args.get("summary", ""),
        room_number=args.get("room_number", ""),
        original_quote=args.get("original_quote", ""),
        language_detected=args.get("language_detected", ""),
    )

    loop = asyncio.new_event_loop()
    ok = loop.run_until_complete(_ticket_sink.send(ticket))
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
    if language and language != "auto":
        messages.append(
            {
                "role": "system",
                "content": (
                    f"IMPORTANT: You MUST reply in language code '{language}'. "
                    f"Do not switch to another language even if the input is ambiguous. "
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
        # Second LLM call to get the final spoken reply. Tools stay available
        # so the model can fix a rejected call — bounded to one retry round.
        response2 = client.chat.completions.create(
            model=model,
            max_tokens=512,
            messages=messages,
            tools=_TOOLS or None,
            tool_choice="auto" if _TOOLS else None,
        )
        msg2 = response2.choices[0].message
        if msg2.tool_calls:
            messages.append(msg2.model_dump())
            for tc2 in msg2.tool_calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc2.id,
                        "content": _handle_tool_call(tc2, session_id),
                    }
                )
            # Final pass without tools so this can never loop.
            response3 = client.chat.completions.create(
                model=model, max_tokens=512, messages=messages
            )
            reply = response3.choices[0].message.content or ""
        else:
            reply = msg2.content or ""
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


# ---------------------------------------------------------------------------
# Inquiry form — rate limiting + email config
# ---------------------------------------------------------------------------
_INQUIRY_DIR = Path(__file__).resolve().parent.parent / "logs" / "inquiries"
_INQUIRY_DIR.mkdir(parents=True, exist_ok=True)

_ZOHO_SMTP_USER: str | None = os.environ.get("ZOHO_SMTP_USER") or None
_ZOHO_SMTP_PASS: str | None = os.environ.get("ZOHO_SMTP_PASS") or None
_INQUIRY_NOTIFY_EMAIL: str = os.environ.get("INQUIRY_NOTIFY_EMAIL", "hello@voxtera.ai")

# Simple per-IP rate limiter: max 3 submissions per hour
_INQUIRY_RATE_LIMIT = 3
_INQUIRY_RATE_WINDOW = 3600  # seconds
_inquiry_rate_map: dict[str, list[float]] = {}
_inquiry_rate_lock = threading.Lock()


def _inquiry_rate_ok(ip: str) -> bool:
    """Return True if this IP hasn't exceeded the submission rate limit."""
    now = time.time()
    with _inquiry_rate_lock:
        timestamps = _inquiry_rate_map.get(ip, [])
        # Prune old entries
        timestamps = [t for t in timestamps if now - t < _INQUIRY_RATE_WINDOW]
        if len(timestamps) >= _INQUIRY_RATE_LIMIT:
            _inquiry_rate_map[ip] = timestamps
            return False
        timestamps.append(now)
        _inquiry_rate_map[ip] = timestamps
        return True


def _send_inquiry_email(payload: dict) -> None:
    """Send inquiry notification via Zoho SMTP. Runs in a thread."""
    if not _ZOHO_SMTP_USER or not _ZOHO_SMTP_PASS:
        return
    import email.mime.text
    import smtplib

    prop = payload.get("property_name", "Unknown")
    loc = payload.get("location", "?")
    subject = f"New Voxtera inquiry: {prop} ({loc})"
    lines = [
        f"Property: {payload.get('property_name')} — {payload.get('location')}",
        f"Size: {payload.get('size', '—')} keys | Type: {payload.get('property_type', '—')}",
        "",
        f"Languages: {', '.join(payload.get('languages', []))}",
        f"Pain: {payload.get('pain', '—')}",
        f"PMS: {payload.get('pms', '—')}",
        f"Channels: {', '.join(payload.get('channels', []))}",
        "",
        f"Name: {payload.get('name')}",
        f"Role: {payload.get('role', '—')}",
        f"Email: {payload.get('email')}",
        f"Next step: {payload.get('next_step', '—')}",
        "",
        f"Submitted: {payload.get('submitted_at', '—')}",
        f"IP: {payload.get('_ip', '—')}",
    ]
    body = "\n".join(lines)

    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = _ZOHO_SMTP_USER
    msg["To"] = _INQUIRY_NOTIFY_EMAIL

    try:
        with smtplib.SMTP("smtp.zoho.com", 587, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(_ZOHO_SMTP_USER, _ZOHO_SMTP_PASS)
            smtp.send_message(msg)
    except Exception as exc:
        sys.stderr.write(f"[inquiry] email send failed: {exc}\n")


# ---------------------------------------------------------------------------
# Demo access gate — HMAC-signed tokens with expiry
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402
import hmac  # noqa: E402

_DEMO_SECRET: str = os.environ.get("VOXTERA_DEMO_SECRET", "dev-demo-secret-change-me")
_DEMO_TOKEN_TTL_DAYS: int = int(os.environ.get("VOXTERA_DEMO_TOKEN_DAYS", "7"))
_DEMO_FREE_MESSAGES: int = 3  # anonymous messages allowed per IP per 24h
_DEMO_FREE_WINDOW: int = 86400  # 24 hours in seconds

# Per-IP anonymous message counter
_demo_anon_map: dict[str, list[float]] = {}
_demo_anon_lock = threading.Lock()


def _demo_anon_ok(ip: str) -> bool:
    """Return True if IP hasn't exceeded anonymous free message limit."""
    now = time.time()
    with _demo_anon_lock:
        timestamps = _demo_anon_map.get(ip, [])
        timestamps = [t for t in timestamps if now - t < _DEMO_FREE_WINDOW]
        if len(timestamps) >= _DEMO_FREE_MESSAGES:
            _demo_anon_map[ip] = timestamps
            return False
        timestamps.append(now)
        _demo_anon_map[ip] = timestamps
        return True


def _demo_anon_count(ip: str) -> int:
    """Return how many anonymous messages this IP has used in the window."""
    now = time.time()
    with _demo_anon_lock:
        timestamps = _demo_anon_map.get(ip, [])
        return len([t for t in timestamps if now - t < _DEMO_FREE_WINDOW])


def _generate_demo_token(email: str) -> str:
    """Generate an HMAC-signed demo access token."""
    exp = int(time.time()) + (_DEMO_TOKEN_TTL_DAYS * 86400)
    payload = json.dumps({"email": email, "exp": exp}, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(_DEMO_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload_b64}.{sig}"


def _validate_demo_token(token: str) -> dict | None:
    """Validate token. Returns payload dict if valid, None otherwise."""
    if not token or "." not in token:
        return None
    parts = token.rsplit(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig = parts
    expected_sig = hmac.new(
        _DEMO_SECRET.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# Per-email rate limit for code requests: max 2 per email per 24h
_demo_request_map: dict[str, list[float]] = {}
_demo_request_lock = threading.Lock()

# Predefined demo codes file (one code per line, # comments allowed)
_DEMO_CODES_FILE: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_codes.txt")
_DEMO_CODE_LOG_FILE: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs", "audit", "demo-code-usage.jsonl"
)


def _log_demo_code_usage(ip: str, user_agent: str, code: str, result: str, email: str) -> None:
    """Append a line to the demo code usage audit log."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "ip": ip,
        "ua": user_agent,
        "code": code if result != "invalid" else code[:4] + "***",
        "result": result,
        "email": email,
    }
    try:
        os.makedirs(os.path.dirname(_DEMO_CODE_LOG_FILE), exist_ok=True)
        with open(_DEMO_CODE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _load_demo_codes() -> set[str]:
    """Load predefined codes from demo_codes.txt (re-reads each call for hot-reload)."""
    codes: set[str] = set()
    try:
        with open(_DEMO_CODES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    codes.add(line)
    except FileNotFoundError:
        pass
    return codes


def _demo_request_rate_ok(email: str) -> bool:
    """Return True if this email hasn't exceeded code request rate limit."""
    now = time.time()
    key = email.lower().strip()
    with _demo_request_lock:
        timestamps = _demo_request_map.get(key, [])
        timestamps = [t for t in timestamps if now - t < 86400]
        if len(timestamps) >= 2:
            _demo_request_map[key] = timestamps
            return False
        timestamps.append(now)
        _demo_request_map[key] = timestamps
        return True


def _send_demo_code_email(name: str, email: str, token: str) -> None:
    """Send demo access code via Zoho SMTP. Runs in a daemon thread."""
    import email.mime.multipart as _mp
    import email.mime.text as _mt
    import smtplib

    if not _ZOHO_SMTP_USER or not _ZOHO_SMTP_PASS:
        sys.stderr.write("[demo-gate] SMTP not configured, skipping email\n")
        return

    subject = "Your Voxtera demo access code"
    body_text = (
        f"Hi {name},\n\n"
        f"Here's your Voxtera demo access code:\n\n"
        f"    {token}\n\n"
        f"Paste it into the demo page to unlock full voice + text access "
        f"for {_DEMO_TOKEN_TTL_DAYS} days.\n\n"
        f"Questions? Just reply to this email.\n\n"
        f"— The Voxtera team"
    )
    body_html = (
        f"<p>Hi {name},</p>"
        f"<p>Here's your Voxtera demo access code:</p>"
        f"<pre style='background:#f4f0e8;padding:12px 16px;border-radius:8px;"
        f"font-family:monospace;font-size:14px;word-break:break-all'>{token}</pre>"
        f"<p>Paste it into the demo page to unlock full voice + text access "
        f"for {_DEMO_TOKEN_TTL_DAYS} days.</p>"
        f"<p>Questions? Just reply to this email.</p>"
        f"<p>— The Voxtera team</p>"
    )

    msg = _mp.MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _ZOHO_SMTP_USER
    msg["To"] = email
    msg["Reply-To"] = _INQUIRY_NOTIFY_EMAIL
    msg.attach(_mt.MIMEText(body_text, "plain"))
    msg.attach(_mt.MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP("smtp.zoho.com", 587, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(_ZOHO_SMTP_USER, _ZOHO_SMTP_PASS)
            smtp.send_message(msg)
        sys.stderr.write(f"[demo-gate] code sent to {email}\n")
    except Exception as exc:
        sys.stderr.write(f"[demo-gate] email send failed: {exc}\n")


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
        if self.path == "/api/stt-providers":
            return self._handle_stt_providers()
        if self.path == "/api/admin/health":
            return self._handle_admin_health()
        if self.path == "/api/admin/verify":
            return self._handle_admin_verify()
        if self.path == "/api/admin/config":
            return self._handle_admin_config_get()
        if self.path == "/api/admin/sessions":
            return self._handle_admin_sessions()
        if self.path == "/api/admin/rooms":
            return self._handle_admin_rooms()
        if self.path == "/api/admin/prompts":
            return self._handle_admin_prompts_list()
        if self.path == "/api/admin/greetings":
            return self._handle_admin_greetings_get()
        if self.path == "/api/admin/voice-prompts":
            return self._handle_admin_voice_prompts_list()
        if self.path == "/api/admin/call-center/status":
            return self._handle_admin_call_center_status()
        if self.path.startswith("/api/admin/call-center/es/hotels"):
            return self._handle_admin_call_center_es_hotels()
        if self.path.startswith("/api/admin/call-center/es/search"):
            return self._handle_admin_call_center_es_search()
        if self.path.startswith("/api/admin/call-center/qdrant/collections"):
            return self._handle_admin_call_center_qdrant_collections()
        if self.path.startswith("/api/admin/call-center/qdrant/points"):
            return self._handle_admin_call_center_qdrant_points()
        if self.path.startswith("/api/admin/calls"):
            return self._handle_admin_calls()
        if "/audio/" in self.path and self.path.startswith("/api/admin/call/"):
            # /api/admin/call/<session_id>/audio/<filename>
            parts = self.path[len("/api/admin/call/") :].split("/audio/", 1)
            if len(parts) == 2:
                return self._handle_admin_call_audio(parts[0], parts[1])
        if self.path.startswith("/api/admin/call/"):
            session_id = self.path[len("/api/admin/call/") :]
            return self._handle_admin_call_detail(session_id)
        if self.path.startswith("/api/admin/visitors"):
            return self._handle_admin_visitors()
        if self.path.startswith("/api/admin/ip-lookup"):
            return self._handle_admin_ip_lookup()
        if self.path == "/api/trace/snapshot":
            return self._handle_trace_snapshot()
        if self.path == "/api/trace/stream":
            return self._handle_trace_stream()
        if self.path == "/api/trace/sessions":
            return self._handle_list_sessions()
        if self.path.startswith("/api/audit/sessions"):
            return self._handle_audit_sessions()
        if self.path.startswith("/api/audit/session/"):
            filename = self.path[len("/api/audit/session/") :]
            return self._handle_audit_session_detail(filename)
        if self.path.startswith("/api/rag/chunks"):
            return self._handle_rag_chunks()
        if self.path.startswith("/api/admin/concierge-logs"):
            return self._handle_concierge_logs()
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
            ".json",
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
        if self.path == "/api/concierge":
            return self._handle_concierge()
        if self.path == "/api/concierge/replay":
            return self._handle_concierge_replay()
        if self.path == "/api/concierge/feedback":
            return self._handle_concierge_feedback()
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
        if self.path == "/api/admin/prompts":
            return self._handle_admin_prompts_save()
        if self.path == "/api/admin/greetings":
            return self._handle_admin_greetings_post()
        if self.path == "/api/admin/voice-prompts":
            return self._handle_admin_voice_prompts_save()
        if self.path == "/api/admin/call-center/qdrant/search":
            return self._handle_admin_call_center_qdrant_search()
        # Phase 3 — on-demand bot launcher
        if self.path == "/api/start-session":
            return self._handle_start_session()
        if self.path == "/api/end-session":
            return self._handle_end_session()
        if self.path == "/api/bot-event":
            return self._handle_bot_event()
        if self.path == "/api/inquiry":
            return self._handle_inquiry()
        if self.path == "/api/demo/request-code":
            return self._handle_demo_request_code()
        if self.path == "/api/demo/validate-code":
            return self._handle_demo_validate_code()
        if self.path == "/api/demo/check-allowance":
            return self._handle_demo_check_allowance()
        if self.path == "/api/audio-device-info":
            return self._handle_audio_device_info()
        if self.path == "/pstn/webhook":
            return self._handle_pstn_webhook()
        self.send_error(404)
        return None

    def do_DELETE(self):  # noqa: N802
        if self.path.startswith("/api/admin/greetings/"):
            lang = self.path[len("/api/admin/greetings/") :].split("?", 1)[0]
            return self._handle_admin_greetings_delete(lang)
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

    def _send_upstream_error(self, service: str, status: int, payload: object) -> None:
        detail = payload if isinstance(payload, dict) else {"detail": payload}
        self._send_json(
            status if 400 <= status <= 599 else 502,
            {"error": f"{service}_request_failed", "detail": detail},
        )

    # ------------------------------------------------------------------
    # Admin auth + helpers
    # ------------------------------------------------------------------

    def _admin_auth(self, *, require_daily: bool = True) -> tuple[bool, dict | None]:
        """Return (ok, error_response) for the current request.

        Centralised so every admin endpoint enforces the same gate. The
        precedence is intentional: if the *server* is misconfigured (no
        token, no Daily key) we report 503 — that's a deployment problem
        the operator needs to see, not "wrong password". Only when the
        server is healthy do we check the operator's token (401). Some
        admin pages do not depend on Daily; those call this with
        require_daily=False.
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
        if require_daily and (
            not _DAILY_API_KEY or (not _DAILY_ROOM_NAME and not _DAILY_DYNAMIC_ROOMS)
        ):
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
    # /api/admin/verify — lightweight token check for the admin portal
    # ------------------------------------------------------------------

    def _handle_admin_verify(self) -> None:
        """GET /api/admin/verify — returns 200 if token is valid, 401 otherwise."""
        if not _ADMIN_TOKEN:
            self._send_json(
                503, {"error": "admin_disabled", "detail": "VOXTERA_ADMIN_TOKEN is not set."}
            )
            return
        provided = self.headers.get("X-Admin-Token", "")
        if not provided or provided != _ADMIN_TOKEN:
            self._send_json(401, {"error": "unauthorized"})
            return
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # /api/admin/visitors — parsed Caddy access log with enrichment
    # ------------------------------------------------------------------

    def _handle_admin_visitors(self) -> None:
        """GET /api/admin/visitors — returns parsed access log entries."""
        ok, _ = self._admin_auth()
        if not ok:
            return

        from urllib.parse import parse_qs, urlparse

        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)
        last_n = int(params.get("last", ["500"])[0])
        summary = "summary" in params
        no_bots = "no-bots" in params

        log_path = "/var/log/caddy/voxtera-access.log"
        if not os.path.isfile(log_path):
            self._send_json(404, {"error": "log_not_found", "path": log_path})
            return

        bot_keywords = [
            "bot",
            "crawler",
            "spider",
            "gptbot",
            "oai-searchbot",
            "googlebot",
            "bingbot",
            "yandex",
            "semrush",
            "ahref",
            "mj12bot",
            "dotbot",
            "bytespider",
            "petalbot",
        ]

        # Paths that only scanners/bots probe — never a real human visitor
        scanner_paths = [
            ".env",
            ".git/",
            ".aws/",
            ".boto",
            ".cargo/credentials",
            ".circleci/",
            ".vscode/sftp",
            ".DS_Store",
            "wp-includes/",
            "wp-login",
            "wp-admin",
            "phpmyadmin",
            "actuator/",
            "docker-compose",
            ".docker",
            "credentials",
            ".ssh/",
            "/.well-known/security.txt",
            "admin.zip",
            "backup.zip",
            "phish.zip",
            "unzip.php",
            "phpinfo",
        ]

        # First pass: collect all entries and track suspicious IPs
        scanner_ips: set = set()
        raw_entries = []
        with open(log_path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                request = entry.get("request", {})
                ip = request.get("remote_ip", "?")
                method = request.get("method", "?")
                uri = request.get("uri", "?")
                status = entry.get("status", 0)
                ts = entry.get("ts", 0)
                headers = request.get("headers", {})
                ua_list = headers.get("User-Agent", [])
                user_agent = ua_list[0] if ua_list else ""

                is_bot = any(b in user_agent.lower() for b in bot_keywords)

                # Detect scanner behaviour by path probing
                uri_lower = uri.lower()
                if any(p in uri_lower for p in scanner_paths):
                    scanner_ips.add(ip)

                raw_entries.append(
                    {
                        "ip": ip,
                        "ts": ts,
                        "method": method,
                        "uri": uri,
                        "status": status,
                        "user_agent": user_agent,
                        "is_bot": is_bot,
                    }
                )

        # Second pass: mark scanner IPs as bots and apply filter
        entries = []
        for e in raw_entries:
            if e["ip"] in scanner_ips:
                e["is_bot"] = True
            if no_bots and e["is_bot"]:
                continue
            entries.append(e)

        if summary:
            ip_data: dict = {}
            for e in entries:
                ip = e["ip"]
                if ip not in ip_data:
                    ip_data[ip] = {
                        "ip": ip,
                        "hits": 0,
                        "pages": set(),
                        "first_seen": e["ts"],
                        "last_seen": e["ts"],
                        "user_agent": e["user_agent"],
                        "is_bot": e["is_bot"],
                    }
                ip_data[ip]["hits"] += 1
                ip_data[ip]["pages"].add(e["uri"])
                ip_data[ip]["last_seen"] = e["ts"]
            result = []
            for data in sorted(ip_data.values(), key=lambda x: x["hits"], reverse=True):
                data["pages"] = sorted(data["pages"])[:20]
                result.append(data)
            self._send_json(200, {"visitors": result[:200]})
        else:
            self._send_json(200, {"entries": entries[-last_n:]})

    # ------------------------------------------------------------------
    # /api/admin/ip-lookup — proxy IP geolocation to avoid mixed content
    # ------------------------------------------------------------------

    def _handle_admin_ip_lookup(self) -> None:
        """GET /api/admin/ip-lookup?ip=x.x.x.x — proxy to ip-api.com."""
        ok, _ = self._admin_auth()
        if not ok:
            return

        import urllib.request
        from urllib.parse import parse_qs, urlparse

        parsed_url = urlparse(self.path)
        params = parse_qs(parsed_url.query)
        ip = params.get("ip", [""])[0].strip()

        # Basic validation: only allow IP-like strings
        import re

        if not ip or not re.match(r"^[\d.:a-fA-F]+$", ip):
            self._send_json(400, {"error": "invalid_ip"})
            return

        fields = (
            "status,country,countryCode,city,regionName,region,zip,lat,lon,"
            "timezone,offset,isp,org,as,asname,reverse,mobile,proxy,hosting,query"
        )
        url = f"http://ip-api.com/json/{ip}?fields={fields}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Voxtera-Admin/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
            self._send_json(200, data)
        except Exception:
            self._send_json(502, {"error": "lookup_failed"})

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
    # /api/admin/rooms — list all vox-* rooms with participant counts
    # ------------------------------------------------------------------

    def _handle_admin_rooms(self) -> None:
        """GET /api/admin/rooms — all vox-* rooms with live participant counts."""
        ok, _ = self._admin_auth()
        if not ok:
            return

        if not _DAILY_API_KEY or not _DAILY_DOMAIN:
            self._send_json(503, {"error": "daily_not_configured"})
            return

        try:
            rooms = list_rooms_with_presence(
                api_key=_DAILY_API_KEY,
                domain=_DAILY_DOMAIN,
                prefix="vox-",
            )
        except DailyAPIError as exc:
            self._send_json(
                502,
                {"error": "daily_api_error", "detail": str(exc), "status": exc.status},
            )
            return

        self._send_json(
            200,
            {
                "domain": _DAILY_DOMAIN,
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "rooms": rooms,
                "room_count": len(rooms),
            },
        )

    # ------------------------------------------------------------------
    # /api/admin/calls — browse call records from logs/calls/
    # ------------------------------------------------------------------

    def _handle_admin_calls(self) -> None:
        """GET /api/admin/calls — list all call records (summary view)."""
        ok, _ = self._admin_auth()
        if not ok:
            return

        calls_dir = Path(__file__).resolve().parent.parent / "logs" / "calls"
        if not calls_dir.exists():
            self._send_json(200, {"calls": [], "total": 0})
            return

        results = []
        for session_dir in sorted(calls_dir.iterdir(), reverse=True):
            if not session_dir.is_dir():
                continue
            record_file = session_dir / "record.json"
            if not record_file.exists():
                continue
            try:
                record = json.loads(record_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            results.append(
                {
                    "session_id": record.get("session_id", session_dir.name),
                    "hotel_id": record.get("hotel_id", ""),
                    "started_at": record.get("started_at", ""),
                    "ended_at": record.get("ended_at"),
                    "duration_secs": record.get("duration_secs"),
                    "transport_mode": record.get("transport_mode", ""),
                    "providers": record.get("providers", {}),
                    "languages": record.get("languages", []),
                    "metrics": record.get("metrics", {}),
                    "has_audio": record.get("audio") is not None,
                    "turn_count": len(record.get("turns", [])),
                }
            )

        # Sort by started_at descending
        results.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        self._send_json(200, {"calls": results, "total": len(results)})

    def _handle_admin_call_detail(self, session_id: str) -> None:
        """GET /api/admin/call/<session_id> — full call record with turns."""
        ok, _ = self._admin_auth()
        if not ok:
            return

        # Security: prevent path traversal
        if "/" in session_id or "\\" in session_id or ".." in session_id:
            self._send_json(400, {"error": "invalid session_id"})
            return

        calls_dir = Path(__file__).resolve().parent.parent / "logs" / "calls"
        record_file = calls_dir / session_id / "record.json"

        if not record_file.exists():
            self._send_json(404, {"error": "call not found"})
            return

        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._send_json(500, {"error": f"failed to read record: {exc}"})
            return

        # Enrich with available audio files on disk
        session_dir = calls_dir / session_id
        audio_files = []
        for fname in ("recording.wav", "input_raw.wav"):
            fpath = session_dir / fname
            if fpath.exists():
                audio_files.append({"file": fname, "size_bytes": fpath.stat().st_size})
        record["audio_files"] = audio_files

        self._send_json(200, record)

    def _handle_admin_call_audio(self, session_id: str, filename: str) -> None:
        """GET /api/admin/call/<session_id>/audio/<filename> — serve a WAV file.

        Accepts auth via X-Admin-Token header OR ?token= query param
        (needed because <audio> elements cannot set custom headers).
        """
        # Check header first, then query param
        from urllib.parse import parse_qs, urlparse

        provided = self.headers.get("X-Admin-Token", "")
        if not provided:
            qs = parse_qs(urlparse(self.path).query)
            provided = (qs.get("token") or [""])[0]
        if not _ADMIN_TOKEN or provided != _ADMIN_TOKEN:
            self._send_json(401, {"error": "unauthorized"})
            return

        # Strip query string from filename if present
        if "?" in filename:
            filename = filename.split("?")[0]

        # Security: prevent path traversal
        if "/" in session_id or "\\" in session_id or ".." in session_id:
            self._send_json(400, {"error": "invalid session_id"})
            return
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(400, {"error": "invalid filename"})
            return
        # Only allow known audio filenames
        allowed = {"recording.wav", "input_raw.wav"}
        if filename not in allowed:
            self._send_json(403, {"error": "file not allowed"})
            return

        calls_dir = Path(__file__).resolve().parent.parent / "logs" / "calls"
        audio_path = calls_dir / session_id / filename

        if not audio_path.exists():
            self._send_json(404, {"error": "audio file not found"})
            return

        try:
            data = audio_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)
        except OSError as exc:
            self._send_json(500, {"error": f"failed to read audio: {exc}"})

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
        # Demo access gate — voice always requires a valid token
        # ------------------------------------------------------------------
        try:
            length = int(self.headers.get("Content-Length", 0))
            start_body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            start_body = {}

        demo_token = (start_body.get("demo_token") or "").strip()
        if not _validate_demo_token(demo_token) and demo_token not in _load_demo_codes():
            self._send_json(
                403,
                {
                    "error": "demo_token_required",
                    "detail": "Voice calls require an access code. Request one for free.",
                },
            )
            return

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
        body: dict = start_body
        llm_model = body.get("llm") or None
        stt_provider = body.get("stt") or None

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
                stt_provider=stt_provider,
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
    # Inquiry form endpoint — POST /api/inquiry
    # ------------------------------------------------------------------

    _INQUIRY_MAX_BODY = 8192  # 8 KB max request body
    _INQUIRY_VALID_PAINS = {
        "after_hours",
        "missed_bookings",
        "language_gap",
        "request_chaos",
        "repetitive",
        "other",
    }
    _INQUIRY_VALID_STEPS = {"demo", "info_pack", "pricing", "pilot"}

    # ------------------------------------------------------------------
    # Demo access gate endpoints
    # ------------------------------------------------------------------

    def _handle_demo_request_code(self) -> None:
        """POST /api/demo/request-code — request a demo access code via email."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 4096 or content_length <= 0:
            self._send_json(400, {"error": "invalid_body"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid_json"})
            return

        name = (body.get("name") or "").strip()[:100]
        email = (body.get("email") or "").strip()[:200]
        company = (body.get("company") or "").strip()[:200]

        if not name or len(name) < 2:
            self._send_json(400, {"error": "name_required"})
            return
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            self._send_json(400, {"error": "invalid_email"})
            return

        # Rate limit per email
        if not _demo_request_rate_ok(email):
            self._send_json(
                429, {"error": "rate_limited", "detail": "Code already sent. Check your inbox."}
            )
            return

        # Generate token and send
        token = _generate_demo_token(email)

        # Log the request
        log_entry = {
            "ts": datetime.now(UTC).isoformat(),
            "name": name,
            "email": email,
            "company": company,
            "ip": (self._client_ip()[1] or self._client_ip()[0]),
        }
        sys.stderr.write(f"[demo-gate] code requested: {json.dumps(log_entry)}\n")

        # Send email in background thread
        t = threading.Thread(target=_send_demo_code_email, args=(name, email, token), daemon=True)
        t.start()

        self._send_json(200, {"ok": True, "detail": "Code sent to your email."})

    def _handle_demo_validate_code(self) -> None:
        """POST /api/demo/validate-code — validate a demo access code."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 2048 or content_length <= 0:
            self._send_json(400, {"error": "invalid_body"})
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid_json"})
            return

        token = (body.get("code") or "").strip()
        direct_ip, fwd_ip = self._client_ip()
        client_ip = fwd_ip or direct_ip
        user_agent = self.headers.get("User-Agent", "")

        # Check HMAC-signed token first
        payload = _validate_demo_token(token)
        if payload:
            _log_demo_code_usage(
                client_ip, user_agent, token, "email_token", payload.get("email", "")
            )
            self._send_json(
                200,
                {
                    "valid": True,
                    "email": payload.get("email", ""),
                    "expires": payload.get("exp", 0),
                },
            )
            return
        # Check predefined codes file
        if token and token in _load_demo_codes():
            _log_demo_code_usage(client_ip, user_agent, token, "predefined_code", "tester")
            self._send_json(200, {"valid": True, "email": "tester", "expires": 0})
            return
        _log_demo_code_usage(client_ip, user_agent, token, "invalid", "")
        self._send_json(401, {"valid": False, "error": "invalid_or_expired"})

    def _handle_demo_check_allowance(self) -> None:
        """POST /api/demo/check-allowance — check remaining free messages for this IP."""
        direct_ip, fwd_ip = self._client_ip()
        client_ip = fwd_ip or direct_ip
        used = _demo_anon_count(client_ip)
        remaining = max(0, _DEMO_FREE_MESSAGES - used)
        self._send_json(200, {"remaining": remaining, "limit": _DEMO_FREE_MESSAGES})

    def _handle_audio_device_info(self) -> None:
        """POST /api/audio-device-info — save client audio track settings.

        Expects JSON with session_id and various audio diagnostic fields.
        Writes to logs/calls/<session_id>/audio_device.json.
        """
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0 or content_length > 8192:
            self._send_json(400, {"error": "invalid_body"})
            return
        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return
        session_id = body.get("session_id")
        if not session_id:
            self._send_json(400, {"error": "missing session_id"})
            return
        # Sanitise session_id to prevent path traversal
        safe_id = session_id.replace("/", "").replace("..", "").replace("\\", "")
        if not safe_id or safe_id != session_id:
            self._send_json(400, {"error": "invalid session_id"})
            return
        calls_dir = Path(__file__).resolve().parent.parent / "logs" / "calls"
        session_dir = calls_dir / safe_id
        session_dir.mkdir(parents=True, exist_ok=True)
        out_path = session_dir / "audio_device.json"
        payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "session_id": safe_id,
            "user_agent": self.headers.get("User-Agent", ""),
            "platform": body.get("platform"),
            "requested_constraints": body.get("requested_constraints"),
            "audio_track_settings": body.get("audio_track_settings"),
            "audio_track_capabilities": body.get("audio_track_capabilities"),
            "available_devices": body.get("available_devices"),
            "network": body.get("network"),
        }
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        self._send_json(200, {"ok": True})

    # ------------------------------------------------------------------
    # PSTN Telephony — pinless dial-in webhook
    # ------------------------------------------------------------------

    def _handle_pstn_webhook(self) -> None:
        """POST /pstn/webhook — Daily calls this when a PSTN call arrives.

        Daily's pinless dial-in flow:
        1. Caller dials our number → Daily places them on hold (music plays).
        2. Daily POSTs here with {From, To, callId, callDomain}.
        3. We create a VCI-prefixed room, spawn a bot into it.
        4. Bot joins, Pipecat fires 'dialin-ready', auto-calls pinlessCallUpdate.
        5. Daily patches the held call into the room → conversation starts.
        """
        if not _PSTN_ENABLED:
            self._send_json(503, {"error": "pstn_disabled"})
            return

        if not _DAILY_API_KEY:
            self._send_json(503, {"error": "daily_api_key_missing"})
            return

        # --- Read and validate body ---
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0 or content_length > 4096:
            self._send_json(400, {"error": "invalid_body"})
            return
        try:
            raw_body = self.rfile.read(content_length)
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return

        # --- HMAC signature verification ---
        # Proves the request genuinely came from Daily and wasn't forged or
        # replayed. Daily signs ``"{timestamp}.{body}"`` with a base64 HMAC-SHA256
        # secret and sends X-Pinless-Signature / X-Pinless-Timestamp. See
        # https://docs.daily.co/guides/products/dial-in-dial-out/dialin-pinless
        if _PSTN_HMAC_VERIFY:
            pinless_signature = self.headers.get("X-Pinless-Signature", "")
            pinless_timestamp = self.headers.get("X-Pinless-Timestamp", "")
            err = verify_pinless_signature(
                raw_body,
                pinless_signature,
                pinless_timestamp,
                _PSTN_WEBHOOK_HMAC,
                tolerance_secs=_PSTN_WEBHOOK_TOLERANCE_SECS,
            )
            if err is not None:
                # 503 for our-side misconfig, 401 for a bad/forged/stale request.
                status = 503 if err in ("hmac_not_configured", "hmac_misconfigured") else 401
                x_headers = sorted(
                    header_name
                    for header_name in self.headers
                    if header_name.lower().startswith("x-")
                )
                print(
                    "[pstn] webhook rejected: "
                    f"{err} "
                    f"(sig_present={bool(pinless_signature)} sig_len={len(pinless_signature)} "
                    f"ts={pinless_timestamp!r} x_headers={x_headers} body_len={len(raw_body)})",
                    flush=True,
                )
                self._send_json(status, {"error": err})
                return

        # --- Handle test probe (Daily sends {To: ...} to verify endpoint) ---
        if "callId" not in body:
            print(f"[pstn] received test probe from Daily: {body}")
            self._send_json(200, {"ok": True})
            return

        call_id = body.get("callId", "")
        call_domain = body.get("callDomain", "")
        caller_number = body.get("From", "unknown")
        called_number = body.get("To", "")

        print(
            f"[pstn] incoming call: from={caller_number} to={called_number} "
            f"callId={call_id[:12]}..."
        )

        # --- Rate limit: per-number call frequency ---
        _now_ts = time.time()
        with _pstn_call_log_lock:
            _prune_pstn_call_log(_now_ts)
            recent_calls = _pstn_call_log.get(caller_number, [])
            if len(recent_calls) >= _PSTN_RATE_LIMIT_PER_NUMBER:
                print(
                    f"[pstn] rejected: caller {caller_number} exceeded rate limit "
                    f"({_PSTN_RATE_LIMIT_PER_NUMBER} calls / {_PSTN_RATE_LIMIT_WINDOW_SECS}s)"
                )
                self._send_json(429, {"error": "rate_limit_exceeded"})
                return
            recent_calls.append(_now_ts)
            _pstn_call_log[caller_number] = recent_calls

        # --- Rate limit: max concurrent PSTN calls ---
        if REGISTRY.count_pstn_sessions() >= _PSTN_MAX_CONCURRENT:
            print(f"[pstn] rejected: max concurrent calls ({_PSTN_MAX_CONCURRENT}) reached")
            self._send_json(503, {"error": "max_concurrent_calls"})
            return

        # --- Create VCI-prefixed room ---
        now = datetime.now(tz=UTC)
        random_suffix = uuid.uuid4().hex[:4]
        room_name = f"VCI-{now.strftime('%Y%m%d')}-{now.strftime('%H%M%S')}-{random_suffix}"

        try:
            create_room(
                api_key=_DAILY_API_KEY,
                room_name=room_name,
                expiry_secs=(_PSTN_MAX_DURATION_MIN + 1) * 60,
                max_participants=2,
                sip_mode="dial-in",
            )
            print(f"[pstn] created room {room_name} (sip_mode=dial-in)")
        except DailyAPIError as exc:
            print(f"[pstn] room creation failed: {exc}")
            self._send_json(502, {"error": f"room_creation_failed: {exc}"})
            return

        # --- Spawn bot with dialin settings ---
        session_id = uuid.uuid4().hex
        callback_url = f"{LAUNCHER_BASE_URL}/api/bot-event"

        try:
            q = REGISTRY.start(session_id)
        except BotSessionBusyError as exc:
            print(f"[pstn] session registry busy: {exc}")
            _safe_delete_room(room_name, reason="registry_busy")
            self._send_json(503, {"error": "server_busy"})
            return

        # Mark as a PSTN call in the registry
        REGISTRY.mark_pstn_session(session_id, caller_number=caller_number, call_id=call_id)

        REGISTRY.attach_room_name(session_id, room_name)

        # Allocate tune port
        tune_port = REGISTRY.allocate_tune_port(session_id, base_port=_DEFAULT_BOT_PORT, span=200)
        if tune_port is None:
            print(f"[pstn] no available tune port for session {session_id[:8]}")
            _safe_delete_room(room_name, reason="no_tune_port")
            REGISTRY.reap(session_id)
            self._send_json(503, {"error": "server_busy"})
            return

        try:
            proc = _spawn_bot(
                session_id,
                callback_url,
                tune_port,
                room_name=room_name,
                dialin_call_id=call_id,
                dialin_call_domain=call_domain,
            )
        except Exception as exc:
            print(f"[pstn] spawn failed: {exc}")
            _safe_delete_room(room_name, reason="spawn_failed")
            REGISTRY.reap(session_id)
            self._send_json(500, {"error": f"spawn_failed: {exc}"})
            return

        REGISTRY.attach_process(session_id, proc)
        _start_reaper_thread(session_id, proc)

        # --- Duration enforcer (same pattern as web sessions) ---
        _max_secs = _PSTN_MAX_DURATION_MIN * 60
        _warn_secs = max(_max_secs - 30, 5)
        _warn_text = (
            "Just to let you know, we have about 30 seconds left. "
            "Is there anything else I can help with?"
        )

        def _pstn_warn(sid: str) -> None:
            _tp = _get_bot_tune_port(sid)
            if _tp is None:
                print(f"[pstn] warn skipped — no tune_port for session {sid[:8]}")
                return
            try:
                payload_bytes = json.dumps({"text": _warn_text}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{_tp}/speak",
                    data=payload_bytes,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = urllib.request.urlopen(req, timeout=3)
                if resp.status == 200:
                    print(f"[pstn] sent duration warning (session={sid[:8]})")
                else:
                    print(f"[pstn] warn got status {resp.status} (session={sid[:8]})")
            except Exception as exc:
                print(f"[pstn] warn failed: {exc}")

        def _pstn_kill(sid: str) -> None:
            print(f"[pstn] session {sid[:8]} hit {_max_secs}s limit — force-ending")
            # Try to speak goodbye before disconnecting
            _tp = _get_bot_tune_port(sid)
            if _tp is not None:
                try:
                    _bye = json.dumps({"text": "Thank you for calling. Goodbye."}).encode()
                    _req = urllib.request.Request(
                        f"http://127.0.0.1:{_tp}/speak",
                        data=_bye,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    _resp = urllib.request.urlopen(_req, timeout=3)
                    print(
                        "[pstn] sent max-duration goodbye "
                        f"(session={sid[:8]}, status={_resp.status})"
                    )
                    time.sleep(2.0)  # let TTS play before disconnecting
                except Exception as exc:
                    print(f"[pstn] goodbye speak failed (session={sid[:8]}): {exc}")
            # Delete the room first — this force-disconnects all participants
            # including the PSTN/SIP leg, ensuring the caller sees a hangup.
            if room_name:
                _safe_delete_room(room_name, reason=f"duration_limit_{sid[:8]}")

            # Then terminate the bot process gracefully and escalate if needed.
            _proc = REGISTRY.get_process(sid)
            if _proc is not None and _proc.poll() is None:
                try:
                    _proc.terminate()
                    _deadline = time.time() + 5.0
                    while time.time() < _deadline and _proc.poll() is None:
                        time.sleep(0.2)
                    if _proc.poll() is None:
                        print(
                            "[pstn] process still alive after terminate, killing "
                            f"(session={sid[:8]})"
                        )
                        _proc.kill()
                except Exception as exc:
                    print(f"[pstn] process shutdown failed (session={sid[:8]}): {exc}")
            else:
                # If the process is already gone (or missing), ensure cleanup now.
                REGISTRY.reap(sid)

        _wd_warn = threading.Timer(_warn_secs, _pstn_warn, args=[session_id])
        _wd_warn.daemon = True
        _wd_warn.start()

        _wd = threading.Timer(_max_secs, _pstn_kill, args=[session_id])
        _wd.daemon = True
        _wd.start()

        REGISTRY.attach_watchdog(session_id, _wd)
        REGISTRY.attach_watchdog_warn(session_id, _wd_warn)

        # --- Log the call ---
        _log_pstn_call(session_id, caller_number, called_number, call_id, room_name)

        # --- Respond immediately so Daily doesn't time out the webhook ---
        # The bot will call pinlessCallUpdate itself once it joins the room
        # and fires dialin-ready. We don't need to block here.
        self._send_json(200, {"room_name": room_name, "session_id": session_id})

        # --- Monitor bot startup in background thread ---
        def _monitor_bot_startup() -> None:
            try:
                event = q.get(timeout=_SPAWN_TIMEOUT_SECS)
            except _queue.Empty:
                print(f"[pstn] bot timed out for session {session_id[:8]} — killing")
                with contextlib.suppress(Exception):
                    proc.kill()
                REGISTRY.reap(session_id)
                return

            event_type = event.get("type")
            if event_type == "_reaped":
                rc = proc.returncode
                print(f"[pstn] bot exited before ready (rc={rc}) for call {call_id[:12]}")
                return

            if event_type != "ready":
                print(f"[pstn] unexpected first event: {event_type}")
                with contextlib.suppress(Exception):
                    proc.kill()
                REGISTRY.reap(session_id)
                return

            print(
                f"[pstn] call connected: caller={caller_number} room={room_name} "
                f"session={session_id[:8]}"
            )

        _monitor_thread = threading.Thread(
            target=_monitor_bot_startup, daemon=True, name=f"pstn-monitor-{session_id[:8]}"
        )
        _monitor_thread.start()

    def _handle_inquiry(self) -> None:
        """POST /api/inquiry — receive discovery form submissions.

        Security: rate-limited, honeypot-checked, input-validated,
        size-limited. Saves to logs/inquiries/ and sends email notification.
        """
        # 1. Size limit
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > self._INQUIRY_MAX_BODY or content_length <= 0:
            self._send_json(400, {"error": "invalid_body"})
            return

        # 2. Rate limit by IP
        direct_ip, fwd_ip = self._client_ip()
        client_ip = fwd_ip or direct_ip
        if not _inquiry_rate_ok(client_ip):
            self._send_json(
                429,
                {
                    "error": "rate_limited",
                    "detail": "Too many submissions. Please try again later.",
                },
            )
            return

        # 3. Parse JSON
        try:
            raw = self.rfile.read(content_length)
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"error": "invalid_json"})
            return

        if not isinstance(body, dict):
            self._send_json(400, {"error": "invalid_json"})
            return

        # 4. Honeypot check — if company_website is filled, it's a bot
        if body.get("company_website", "").strip():
            # Silently accept to not tip off bots, but don't save
            self._send_json(200, {"status": "ok"})
            return

        # 5. Validate required fields
        property_name = str(body.get("property_name", "")).strip()[:200]
        location = str(body.get("location", "")).strip()[:200]
        name = str(body.get("name", "")).strip()[:100]
        email_val = str(body.get("email", "")).strip()[:254]
        next_step = str(body.get("next_step", "")).strip()

        if not property_name or not location or not name or not email_val or not next_step:
            self._send_json(
                422,
                {
                    "error": "missing_required",
                    "detail": "property_name, location, name, email, and next_step are required.",
                },
            )
            return

        # 6. Email format validation (basic)
        import re as _re

        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_val):
            self._send_json(422, {"error": "invalid_email"})
            return

        # 7. Validate enum fields
        if next_step not in self._INQUIRY_VALID_STEPS:
            self._send_json(422, {"error": "invalid_next_step"})
            return

        pain = str(body.get("pain", "")).strip()
        if pain and pain not in self._INQUIRY_VALID_PAINS:
            pain = ""

        # 8. Sanitize optional fields (truncate to reasonable lengths)
        size = str(body.get("size", "")).strip()[:50]
        property_type = str(body.get("property_type", "")).strip()[:100]
        pms = str(body.get("pms", "")).strip()[:100]
        role = str(body.get("role", "")).strip()[:100]

        # Lists — only accept known short values
        languages_raw = body.get("languages", [])
        languages = [str(lang).strip()[:10] for lang in languages_raw if isinstance(lang, str)][:20]
        channels_raw = body.get("channels", [])
        channels = [str(c).strip()[:20] for c in channels_raw if isinstance(c, str)][:10]

        # 9. Build clean payload
        inquiry_id = uuid.uuid4().hex[:12]
        now = datetime.now(UTC).isoformat()
        payload = {
            "id": inquiry_id,
            "submitted_at": now,
            "property_name": property_name,
            "location": location,
            "size": size or None,
            "property_type": property_type or None,
            "languages": languages,
            "pain": pain or None,
            "pms": pms or None,
            "channels": channels,
            "name": name,
            "role": role or None,
            "email": email_val,
            "next_step": next_step,
            "_ip": client_ip,
        }

        # 10. Save to disk
        filename = f"{now[:10]}_{inquiry_id}.json"
        filepath = _INQUIRY_DIR / filename
        try:
            filepath.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"[inquiry] file write failed: {exc}\n")
            self._send_json(500, {"error": "server_error"})
            return

        # 11. Send email notification (async, don't block the response)
        threading.Thread(target=_send_inquiry_email, args=(payload,), daemon=True).start()

        # 12. Respond
        self._send_json(200, {"status": "ok", "id": inquiry_id})

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

    # ── Audit log viewer endpoints ──────────────────────────────────────────

    def _handle_audit_sessions(self):
        """GET /api/audit/sessions?from=YYYY-MM-DD&to=YYYY-MM-DD — list session logs."""
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        date_from = (params.get("from") or [None])[0]
        date_to = (params.get("to") or [None])[0]

        sessions_dir = Path(__file__).resolve().parent.parent / "logs" / "audit" / "sessions"
        if not sessions_dir.exists():
            self._send_json(200, [])
            return

        results = []
        for f in sorted(sessions_dir.glob("*.jsonl"), reverse=True):
            # Filename: YYYY-MM-DD_HH-MM-SS_<id8>.jsonl
            name = f.stem  # e.g. 2026-05-25_12-06-33_test-aud
            file_date = name[:10]  # YYYY-MM-DD

            # Apply date filters
            if date_from and file_date < date_from:
                continue
            if date_to and file_date > date_to:
                continue

            # Read first and last line for summary
            lines = f.read_text(encoding="utf-8").strip().splitlines()
            if not lines:
                continue
            first = json.loads(lines[0])
            last = json.loads(lines[-1]) if len(lines) > 1 else first

            results.append(
                {
                    "filename": f.name,
                    "date": file_date,
                    "time": name[11:19].replace("-", ":"),
                    "session_id": first.get("session_id", ""),
                    "turns": len(lines),
                    "client_ip": first.get("client_ip", ""),
                    "first_query": (first.get("user_query", ""))[:80],
                    "last_ts": last.get("ts", ""),
                    "channel": first.get("channel", ""),
                    "model": first.get("model", ""),
                }
            )

        self._send_json(200, results)

    def _handle_audit_session_detail(self, filename: str):
        """GET /api/audit/session/<filename> — return all turns of a session."""
        # Security: prevent path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            self._send_json(400, {"error": "invalid filename"})
            return

        sessions_dir = Path(__file__).resolve().parent.parent / "logs" / "audit" / "sessions"
        filepath = sessions_dir / filename
        if not filepath.exists() or filepath.suffix != ".jsonl":
            self._send_json(404, {"error": "not found"})
            return

        turns = []
        for line in filepath.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                turns.append(json.loads(line))

        self._send_json(200, turns)

    # ── Vector store viewer endpoint ────────────────────────────────────────

    def _handle_rag_chunks(self):
        """GET /api/rag/chunks?hotel_id=&doc_id=&language=&page=&per_page= — browse chunks."""
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        hotel_id = (params.get("hotel_id") or [None])[0]
        doc_id = (params.get("doc_id") or [None])[0]
        language = (params.get("language") or [None])[0]
        page = int((params.get("page") or ["1"])[0])
        per_page = min(int((params.get("per_page") or ["50"])[0]), 200)

        import os

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        if not db_path.exists():
            self._send_json(
                200, {"chunks": [], "total": 0, "hotels": [], "docs": [], "languages": []}
            )
            return

        import sqlite3

        conn = sqlite3.connect(str(db_path), check_same_thread=False)

        # Get distinct values for filters
        hotels = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT hotel_id FROM chunks ORDER BY hotel_id"
            ).fetchall()
        ]
        docs = []
        languages_list = []

        # Build query with filters
        where_parts = []
        query_params = []
        if hotel_id:
            where_parts.append("hotel_id = ?")
            query_params.append(hotel_id)
        if doc_id:
            where_parts.append("doc_id = ?")
            query_params.append(doc_id)
        if language:
            where_parts.append("language = ?")
            query_params.append(language)

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # Get total count
        total = conn.execute(f"SELECT COUNT(*) FROM chunks{where_clause}", query_params).fetchone()[
            0
        ]

        # Get distinct docs and languages for current hotel filter
        if hotel_id:
            docs = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT doc_id FROM chunks WHERE hotel_id = ? ORDER BY doc_id",
                    (hotel_id,),
                ).fetchall()
            ]
            languages_list = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT language FROM chunks WHERE hotel_id = ? ORDER BY language",
                    (hotel_id,),
                ).fetchall()
            ]
        else:
            docs = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT doc_id FROM chunks ORDER BY doc_id"
                ).fetchall()
            ]
            languages_list = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT language FROM chunks ORDER BY language"
                ).fetchall()
            ]

        # Paginated fetch (exclude embedding blob for performance)
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"SELECT id, hotel_id, doc_id, chunk_index, language, category, text, updated_at "
            f"FROM chunks{where_clause} ORDER BY hotel_id, doc_id, chunk_index "
            f"LIMIT ? OFFSET ?",
            query_params + [per_page, offset],
        ).fetchall()
        conn.close()

        chunks = []
        for r in rows:
            chunks.append(
                {
                    "id": r[0],
                    "hotel_id": r[1],
                    "doc_id": r[2],
                    "chunk_index": r[3],
                    "language": r[4],
                    "category": r[5],
                    "text": r[6],
                    "updated_at": r[7],
                }
            )

        self._send_json(
            200,
            {
                "chunks": chunks,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page,
                "hotels": hotels,
                "docs": docs,
                "languages": languages_list,
            },
        )

    def _handle_admin_call_center_status(self) -> None:
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return

        payload = {"es": "error", "qdrant": "error"}

        es_status, es_body = _es_json_request("GET", "/")
        if es_status == 200 and isinstance(es_body, dict):
            version = (es_body.get("version") or {}).get("number")
            if version:
                payload["es"] = "ok"
                payload["es_version"] = version
            else:
                payload["es_error"] = es_body
        else:
            payload["es_error"] = es_body

        qdrant_status, qdrant_body = _qdrant_json_request("GET", "/collections")
        if qdrant_status == 200 and isinstance(qdrant_body, dict):
            collections = (qdrant_body.get("result") or {}).get("collections") or []
            payload["qdrant"] = "ok"
            payload["qdrant_collections"] = len(collections)
        else:
            payload["qdrant_error"] = qdrant_body

        self._send_json(200, payload)

    def _handle_admin_call_center_es_hotels(self) -> None:
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return

        status, body = _es_json_request(
            "POST",
            f"/{ES_INDEX}/_search",
            payload={
                "size": 1000,
                "query": {"match_all": {}},
                "_source": [
                    "hotel_id",
                    "name",
                    "chain",
                    "district",
                    "price_tier",
                    "board_type",
                    "star_rating",
                ],
            },
        )
        if status != 200 or not isinstance(body, dict):
            self._send_upstream_error("elasticsearch", status, body)
            return

        hits = (body.get("hits") or {}).get("hits") or []
        hotels = [hit.get("_source", {}) for hit in hits]
        self._send_json(200, {"count": len(hotels), "hotels": hotels})

    def _handle_admin_call_center_es_search(self) -> None:
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        query = (params.get("q") or [""])[0].strip()
        if not query:
            self._send_json(400, {"error": "missing_query", "detail": "Missing ?q= parameter"})
            return

        status, body = _es_json_request(
            "POST",
            f"/{ES_INDEX}/_search",
            payload={
                "size": 10,
                "query": {
                    "multi_match": {
                        "query": query,
                        "fields": ["name^3", "aliases^2", "chain", "district", "city"],
                        "type": "best_fields",
                        "fuzziness": "AUTO",
                    }
                },
            },
        )
        if status != 200 or not isinstance(body, dict):
            self._send_upstream_error("elasticsearch", status, body)
            return

        hits = (body.get("hits") or {}).get("hits") or []
        results = [
            {
                "hotel_id": (hit.get("_source") or {}).get("hotel_id"),
                "name": (hit.get("_source") or {}).get("name"),
                "score": hit.get("_score", 0.0),
            }
            for hit in hits
        ]
        self._send_json(200, {"query": query, "count": len(results), "results": results})

    def _handle_admin_call_center_qdrant_collections(self) -> None:
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return

        status, body = _qdrant_json_request("GET", "/collections")
        if status != 200 or not isinstance(body, dict):
            self._send_upstream_error("qdrant", status, body)
            return

        collections = (body.get("result") or {}).get("collections") or []
        self._send_json(200, {"collections": collections})

    def _handle_admin_call_center_qdrant_points(self) -> None:
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        collection = (params.get("collection") or [QDRANT_COLLECTION])[0]
        hotel_id = (params.get("hotel_id") or [""])[0].strip()
        try:
            limit = max(1, min(int((params.get("limit") or ["20"])[0]), 100))
        except ValueError:
            limit = 20

        payload = {"limit": limit, "with_payload": True, "with_vector": False}
        if hotel_id:
            payload["filter"] = {"must": [{"key": "hotel_id", "match": {"value": hotel_id}}]}

        status, body = _qdrant_json_request(
            "POST",
            f"/collections/{collection}/points/scroll",
            payload=payload,
        )
        if status != 200 or not isinstance(body, dict):
            self._send_upstream_error("qdrant", status, body)
            return

        points = (body.get("result") or {}).get("points") or []
        self._send_json(200, {"collection": collection, "count": len(points), "points": points})

    def _handle_admin_call_center_qdrant_search(self) -> None:
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return

        body = self._read_json_body()
        query = str(body.get("query") or "").strip()
        hotel_id = str(body.get("hotel_id") or "").strip()
        try:
            limit = max(1, min(int(body.get("limit", 5)), 20))
        except (TypeError, ValueError):
            limit = 5

        if not query:
            self._send_json(400, {"error": "missing_query", "detail": "Missing 'query' in body"})
            return

        try:
            t0 = time.perf_counter()
            embeddings = embed_texts([query], prefix=PREFIX_QUERY)
            embed_ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:
            self._send_json(500, {"error": "embed_failed", "detail": str(exc)})
            return

        payload = {"vector": embeddings[0], "limit": limit, "with_payload": True}
        if hotel_id:
            payload["filter"] = {"must": [{"key": "hotel_id", "match": {"value": hotel_id}}]}

        status, qdrant_body = _qdrant_json_request(
            "POST",
            f"/collections/{QDRANT_COLLECTION}/points/search",
            payload=payload,
        )
        if status != 200 or not isinstance(qdrant_body, dict):
            self._send_upstream_error("qdrant", status, qdrant_body)
            return

        results = qdrant_body.get("result") or []
        self._send_json(
            200,
            {
                "query": query,
                "embed_ms": round(embed_ms, 1),
                "count": len(results),
                "results": [
                    {
                        "score": result.get("score"),
                        "hotel_id": (result.get("payload") or {}).get("hotel_id"),
                        "hotel_name": (result.get("payload") or {}).get("hotel_name"),
                        "category": (result.get("payload") or {}).get("category"),
                        "text": (result.get("payload") or {}).get("text"),
                        "text_en": (result.get("payload") or {}).get("text_en"),
                    }
                    for result in results
                ],
            },
        )

    def _handle_concierge_logs(self):
        """GET /api/admin/concierge-logs?date=YYYY-MM-DD — return all log entries for a day."""
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        date_str = (params.get("date") or [None])[0]

        # Check both possible log directories (CWD/logs and project-root/logs)
        log_dirs = []
        log_dirs.append(Path(os.environ.get("CONCIERGE_LOG_DIR", "logs")))
        project_root = Path(os.path.dirname(os.path.abspath(__file__))).parent / "logs"
        if project_root.exists() and project_root not in log_dirs:
            log_dirs.append(project_root)

        # If no date, list available days
        if not date_str:
            days = set()
            for log_dir in log_dirs:
                if log_dir.exists():
                    for f in log_dir.glob("travel_agent_consierge-*.jsonl"):
                        day = f.stem.replace("travel_agent_consierge-", "")
                        if "_" not in day:  # skip files like -2026-06-05_old_test
                            days.add(day)
            self._send_json(200, {"days": sorted(days, reverse=True)})
            return

        # Find the log file
        log_file = None
        for log_dir in log_dirs:
            candidate = log_dir / f"travel_agent_consierge-{date_str}.jsonl"
            if candidate.exists():
                log_file = candidate
                break

        if not log_file:
            self._send_json(200, {"entries": [], "sessions": []})
            return

        entries = []
        for line in log_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # Group session ids preserving order of first appearance
        seen = {}
        for e in entries:
            sid = e.get("session_id") or "unknown"
            if sid not in seen:
                seen[sid] = {
                    "session_id": sid,
                    "first_ts": e.get("ts"),
                    "turns": 0,
                    "first_utterance": e.get("utterance", "")[:60],
                }
            seen[sid]["turns"] += 1

        self._send_json(200, {"entries": entries, "sessions": list(seen.values())})

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

    def _handle_stt_providers(self):
        """GET /api/stt-providers — available STT engines and current default."""
        from voxtera.stt import _STT_BUILDERS

        current = os.environ.get("STT_PROVIDER", "whisper").lower()
        providers = list(_STT_BUILDERS.keys())
        payload = json.dumps({"providers": providers, "current": current}).encode("utf-8")
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
            # fill_hotel_name resolves the optional {hotel_name} placeholder to
            # the per-language generic ("our hotel") — the TTS test page has no
            # hotel context.
            from voxtera.prompts import fill_hotel_name

            text = GREETINGS.get(lang)
            if not text:
                base = fill_hotel_name(GREETINGS["en"], None, "en")
                text = _translate_greeting(base, lang, model)
            else:
                text = fill_hotel_name(text, None, lang)

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
        demo_token = (body.get("demo_token") or "").strip()

        request_id = str(uuid.uuid4())
        client_ip, forwarded_for = self._client_ip()
        user_agent = self.headers.get("User-Agent", "")
        rag_elapsed_ms: float | None = None
        llm_elapsed_ms: float | None = None

        # ── Demo access gate ──
        # If user has a valid token, allow. Otherwise enforce free message limit.
        has_valid_token = _validate_demo_token(demo_token) is not None or (
            demo_token in _load_demo_codes()
        )
        if not has_valid_token:
            ip_for_limit = forwarded_for or client_ip
            if not _demo_anon_ok(ip_for_limit):
                self._send_json(
                    403,
                    {
                        "error": "demo_limit_reached",
                        "detail": "Free messages used. Enter an access code for full demo.",
                        "remaining": 0,
                    },
                )
                return

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
                # Keep tools available so the model can FIX a rejected call
                # (e.g. invalid category) — bounded to one retry round below.
                r2 = oai_client.chat.completions.create(
                    model=model,
                    max_tokens=150,
                    messages=messages,
                    stream=False,
                    tools=_TOOLS or None,
                    tool_choice="auto" if _TOOLS else None,
                )
                print(f"[timing] llm_tool_followup={(_time.monotonic() - t0_llm2) * 1000:.0f}ms")
                msg2 = r2.choices[0].message
                if msg2.tool_calls:
                    # One corrected retry, then a final text-only pass (no
                    # tools) so this can never loop.
                    messages.append(msg2.model_dump())
                    for tc2 in msg2.tool_calls:
                        result2 = _handle_tool_call(tc2, session_id)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc2.id, "content": result2}
                        )
                    r3 = oai_client.chat.completions.create(
                        model=model, max_tokens=150, messages=messages, stream=False
                    )
                    full_text = (r3.choices[0].message.content or "").strip()
                else:
                    full_text = (msg2.content or "").strip()
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

    def _handle_admin_prompts_list(self) -> None:
        """GET /api/admin/prompts — every editable prompt with its explanation."""
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        pdir = _prompts_dir()
        out = []
        for name, meta in _PROMPT_REGISTRY.items():
            path = pdir / meta["file"]
            try:
                content = path.read_bytes().decode("utf-8")
                mtime = path.stat().st_mtime
            except OSError:
                content, mtime = "", None
            out.append(
                {
                    "name": name,
                    "file": meta["file"],
                    "title": meta["title"],
                    "description": meta["description"],
                    "content": content,
                    "mtime": mtime,
                }
            )
        self._send_json(200, {"prompts": out})

    def _handle_admin_prompts_save(self) -> None:
        """POST /api/admin/prompts — save one prompt {name, content}.

        Whitelisted names only (no path traversal); JSON prompts are validated
        before writing; the previous version is backed up to
        logs/prompt_backups/. Changes hot-reload — no restart needed.
        """
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        body = self._read_json_body()
        name = (body.get("name") or "").strip()
        content = body.get("content")
        meta = _PROMPT_REGISTRY.get(name)
        if meta is None or not isinstance(content, str) or not content.strip():
            self._send_json(400, {"error": "unknown_prompt_or_empty_content"})
            return
        if meta["file"].endswith(".json"):
            try:
                json.loads(content)
            except ValueError as e:
                self._send_json(400, {"error": "invalid_json", "detail": str(e)})
                return
        path = _prompts_dir() / meta["file"]
        try:
            backup_dir = Path(__file__).resolve().parent.parent / "logs" / "prompt_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            # Save time in the filename (same convention as the audit logs), e.g.
            # concierge_render.md.2026-06-06_18-42-07.bak
            stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
            (backup_dir / f"{meta['file']}.{stamp}.bak").write_bytes(path.read_bytes())
        except OSError:
            pass  # backup is best-effort; never block a save on it
        path.write_bytes(content.encode("utf-8"))
        self._send_json(200, {"ok": True, "name": name, "mtime": path.stat().st_mtime})

    # Legacy compatibility: keep the old Greetings Editor API working.
    def _greetings_json_path(self) -> Path:
        return (_voice_prompts_dir() / _VOICE_PROMPT_REGISTRY["greetings"]["file"]).resolve()

    def _load_greetings_json(self) -> tuple[Path, dict] | tuple[Path, None]:
        path = self._greetings_json_path()
        if not path.is_file():
            return path, None
        data = json.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("greetings.json top level must be an object")
        if not isinstance(data.get("greetings"), dict):
            data["greetings"] = {}
        if not isinstance(data.get("timed_greetings"), dict):
            data["timed_greetings"] = {}
        if not isinstance(data.get("generic_hotel"), dict):
            data["generic_hotel"] = {}
        return path, data

    def _write_greetings_json(self, path: Path, data: dict) -> None:
        content = json.dumps(data, ensure_ascii=False, indent=2)
        err = _validate_greetings_json(content)
        if err:
            raise ValueError(err)
        path.write_bytes((content + "\n").encode("utf-8"))

    def _handle_admin_greetings_get(self) -> None:
        """GET /api/admin/greetings — return full greetings JSON."""
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        try:
            _, data = self._load_greetings_json()
        except (ValueError, OSError) as exc:
            self._send_json(500, {"error": "invalid_greetings_file", "detail": str(exc)})
            return
        if data is None:
            self._send_json(404, {"error": "greetings.json not found"})
            return
        self._send_json(200, data)

    def _handle_admin_greetings_post(self) -> None:
        """POST /api/admin/greetings — update or add one language greeting."""
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        body = self._read_json_body()
        lang = (body.get("lang") or "").strip().lower()
        greeting_raw = body.get("greeting")
        greeting = greeting_raw.strip() if isinstance(greeting_raw, str) else ""
        timed = body.get("timed")
        if not lang or not greeting:
            self._send_json(400, {"error": "lang and greeting are required"})
            return
        if timed is not None and not isinstance(timed, dict):
            self._send_json(400, {"error": "timed must be an object when provided"})
            return
        try:
            path, data = self._load_greetings_json()
        except (ValueError, OSError) as exc:
            self._send_json(500, {"error": "invalid_greetings_file", "detail": str(exc)})
            return
        if data is None:
            data = {"greetings": {}, "timed_greetings": {}, "generic_hotel": {}}

        data["greetings"][lang] = greeting
        if isinstance(timed, dict):
            morning = (timed.get("morning") or "").strip() or greeting
            afternoon = (timed.get("afternoon") or "").strip() or greeting
            evening = (timed.get("evening") or "").strip() or greeting
            data["timed_greetings"][lang] = {
                "morning": morning,
                "afternoon": afternoon,
                "evening": evening,
            }

        try:
            self._write_greetings_json(path, data)
        except ValueError as exc:
            self._send_json(400, {"error": "invalid_greetings", "detail": str(exc)})
            return
        except OSError as exc:
            self._send_json(500, {"error": "write_failed", "detail": str(exc)})
            return
        self._send_json(200, {"ok": True, "lang": lang})

    def _handle_admin_greetings_delete(self, lang: str) -> None:
        """DELETE /api/admin/greetings/<lang> — remove one language."""
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        lang = (lang or "").strip().lower()
        if not lang:
            self._send_json(400, {"error": "language code required"})
            return
        if lang == "en":
            self._send_json(400, {"error": "cannot delete English (fallback language)"})
            return

        try:
            path, data = self._load_greetings_json()
        except (ValueError, OSError) as exc:
            self._send_json(500, {"error": "invalid_greetings_file", "detail": str(exc)})
            return
        if data is None:
            self._send_json(404, {"error": "greetings.json not found"})
            return

        removed = False
        if lang in data["greetings"]:
            del data["greetings"][lang]
            removed = True
        if lang in data["timed_greetings"]:
            del data["timed_greetings"][lang]
            removed = True
        if lang in data["generic_hotel"]:
            del data["generic_hotel"][lang]
            removed = True

        if not removed:
            self._send_json(404, {"error": f"language '{lang}' not found"})
            return

        try:
            self._write_greetings_json(path, data)
        except ValueError as exc:
            self._send_json(400, {"error": "invalid_greetings", "detail": str(exc)})
            return
        except OSError as exc:
            self._send_json(500, {"error": "write_failed", "detail": str(exc)})
            return
        self._send_json(200, {"ok": True, "deleted": lang})

    def _handle_admin_voice_prompts_list(self) -> None:
        """GET /api/admin/voice-prompts — voice-concierge prompts + explanations."""
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        pdir = _voice_prompts_dir()
        out = []
        for name, meta in _VOICE_PROMPT_REGISTRY.items():
            path = (pdir / meta["file"]).resolve()
            try:
                content = path.read_bytes().decode("utf-8")
                mtime = path.stat().st_mtime
            except OSError:
                content, mtime = "", None
            out.append(
                {
                    "name": name,
                    "file": Path(meta["file"]).name,
                    "title": meta["title"],
                    "description": meta["description"],
                    "readonly": bool(meta.get("readonly")),
                    "content": content,
                    "mtime": mtime,
                }
            )
        self._send_json(200, {"prompts": out})

    def _handle_admin_voice_prompts_save(self) -> None:
        """POST /api/admin/voice-prompts — save one prompt {name, content}.

        Whitelisted names only; readonly entries are rejected; greetings.json
        gets structural validation (a bad save would stop the voice bot from
        booting). Backup to logs/prompt_backups/ before every write. NO
        hot-reload here: the voice bot imports its prompts at startup, so the
        save applies from the NEXT CALL.

        Bytes are written exactly as received (UTF-8, no newline
        normalisation) — audio.py embeds system_prompt.md as a byte-stable
        semantic fingerprint.
        """
        ok, _ = self._admin_auth(require_daily=False)
        if not ok:
            return
        body = self._read_json_body()
        name = (body.get("name") or "").strip()
        content = body.get("content")
        meta = _VOICE_PROMPT_REGISTRY.get(name)
        if meta is None or not isinstance(content, str) or not content.strip():
            self._send_json(400, {"error": "unknown_prompt_or_empty_content"})
            return
        if meta.get("readonly"):
            self._send_json(403, {"error": "readonly_prompt"})
            return
        if name == "greetings":
            err = _validate_greetings_json(content)
            if err:
                self._send_json(400, {"error": "invalid_greetings", "detail": err})
                return
        path = (_voice_prompts_dir() / meta["file"]).resolve()
        try:
            backup_dir = Path(__file__).resolve().parent.parent / "logs" / "prompt_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
            (backup_dir / f"{path.name}.{stamp}.bak").write_bytes(path.read_bytes())
        except OSError:
            pass  # backup is best-effort; never block a save on it
        path.write_bytes(content.encode("utf-8"))
        self._send_json(200, {"ok": True, "name": name, "mtime": path.stat().st_mtime})

    def _handle_concierge(self) -> None:
        """POST /api/concierge — synchronous JSON Q&A backed by ConciergePipeline.

        Request:  {"utterance": str, "region": str, "session_id": str | None}
        Response: full ConciergePipeline.run() dict
                  (session_id, path, reason, answer, escalation,
                   clarification, decomposition, router, retrieval, timings).
        """
        from voxtera.call_center.compound import CompoundAndDiscovery
        from voxtera.call_center.pipeline import ConciergePipeline
        from voxtera.call_center.resolver import HotelResolver
        from voxtera.call_center.router import SourceRouter
        from voxtera.call_center.triage import Triage
        from voxtera.call_center.web_retriever import WebRetriever

        body = self._read_json_body()
        utterance = (body.get("utterance") or "").strip()
        # Preserve empty string as explicit "all regions" signal (distinct from None/absent).
        raw_region = body.get("region")
        region = raw_region.strip() if isinstance(raw_region, str) else None
        session_id = (body.get("session_id") or "").strip() or None
        if not utterance:
            self._send_json(400, {"error": "utterance_required"})
            return

        async def _run() -> dict:
            # Shared warm deps (connections, LLM fns) + cheap per-request wiring
            # so per-run pipeline state stays isolated across concurrent requests.
            deps = await _concierge_deps()
            pipeline = ConciergePipeline(
                session_store=deps["store"],
                classifier=deps["classifier"],
                decomposer=deps["decomposer"],
                triage=Triage(),
                router=SourceRouter(),
                compound=CompoundAndDiscovery(session=deps["http"]),
                resolver=HotelResolver(session=deps["http"]),
                web_retriever=WebRetriever(),
                render_fn=deps["render_fn"],
                web_synth_fn=deps["web_synth_fn"],
                converse_fn=deps["converse_fn"],
                web_query_fn=deps["web_query_fn"],
            )
            return await pipeline.run(
                utterance=utterance,
                session_id=session_id,
                region=region,
            )

        try:
            result = asyncio.run_coroutine_threadsafe(_run(), _concierge_loop()).result(timeout=120)
        except Exception as exc:  # noqa: BLE001
            print(f"[concierge] error: {exc}")
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, result)

    def _handle_concierge_feedback(self) -> None:
        """POST /api/concierge/feedback — store a thumbs up/down rating + comment.

        Request:  {"session_id": str|null, "utterance": str, "answer": str,
                   "rating": "up"|"down", "comment": str}
        Appended as a ``{"type": "feedback"}`` NDJSON record to the same daily
        ``travel_agent_consierge-*.jsonl`` log as the dialog records, so each
        conversation and its user feedback live in one file.
        """
        from voxtera.call_center.pipeline import append_feedback_record

        body = self._read_json_body()
        rating = (body.get("rating") or "").strip().lower()
        if rating not in ("up", "down"):
            self._send_json(400, {"error": "rating_must_be_up_or_down"})
            return
        append_feedback_record(
            {
                "session_id": (body.get("session_id") or "").strip() or None,
                "utterance": str(body.get("utterance") or "")[:2000],
                "answer": str(body.get("answer") or "")[:4000],
                "rating": rating,
                "comment": str(body.get("comment") or "")[:2000],
            }
        )
        self._send_json(200, {"ok": True})

    def _handle_concierge_replay(self) -> None:
        """POST /api/concierge/replay — DEBUG: run the pipeline from a user-edited
        decomposition, skipping the LLM decompose step.

        Lets you fix a field (e.g. set hotel_mention) in the debug drawer and see
        whether the DOWNSTREAM (triage → route → resolve → retrieve → render) then
        works — isolating decomposer bugs from retrieval bugs. The decomposition
        is used VERBATIM (no coerce), so what you type is exactly what runs.

        Request:  {"utterance": str, "region": str|null, "session_id": str|null,
                   "decomposition": {...edited fields...}}
        Response: same shape as /api/concierge.
        """
        from voxtera.call_center.compound import CompoundAndDiscovery
        from voxtera.call_center.pipeline import ConciergePipeline
        from voxtera.call_center.resolver import HotelResolver
        from voxtera.call_center.router import SourceRouter
        from voxtera.call_center.triage import Triage

        body = self._read_json_body()
        utterance = (body.get("utterance") or "").strip()
        raw_region = body.get("region")
        region = raw_region.strip() if isinstance(raw_region, str) else None
        session_id = (body.get("session_id") or "").strip() or None
        edited = body.get("decomposition")
        if not utterance or not isinstance(edited, dict):
            self._send_json(400, {"error": "utterance_and_decomposition_required"})
            return

        class _FixedDecomposer:
            """Returns the operator's edited decomposition verbatim (no LLM, no coerce)."""

            async def decompose(self, _utterance: str, _ctx: dict) -> dict:
                return dict(edited)

        async def _run() -> dict:
            deps = await _concierge_deps()
            pipeline = ConciergePipeline(
                session_store=deps["store"],
                classifier=deps["classifier"],
                decomposer=_FixedDecomposer(),  # <-- the only difference from /api/concierge
                triage=Triage(),
                router=SourceRouter(),
                compound=CompoundAndDiscovery(session=deps["http"]),
                resolver=HotelResolver(session=deps["http"]),
                render_fn=deps["render_fn"],
                web_synth_fn=deps["web_synth_fn"],
                converse_fn=deps["converse_fn"],
                web_query_fn=deps["web_query_fn"],
            )
            return await pipeline.run(utterance=utterance, session_id=session_id, region=region)

        try:
            result = asyncio.run_coroutine_threadsafe(_run(), _concierge_loop()).result(timeout=120)
        except Exception as exc:  # noqa: BLE001
            print(f"[concierge-replay] error: {exc}")
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, result)

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

        # ── request metadata for audit logging ──
        client_ip = self.client_address[0]
        forwarded_for = self.headers.get("X-Forwarded-For")
        user_agent = self.headers.get("User-Agent")
        request_id = str(uuid.uuid4())

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

        _product_turn_counters[session_id] = _product_turn_counters.get(session_id, 0) + 1
        _audit.write_turn(
            session_id=session_id,
            turn_number=_product_turn_counters[session_id],
            client_ip=client_ip,
            forwarded_for=forwarded_for,
            user_agent=user_agent,
            channel="product_chat_http",
            hotel_id="voxtera",
            user_query=text,
            bot_reply=full_text,
            model=model,
            language="en",
            tts_provider="none",
            llm_ms=elapsed,
            rag_ms=None,
            status="answered",
            request_id=request_id,
        )
        push({"type": "done", "session_id": session_id, "text": full_text})
        finish()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    # --- Kill ALL stale voxtera processes from previous runs --------------------
    # Prevents phantom processes from holding Daily rooms, STT sessions, ports,
    # or memory open. This is the FIRST operation at startup.
    import signal

    _my_pid = os.getpid()
    _my_ppid = os.getppid()
    _killed = []
    _STALE_PATTERNS = ("voxtera.bot", "embedding_server.py", "serve.py")
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
            if proc.info["pid"] in (_my_pid, _my_ppid):
                continue
            cmdline = proc.info.get("cmdline") or []
            cmdline_str = " ".join(cmdline)
            if any(pat in cmdline_str for pat in _STALE_PATTERNS):
                try:
                    proc.kill()
                    _killed.append(proc.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
    except ImportError:
        # psutil not available — fall back to pkill
        import subprocess as _sp

        for pat in _STALE_PATTERNS:
            result = _sp.run(
                ["pkill", "-9", "-f", pat],
                capture_output=True,
            )
            if result.returncode == 0:
                _killed.append(f"(pkill {pat})")
    if _killed:
        print(f"[startup] killed stale phantom processes: {_killed}")
        # Give OS time to release ports held by killed processes
        import time

        time.sleep(0.5)
    else:
        print("[startup] no stale processes found — clean start")
    # --------------------------------------------------------------------------

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
    # Clean up orphaned rooms from previous server runs before accepting traffic.
    _cleanup_orphaned_rooms()

    # --------------------------------------------------------------------------
    # Pre-warm the call-center embedding model (e5-large) in a background
    # thread so the first /api/concierge request doesn't pay the ~3s cold
    # load. Embedding the warmup string forces sentence-transformers to
    # load weights now.
    def _warm_call_center_embed() -> None:
        try:
            from voxtera.call_center.embeddings import embed_query

            t0 = time.perf_counter()
            embed_query("warmup")
            print(f"[warmup] call_center embed model ready in {time.perf_counter() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"[warmup] embed pre-warm failed: {exc}")
        # Also warm the persistent concierge runtime so the first guest turn
        # doesn't pay it: create the shared loop + deps (Redis client, aiohttp
        # session) and fire a 1-token Anthropic ping to establish the TLS/HTTP2
        # connection the decompose/render calls will reuse.
        try:
            t0 = time.perf_counter()

            async def _warm_deps() -> None:
                deps = await _concierge_deps()
                # touch Redis so the connection is established now
                await deps["store"].load("warmup")
                if os.environ.get("ANTHROPIC_API_KEY"):
                    from voxtera.call_center.clients import anthropic_client

                    await anthropic_client().messages.create(
                        model=os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001"),
                        max_tokens=1,
                        messages=[{"role": "user", "content": "ping"}],
                    )

            asyncio.run_coroutine_threadsafe(_warm_deps(), _concierge_loop()).result(timeout=60)
            print(f"[warmup] concierge runtime warm in {time.perf_counter() - t0:.1f}s")
        except Exception as exc:  # noqa: BLE001
            print(f"[warmup] concierge runtime pre-warm failed: {exc}")

    threading.Thread(target=_warm_call_center_embed, daemon=True).start()
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
            print(f"Admin page on http://localhost:{port}/admin/admin.html")
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
            print(f"Trace page on http://localhost:{port}/admin/trace.html")
        else:
            print("Trace page disabled — set VOXTERA_ADMIN_TOKEN to enable")

        def _shutdown_handler(signum, frame):
            print(f"\n[shutdown] received signal {signum}, stopping...")
            httpd.shutdown()

        signal.signal(signal.SIGTERM, _shutdown_handler)

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            if _DAILY_DYNAMIC_ROOMS:
                REGISTRY.cleanup_all_rooms()
            if _embedding_proc is not None:
                _embedding_proc.terminate()
                _embedding_proc.wait(timeout=5)
