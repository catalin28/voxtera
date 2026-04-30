"""Structured conversation logger for audit, debugging, and evaluation.

Produces a JSONL file (one JSON object per conversational turn) at
``~/.voxtera/logs/conversations-<date>.jsonl``.

Each entry records:
- ``timestamp``: ISO-8601 UTC
- ``turn_id``: sequential turn counter per session
- ``hotel_id``: which hotel's knowledge was queried
- ``user_query``: what the guest said / typed
- ``rag_chunks``: list of retrieved chunks (doc_id, score, text preview)
- ``rag_elapsed_ms``: retrieval latency
- ``bot_reply``: the full LLM response text
- ``llm_elapsed_ms``: LLM thinking time

Usage:
    The ``RAGContextInjector`` calls ``log_rag_context(...)`` after retrieval.
    The ``PipelineTracer`` calls ``log_bot_reply(...)`` after the LLM finishes.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from loguru import logger

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_STATE_FILE = _LOG_DIR / "conversation-state.json"

# In-flight turn state: the injector writes the query + RAG data first,
# then the tracer completes the entry with the bot reply.
_lock = threading.Lock()
_pending_turn: dict | None = None
_turn_counter = 0
_log_file: Path | None = None


def get_state_file_path() -> Path:
    """Return the shared live state JSON path used by the demo dashboard."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_FILE


def _ensure_log_file() -> Path:
    global _log_file
    if _log_file is None:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        _log_file = _LOG_DIR / f"conversations-{date_str}.jsonl"
    return _log_file


def _write_entry(entry: dict) -> None:
    path = _ensure_log_file()
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_state() -> dict:
    path = get_state_file_path()
    if not path.exists():
        return {"current_turn": None, "recent_turns": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"current_turn": None, "recent_turns": []}


def _write_state(state: dict) -> None:
    path = get_state_file_path()
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        json.dump(state, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temp_path = Path(tmp.name)
    temp_path.replace(path)


def _sync_current_turn(entry: dict) -> None:
    state = _read_state()
    state["current_turn"] = entry
    _write_state(state)


def _finalize_turn(entry: dict) -> None:
    state = _read_state()
    recent_turns = state.get("recent_turns", [])
    recent_turns = [t for t in recent_turns if t.get("turn_id") != entry.get("turn_id")]
    recent_turns.insert(0, entry)
    state["current_turn"] = entry
    state["recent_turns"] = recent_turns[:20]
    _write_state(state)


def log_user_query(*, user_query: str, hotel_id: str | None = None) -> None:
    """Record a user question as soon as it is heard/typed for live demo UI."""
    global _pending_turn, _turn_counter

    text = user_query.strip()
    if not text:
        return

    with _lock:
        _turn_counter += 1
        _pending_turn = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "turn_id": _turn_counter,
            "hotel_id": hotel_id,
            "user_query": text,
            "rag_chunks": [],
            "rag_elapsed_ms": None,
            "bot_reply": None,
            "llm_elapsed_ms": None,
            "status": "heard",
        }
        _sync_current_turn(_pending_turn.copy())


def log_rag_context(
    *,
    hotel_id: str,
    user_query: str,
    chunks: list[dict],
    elapsed_ms: float,
) -> None:
    """Called by RAGContextInjector after retrieval."""
    global _pending_turn, _turn_counter

    with _lock:
        if _pending_turn is None or _pending_turn.get("bot_reply") is not None:
            _turn_counter += 1
            _pending_turn = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "turn_id": _turn_counter,
                "hotel_id": hotel_id,
                "user_query": user_query,
                "rag_chunks": chunks,
                "rag_elapsed_ms": round(elapsed_ms, 1),
                "bot_reply": None,
                "llm_elapsed_ms": None,
                "status": "thinking",
            }
        else:
            _pending_turn["hotel_id"] = hotel_id
            _pending_turn["user_query"] = user_query
            _pending_turn["rag_chunks"] = chunks
            _pending_turn["rag_elapsed_ms"] = round(elapsed_ms, 1)
            _pending_turn["status"] = "thinking"
        _sync_current_turn(_pending_turn.copy())

    # Console output for live debugging / evaluation
    logger.info(
        "[rag-log] Q: {!r}  →  {} chunks (top score {:.3f}, {:.0f}ms)",
        user_query,
        len(chunks),
        chunks[0]["score"] if chunks else 0.0,
        elapsed_ms,
    )
    for i, c in enumerate(chunks):
        preview = c["text"][:120].replace("\n", " ")
        logger.debug(
            "[rag-log]   #{} score={:.3f} doc={}: {}",
            i + 1,
            c["score"],
            c["doc_id"],
            preview,
        )


def log_bot_reply(*, reply: str, elapsed_ms: float | None = None) -> None:
    """Called by PipelineTracer when the LLM finishes a response."""
    global _pending_turn

    with _lock:
        if _pending_turn is not None:
            _pending_turn["bot_reply"] = reply
            _pending_turn["llm_elapsed_ms"] = round(elapsed_ms, 1) if elapsed_ms else None
            _pending_turn["status"] = "answered"
            _write_entry(_pending_turn)
            _finalize_turn(_pending_turn.copy())
            _pending_turn = None
        else:
            # Bot reply without a preceding RAG turn (e.g. greeting or
            # RAG-disabled mode). Still log it.
            _turn_counter_local = _turn_counter
            entry = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "turn_id": _turn_counter_local,
                "hotel_id": None,
                "user_query": None,
                "rag_chunks": [],
                "rag_elapsed_ms": None,
                "bot_reply": reply,
                "llm_elapsed_ms": round(elapsed_ms, 1) if elapsed_ms else None,
                "status": "answered",
            }
            _write_entry(entry)
            _finalize_turn(entry)

    logger.info("[conv-log] A: {!r}", reply[:200])
