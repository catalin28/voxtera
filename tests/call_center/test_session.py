"""Unit tests for the Redis-backed SessionStore (Phase 3).

Runs fully offline by injecting a minimal in-memory fake client that
mimics the subset of redis.asyncio used by SessionStore (``get`` /
``set`` / ``aclose``).
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.session import (
    DEFAULT_TTL_SECONDS,
    KEY_PREFIX,
    SessionStore,
    decide_language,
    detect_language_unlock,
    maybe_lock_language,
    new_session_id,
)


class _FakeRedis:
    """Captures set calls; .get returns whatever was last set."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.last_ex: int | None = None
        self.calls: list[tuple[str, str, str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        self.last_ex = ex
        self.calls.append(("set", key, value, ex or 0))

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_load_unknown_session_returns_empty_skeleton() -> None:
    store = SessionStore(client=_FakeRedis())
    s = await store.load("does-not-exist")
    assert s["session_id"] == "does-not-exist"
    assert s["turn_count"] == 0
    assert s["clarification_count"] == 0
    assert s["history"] == []
    assert s["pending_slots"] == []
    assert s["active_hotel_id"] is None


@pytest.mark.asyncio
async def test_save_then_load_roundtrip() -> None:
    fake = _FakeRedis()
    store = SessionStore(client=fake)
    sid = new_session_id()
    s = await store.load(sid)
    s["active_region"] = "antalya"
    s["language"] = "tr"
    await store.save(s)

    # Stored under prefixed key with default TTL.
    assert KEY_PREFIX + sid in fake.store
    assert fake.last_ex == DEFAULT_TTL_SECONDS

    s2 = await store.load(sid)
    assert s2["active_region"] == "antalya"
    assert s2["language"] == "tr"


@pytest.mark.asyncio
async def test_append_turn_increments_turn_count_and_resets_clarifications() -> None:
    store = SessionStore(client=_FakeRedis())
    s = await store.load("sid1")
    # 2 clarification turns
    await store.append_turn(s, utterance="ne?", decomposition=None,
                            reason="clarify_geography", answer="Hangi şehir?",
                            is_clarification=True)
    await store.append_turn(s, utterance="ne?", decomposition=None,
                            reason="clarify_intent", answer="Otel mi?",
                            is_clarification=True)
    assert s["clarification_count"] == 2
    assert s["turn_count"] == 0
    # full-answer turn resets clarification streak and bumps turn_count
    await store.append_turn(s, utterance="Belek otel",
                            decomposition={"requirements": ["spa"]},
                            reason=None, answer="Rixos Premium Belek...",
                            is_clarification=False)
    assert s["clarification_count"] == 0
    assert s["turn_count"] == 1
    assert len(s["history"]) == 3


@pytest.mark.asyncio
async def test_history_is_ring_buffer() -> None:
    store = SessionStore(client=_FakeRedis(), history_limit=3)
    s = await store.load("sid-ring")
    for i in range(6):
        await store.append_turn(s, utterance=f"u{i}", decomposition=None,
                                reason=None, answer=f"a{i}",
                                is_clarification=False)
    await store.save(s)
    s2 = await store.load("sid-ring")
    # save() trims to history_limit
    assert len(s2["history"]) == 3
    assert [h["utterance"] for h in s2["history"]] == ["u3", "u4", "u5"]


@pytest.mark.asyncio
async def test_falls_back_to_memory_when_no_client_and_no_url(monkeypatch: Any) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = SessionStore()  # no client, no URL -> in-memory dict
    sid = new_session_id()
    s = await store.load(sid)
    s["active_region"] = "izmir"
    await store.save(s)
    s2 = await store.load(sid)
    assert s2["active_region"] == "izmir"


@pytest.mark.asyncio
async def test_corrupt_payload_falls_back_to_empty() -> None:
    fake = _FakeRedis()
    fake.store[KEY_PREFIX + "sid-corrupt"] = "{not json"
    store = SessionStore(client=fake)
    s = await store.load("sid-corrupt")
    # Recovers as a fresh skeleton rather than raising.
    assert s["session_id"] == "sid-corrupt"
    assert s["history"] == []


# ---------------------------------------------------------------------------
# Phase 2 — language-lock helpers.


def test_decide_language_prefers_locked_over_decomposition_and_session() -> None:
    """Lock is the highest-priority source — a mis-detected per-turn language
    cannot flip the bot once the lock is engaged (Alo? bug)."""
    session = {"locked_language": "tr", "language": "fr"}
    decomposition = {"language": "fr"}
    assert decide_language(session, decomposition) == "tr"


def test_decide_language_falls_through_when_unlocked() -> None:
    """Without a lock: per-turn decomposition wins, then session, then default."""
    assert decide_language({"language": "en"}, {"language": "fr"}) == "fr"
    assert decide_language({"language": "en"}, None) == "en"
    assert decide_language(None, None) == "en"
    assert decide_language(None, None, default=None) is None


def test_maybe_lock_language_requires_substantive_utterance() -> None:
    """A one-word "Alo?" is not enough signal to lock — Gladia routinely
    mis-detects short noises."""
    session: dict[str, Any] = {}
    assert maybe_lock_language(session, "fr", "Alo?") is False
    assert session.get("locked_language") is None

    assert maybe_lock_language(session, "tr", "Merhaba spa hakkında bilgi") is True
    assert session["locked_language"] == "tr"

    # Already locked → second call is a no-op even with a substantive utterance.
    assert maybe_lock_language(session, "fr", "Bonjour je veux parler français") is False
    assert session["locked_language"] == "tr"


def test_detect_language_unlock_catches_explicit_switch_requests() -> None:
    """Deterministic regex on a small set of unambiguous phrases."""
    assert detect_language_unlock("please speak English") == "en"
    assert detect_language_unlock("ingilizce konuşalım") == "en"
    assert detect_language_unlock("türkçe lütfen") == "tr"
    assert detect_language_unlock("parlez en français") == "fr"
    # Casual mention does NOT unlock — only directive phrases.
    assert detect_language_unlock("My English is poor") is None
    assert detect_language_unlock("") is None
