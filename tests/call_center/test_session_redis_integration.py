"""Opt-in real-Redis integration tests for SessionStore.

These tests hit the live Redis instance from REDIS_URL (in .env). They are
**skipped automatically** if:
  - REDIS_URL is not set, OR
  - the redis package is not installed, OR
  - the server is not reachable within a short timeout.

Keys are written under a dedicated test prefix (``voxtera:cc:itest:``) and
explicitly cleaned up so we never pollute production session data.

Run only this file with:  pytest tests/call_center/test_session_redis_integration.py -q
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from dotenv import dotenv_values

# Read REDIS_URL for the live tests WITHOUT mutating os.environ. pytest
# imports this module during collection, so a load_dotenv() here used to
# leak the production REDIS_URL into the whole test process — every
# "in-memory" SessionStore() in OTHER test modules silently connected to
# the production Redis, and fixed test session ids accumulated turns
# across runs (flaky history-length asserts, polluted prod data).
REDIS_URL = os.environ.get("REDIS_URL") or dotenv_values().get("REDIS_URL") or ""


def _reachable(url: str, timeout: float = 2.0) -> bool:
    if not url:
        return False
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
        return bool(client.ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(REDIS_URL),
    reason=f"Redis at {REDIS_URL!r} not reachable — skipping live integration tests",
)


# Import after the skipif so the module never errors when redis isn't installed.
from voxtera.call_center.session import (  # noqa: E402
    KEY_PREFIX,
    SessionStore,
)


@pytest.fixture
def itest_prefix() -> str:
    """Unique prefix for this test run; cleaned up in cleanup_keys."""
    return f"itest-{uuid.uuid4().hex[:8]}-"


@pytest.fixture
async def cleanup_keys(itest_prefix: str):
    """Yield, then DEL every key produced during the test."""
    created: list[str] = []
    yield created

    import redis.asyncio as aioredis  # type: ignore

    client = aioredis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        for sid in created:
            await client.delete(KEY_PREFIX + sid)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_live_redis_roundtrip(itest_prefix: str, cleanup_keys: list[str]) -> None:
    """Save + load against the real Redis instance, then verify TTL is set."""
    store = SessionStore(url=REDIS_URL, ttl_seconds=60)
    sid = itest_prefix + "roundtrip"
    cleanup_keys.append(sid)

    s = await store.load(sid)
    s["active_region"] = "antalya"
    s["language"] = "tr"
    s["active_hotel_id"] = "rixos_premium_belek"
    await store.save(s)

    s2 = await store.load(sid)
    assert s2["active_region"] == "antalya"
    assert s2["language"] == "tr"
    assert s2["active_hotel_id"] == "rixos_premium_belek"

    # TTL should be set (positive integer seconds, ≤ 60).
    import redis.asyncio as aioredis  # type: ignore

    client = aioredis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    try:
        ttl = await client.ttl(KEY_PREFIX + sid)
    finally:
        await client.aclose()
    assert 0 < ttl <= 60

    await store.close()


@pytest.mark.asyncio
async def test_live_redis_append_turn_persists(
    itest_prefix: str,
    cleanup_keys: list[str],
) -> None:
    """Verify append_turn + save survives a fresh SessionStore instance."""
    store = SessionStore(url=REDIS_URL, ttl_seconds=60)
    sid = itest_prefix + "append"
    cleanup_keys.append(sid)

    s = await store.load(sid)
    await store.append_turn(
        s,
        utterance="Belek'te spa olan otel?",
        decomposition={"requirements": ["spa"]},
        reason=None,
        answer="Rixos Premium Belek...",
        is_clarification=False,
    )
    await store.save(s)
    await store.close()

    # Fresh store instance — must read the same data back from Redis.
    store2 = SessionStore(url=REDIS_URL)
    s2 = await store2.load(sid)
    assert s2["turn_count"] == 1
    assert len(s2["history"]) == 1
    assert s2["history"][0]["utterance"] == "Belek'te spa olan otel?"
    await store2.close()


@pytest.mark.asyncio
async def test_live_redis_ttl_expiry_removes_key(
    itest_prefix: str,
    cleanup_keys: list[str],
) -> None:
    """Use a 1-second TTL and confirm the key disappears."""
    store = SessionStore(url=REDIS_URL, ttl_seconds=1)
    sid = itest_prefix + "ttl"
    cleanup_keys.append(sid)

    s = await store.load(sid)
    s["language"] = "en"
    await store.save(s)

    # Confirm present.
    s_now = await store.load(sid)
    assert s_now["language"] == "en"

    await asyncio.sleep(1.5)

    # After TTL, load returns a fresh empty skeleton (Redis evicted the key).
    s_after = await store.load(sid)
    assert s_after["language"] is None
    assert s_after["turn_count"] == 0

    await store.close()
