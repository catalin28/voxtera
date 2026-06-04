"""Session state — Redis-backed conversation state for the call-center concierge (Phase 3).

Keeps per-conversation context across turns so triage can enforce the
two-turn-maximum rule, decomposition can inherit missing slots from
previous turns, and the source router can detect hotel-switch events.

Decision contract — session object stored in Redis:

    {
      "session_id":      str,
      "created_at":      float,        # unix epoch seconds
      "updated_at":      float,
      "active_hotel_id": str | None,   # set after first successful resolution
      "active_region":   str | None,
      "language":        str | None,   # ISO-639-1
      "turn_count":      int,          # number of completed turns
      "clarification_count": int,      # consecutive triage questions asked since last full answer
      "pending_slots":   list[str],    # decomposition slots the triage asked about
      "history": [                     # bounded ring buffer (last N turns)
        {
          "ts": float,
          "utterance": str,
          "decomposition": dict | None,
          "reason": str | None,        # retrieval / escalation reason
          "answer": str | None,
        }, ...
      ],
    }

Storage layout:

    Key:   voxtera:cc:session:{session_id}
    Value: JSON-encoded session object
    TTL:   30 minutes (refreshed on every write — chat-mode default)

The wrapper is async (uses ``redis.asyncio``) and dependency-injectable:
``SessionStore(client=fake_redis)`` for tests. When Redis is unreachable
the store falls back to an in-process dict so unit tests and local dev
don't require a live Redis.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from loguru import logger

DEFAULT_TTL_SECONDS = 30 * 60  # 30-minute chat-mode TTL (per architecture doc)
DEFAULT_HISTORY_LIMIT = 8       # ring buffer for recent turns
KEY_PREFIX = "voxtera:cc:session:"


def _now() -> float:
    return time.time()


def new_session_id() -> str:
    """Generate a fresh URL-safe session id."""
    return uuid.uuid4().hex


def _empty_session(session_id: str) -> dict[str, Any]:
    now = _now()
    return {
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "active_hotel_id": None,
        "active_region": None,
        "language": None,
        "turn_count": 0,
        "clarification_count": 0,
        "pending_slots": [],
        "history": [],
    }


class SessionStore:
    """Async Redis-backed session store with in-memory fallback."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        url: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._client = client
        self._url = url or os.environ.get("REDIS_URL", "")
        self._ttl = ttl_seconds
        self._history_limit = history_limit
        self._fallback: dict[str, dict[str, Any]] = {}
        self._fallback_warned = False

    async def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self._url:
            return None
        try:
            import redis.asyncio as aioredis  # type: ignore

            self._client = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True,
                socket_connect_timeout=2.0, socket_timeout=2.0,
            )
            return self._client
        except Exception as e:  # noqa: BLE001
            if not self._fallback_warned:
                logger.warning("SessionStore: Redis unavailable ({}), using in-memory fallback", e)
                self._fallback_warned = True
            return None

    async def load(self, session_id: str) -> dict[str, Any]:
        """Load session by id, or return a fresh empty session if missing/expired."""
        if not session_id:
            return _empty_session(new_session_id())
        client = await self._get_client()
        if client is None:
            return self._fallback.get(session_id) or _empty_session(session_id)
        try:
            raw = await client.get(KEY_PREFIX + session_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("SessionStore.load failed for {}: {}", session_id, e)
            return self._fallback.get(session_id) or _empty_session(session_id)
        if not raw:
            return _empty_session(session_id)
        try:
            obj = json.loads(raw)
            obj.setdefault("history", [])
            obj.setdefault("pending_slots", [])
            return obj
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("SessionStore.load: corrupt JSON for {}: {}", session_id, e)
            return _empty_session(session_id)

    async def save(self, session: dict[str, Any]) -> None:
        """Persist session with TTL refresh; trim history ring."""
        session["updated_at"] = _now()
        if len(session.get("history", [])) > self._history_limit:
            session["history"] = session["history"][-self._history_limit :]
        sid = session.get("session_id")
        if not sid:
            return
        client = await self._get_client()
        payload = json.dumps(session, ensure_ascii=False)
        if client is None:
            self._fallback[sid] = session
            return
        try:
            await client.set(KEY_PREFIX + sid, payload, ex=self._ttl)
        except Exception as e:  # noqa: BLE001
            logger.warning("SessionStore.save failed for {}: {}", sid, e)
            self._fallback[sid] = session

    async def append_turn(
        self,
        session: dict[str, Any],
        *,
        utterance: str,
        decomposition: dict[str, Any] | None,
        reason: str | None,
        answer: str | None,
        is_clarification: bool = False,
    ) -> None:
        """Append a turn to history and update counters; caller must still call ``save``."""
        session.setdefault("history", []).append({
            "ts": _now(),
            "utterance": utterance,
            "decomposition": decomposition,
            "reason": reason,
            "answer": answer,
            "is_clarification": is_clarification,
        })
        if is_clarification:
            session["clarification_count"] = int(session.get("clarification_count", 0)) + 1
        else:
            # Full answer turn — reset the clarification streak.
            session["clarification_count"] = 0
            session["turn_count"] = int(session.get("turn_count", 0)) + 1

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
