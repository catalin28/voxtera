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
      "language":        str | None,   # ISO-639-1, last detected (per-turn signal)
      "locked_language": str | None,   # ISO-639-1, sticky once we have a confident signal
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

import contextlib
import json
import os
import time
import uuid
from typing import Any

from loguru import logger

DEFAULT_TTL_SECONDS = 30 * 60  # 30-minute chat-mode TTL (per architecture doc)
# Keep the FULL conversation per session (a voice call is one session). The cap
# is only a runaway-safety bound, not a 2-3-turn window — real dialogue needs the
# whole history so follow-ups ("the other one", "yes", "what did I ask?") resolve.
DEFAULT_HISTORY_LIMIT = 500
KEY_PREFIX = "voxtera:cc:session:"

# Budget for how much transcript we FEED the LLM each turn (storage stays full;
# this only bounds tokens/latency on very long calls — newest turns are kept).
DEFAULT_TRANSCRIPT_CHAR_BUDGET = 8000


def build_transcript(
    history: list[dict[str, Any]] | None,
    *,
    char_budget: int = DEFAULT_TRANSCRIPT_CHAR_BUDGET,
    include_current: bool = True,
) -> str:
    """Render session history into a plain 'User:/Assistant:' transcript.

    Newest-first budgeting: if the full transcript exceeds `char_budget`, the
    OLDEST turns are dropped (the recent context matters most for a follow-up),
    but the stored history in Redis is never trimmed by this function.

    NOTE: This string form is for components that need a flat text view of the
    conversation (e.g. the decomposer's classifier input). For the chat-style
    Anthropic Messages API, use :func:`build_message_turns` — passing the
    transcript inline as ``User:`` / ``Assistant:`` headers teaches the model
    to autocomplete the scaffold and was the cause of the P0 prompt-leak.
    """
    turns = list(history or [])
    if not include_current and turns:
        turns = turns[:-1]
    lines: list[str] = []
    total = 0
    for turn in reversed(turns):  # newest first, so we keep recent under budget
        u = (turn.get("utterance") or "").strip()
        a = (turn.get("answer") or "").strip()
        block = ""
        if u:
            block += f"User: {u}\n"
        if a:
            block += f"Assistant: {a}\n"
        if not block:
            continue
        if total + len(block) > char_budget and lines:
            break
        lines.append(block)
        total += len(block)
    return "".join(reversed(lines)).strip()


def build_message_turns(
    history: list[dict[str, Any]] | None,
    *,
    char_budget: int = DEFAULT_TRANSCRIPT_CHAR_BUDGET,
    include_current: bool = False,
) -> list[dict[str, str]]:
    """Render session history as role-separated messages for the Anthropic API.

    Returns ``[{"role": "user", "content": ...}, {"role": "assistant", ...}, ...]``
    in chronological order. Pair this with a final ``{"role": "user"}`` carrying
    the current turn's payload so Claude sees structured chat history instead of
    a hand-built ``User:`` / ``Assistant:`` text scaffold that it learns to
    continue (the P0 leak that fabricated bookings).

    Budgeting matches :func:`build_transcript` — newest turns kept, oldest
    dropped once ``char_budget`` is exceeded.
    """
    turns = list(history or [])
    if not include_current and turns:
        turns = turns[:-1]
    selected: list[dict[str, Any]] = []
    total = 0
    for turn in reversed(turns):  # newest first for budgeting
        u = (turn.get("utterance") or "").strip()
        a = (turn.get("answer") or "").strip()
        if not u and not a:
            continue
        cost = len(u) + len(a)
        if total + cost > char_budget and selected:
            break
        selected.append(turn)
        total += cost
    out: list[dict[str, str]] = []
    for turn in reversed(selected):  # back to chronological order
        u = (turn.get("utterance") or "").strip()
        a = (turn.get("answer") or "").strip()
        if u:
            out.append({"role": "user", "content": u})
        if a:
            out.append({"role": "assistant", "content": a})
    return out


# Minimum token count for an utterance to confidently lock the conversation
# language. Short utterances like "Hello?", "Alo?", "Yes" are unreliable
# language signals (Gladia routinely flips on them) so we keep the lock open
# until we see something substantive.
LANGUAGE_LOCK_MIN_TOKENS = 3

