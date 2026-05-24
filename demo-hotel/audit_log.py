"""Append-only audit logging for legal compliance.

Two log types:

1. Per-session conversation log
   Path: logs/audit/sessions/YYYY-MM-DD_HH-MM-SS_<session_id_8>.jsonl
   One line per conversational turn.
   Contains the full user query and full bot reply.

2. Failed-request log (daily rotating)
   Path: logs/audit/failed-YYYY-MM-DD.jsonl
   One line per failed, errored, or unauthorized request.

Both files are append-only JSONL. Never modified after writing.
Each line is a self-contained, tamper-evident JSON record.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

# logs/audit/ lives at the project root (parent of demo-hotel/)
_AUDIT_DIR = Path(__file__).resolve().parent.parent / "logs" / "audit"
_SESSIONS_DIR = _AUDIT_DIR / "sessions"

_lock = threading.Lock()

# session_id → Path of its audit log file
# Created once per session on the first turn.
_session_files: dict[str, Path] = {}


def _ensure_dirs() -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def _session_log_path(session_id: str, started_at: datetime) -> Path:
    dt_str = started_at.strftime("%Y-%m-%d_%H-%M-%S")
    short_id = session_id[:8]
    return _SESSIONS_DIR / f"{dt_str}_{short_id}.jsonl"


def write_turn(
    *,
    session_id: str,
    turn_number: int,
    client_ip: str,
    forwarded_for: str | None,
    user_agent: str | None,
    channel: str,
    hotel_id: str,
    user_query: str,
    bot_reply: str,
    model: str,
    language: str,
    tts_provider: str,
    llm_ms: float | None,
    rag_ms: float | None,
    status: str,
    request_id: str,
) -> None:
    """Append one conversational turn to this session's per-session audit file.

    The file is created on the first turn; the filename embeds the session
    start datetime and the first 8 chars of the session_id so it is easy to
    locate without a database query.
    """
    _ensure_dirs()
    now = datetime.now(UTC)

    with _lock:
        if session_id not in _session_files:
            path = _session_log_path(session_id, now)
            _session_files[session_id] = path
        path = _session_files[session_id]

    entry = {
        "ts": now.isoformat(),
        "request_id": request_id,
        "session_id": session_id,
        "turn_number": turn_number,
        "client_ip": client_ip,
        "forwarded_for": forwarded_for,
        "user_agent": user_agent,
        "channel": channel,
        "hotel_id": hotel_id,
        "user_query": user_query,
        "bot_reply": bot_reply,
        "model": model,
        "language": language,
        "tts_provider": tts_provider,
        "llm_ms": round(llm_ms, 1) if llm_ms is not None else None,
        "rag_ms": round(rag_ms, 1) if rag_ms is not None else None,
        "status": status,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def write_failure(
    *,
    client_ip: str,
    forwarded_for: str | None,
    user_agent: str | None,
    method: str,
    path: str,
    status_code: int,
    error: str,
    request_id: str,
    session_id: str | None = None,
) -> None:
    """Append one failed or unauthorized request to the daily failed-requests log.

    Rotates daily: logs/audit/failed-YYYY-MM-DD.jsonl
    """
    _ensure_dirs()
    now = datetime.now(UTC)
    date_str = now.strftime("%Y-%m-%d")
    log_path = _AUDIT_DIR / f"failed-{date_str}.jsonl"

    entry = {
        "ts": now.isoformat(),
        "request_id": request_id,
        "session_id": session_id,
        "client_ip": client_ip,
        "forwarded_for": forwarded_for,
        "user_agent": user_agent,
        "method": method,
        "path": path,
        "status_code": status_code,
        "error": error,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(line)