# Deterministic unlock requests — guest explicitly asks to switch language.
# Keep this list small and high-precision; false positives silently break the
# lock. Matched case-insensitively against the raw utterance.
_UNLOCK_PATTERNS: tuple[tuple[str, str], ...] = (
    # English
    (r"\b(speak|switch|talk)\s+(in\s+)?english\b", "en"),
    (r"\bin\s+english\s+please\b", "en"),
    (r"\benglish\s+please\b", "en"),
    # Turkish
    (r"\bingilizce\s+(konu[sş]|l[uü]tfen)", "en"),
    (r"\bt[uü]rk[cç]e\s+(konu[sş]|l[uü]tfen)", "tr"),
    # French
    (r"\b(parlez|parler)\s+(en\s+)?fran[cç]ais\b", "fr"),
    (r"\ben\s+fran[cç]ais\s+s.?il\s+vous\s+pla[iî]t\b", "fr"),
    # Spanish
    (r"\b(habl|hablemos)\s+(en\s+)?espa[nñ]ol\b", "es"),
    # Russian
    (r"\bпо[\s-]?русски\b", "ru"),
    # Arabic
    (r"\bبالعربي(?:ة)?\b", "ar"),
)


def detect_language_unlock(utterance: str) -> str | None:
    """Return the requested language code if the utterance is an explicit
    switch request, else None. Used to break a sticky ``locked_language``.
    """
    import re

    if not utterance:
        return None
    text = utterance.lower()
    for pattern, lang in _UNLOCK_PATTERNS:
        if re.search(pattern, text):
            return lang
    return None


def decide_language(
    session: dict[str, Any] | None,
    decomposition: dict[str, Any] | None = None,
    *,
    default: str | None = "en",
) -> str | None:
    """Pick the language for THIS turn's reply.

    Precedence (highest first):
      1. ``session["locked_language"]`` — set once we have a confident signal,
         so subsequent short or mis-detected utterances ("Alo?", "Yes?") cannot
         flip the bot mid-call.
      2. ``decomposition["language"]`` — the per-turn STT/decomposer signal,
         used while the lock is still open.
      3. ``session["language"]`` — last-known sticky default.
      4. ``default`` (typically ``"en"``; pass ``None`` for retrieval call sites
         that prefer the index's own fallback chain when no signal exists).
    """
    sess = session or {}
    locked = sess.get("locked_language")
    if locked:
        return str(locked).lower()
    if decomposition and decomposition.get("language"):
        return str(decomposition["language"]).lower()
    if sess.get("language"):
        return str(sess["language"]).lower()
    return default


def maybe_lock_language(
    session: dict[str, Any],
    detected_language: str | None,
    utterance: str,
    *,
    min_tokens: int = LANGUAGE_LOCK_MIN_TOKENS,
) -> bool:
    """Set ``session["locked_language"]`` once we have a confident signal.

    Returns True if the lock was just established (caller may log it). Does
    nothing if the lock is already set or the inputs are too weak to be
    reliable — see ``LANGUAGE_LOCK_MIN_TOKENS`` for the threshold.
    """
    if session is None or session.get("locked_language"):
        return False
    if not detected_language:
        return False
    token_count = len((utterance or "").split())
    if token_count < min_tokens:
        return False
    session["locked_language"] = str(detected_language).lower()
    return True


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
        # The active hotel's REAL location (district/region from its payload).
        # Anchors "near the hotel" recommendations to where the hotel actually
        # is, not to a region the conversation merely discussed (D19).
        "active_hotel_location": None,
        "active_region": None,
        "language": None,
        "locked_language": None,
        "turn_count": 0,
        "clarification_count": 0,
        "pending_slots": [],
        # Last presented hotel list [{hotel_id, name}] — referent for
        # follow-ups like "the first one" / "compare those two" (D9/D10).
        "last_results": [],
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
                self._url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
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
        session.setdefault("history", []).append(
            {
                "ts": _now(),
                "utterance": utterance,
                "decomposition": decomposition,
                "reason": reason,
                "answer": answer,
                "is_clarification": is_clarification,
            }
        )
        if is_clarification:
            session["clarification_count"] = int(session.get("clarification_count", 0)) + 1
        else:
            # Full answer turn — reset the clarification streak.
            session["clarification_count"] = 0
            session["turn_count"] = int(session.get("turn_count", 0)) + 1

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            with contextlib.suppress(Exception):
                await self._client.aclose()
