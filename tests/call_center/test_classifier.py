"""Unit tests for the EscalationClassifier (Phase 3).

LLM calls are stubbed via ``classify_fn`` injection so tests run
offline. The cache is exercised via in-memory dict shims.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.classifier import (
    CACHE_KEY_PREFIX,
    EscalationClassifier,
)


def _scripted(verdicts: list[dict[str, Any]]) -> Any:
    """Build a classify_fn that returns the next scripted verdict each call."""
    state = {"i": 0, "calls": []}

    async def classify(utterance: str) -> dict[str, Any]:
        state["calls"].append(utterance)
        v = verdicts[state["i"]]
        state["i"] = min(state["i"] + 1, len(verdicts) - 1)
        return v

    classify.state = state  # type: ignore[attr-defined]
    return classify


@pytest.mark.asyncio
async def test_empty_utterance_returns_no_escalation() -> None:
    clf = EscalationClassifier(classify_fn=_scripted([{"type": "medical", "confidence": 1.0}]))
    v = await clf.classify("")
    assert v["escalate"] is False
    assert v["escalation_type"] is None


@pytest.mark.asyncio
async def test_high_confidence_live_complaint_escalates() -> None:
    clf = EscalationClassifier(
        classify_fn=_scripted(
            [{"type": "live_complaint", "confidence": 0.92, "signal": "odama giremiyorum"}]
        ),
    )
    v = await clf.classify("Oteldeyim ve odama giremiyorum")
    assert v["escalate"] is True
    assert v["escalation_type"] == "live_complaint"
    assert v["signal"] == "odama giremiyorum"
    assert v["confidence"] == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_low_confidence_does_not_escalate_even_with_category() -> None:
    clf = EscalationClassifier(
        classify_fn=_scripted([{"type": "booking", "confidence": 0.40}]),
        min_confidence=0.55,
    )
    # "book" passes the fast gate so the (scripted) LLM verdict is exercised.
    v = await clf.classify("Could I book one of your rooms?")
    assert v["escalate"] is False
    assert v["escalation_type"] is None


@pytest.mark.asyncio
async def test_unknown_category_treated_as_none() -> None:
    clf = EscalationClassifier(
        classify_fn=_scripted([{"type": "weather_chat", "confidence": 0.9}]),
    )
    # "urgent" passes the fast gate; the scripted LLM returns a junk category.
    v = await clf.classify("Urgent — how's the weather in Belek?")
    assert v["escalate"] is False
    assert v["escalation_type"] is None


@pytest.mark.asyncio
async def test_llm_exception_returns_safe_default() -> None:
    async def boom(_u: str) -> dict[str, Any]:
        raise RuntimeError("network down")

    clf = EscalationClassifier(classify_fn=boom)
    # "rezerv"/"iptal" pass the fast gate so the exploding LLM fn is reached.
    v = await clf.classify("Rezervasyonumu iptal etmek istiyorum.")
    assert v["escalate"] is False
    assert v["escalation_type"] is None
    assert v["latency_ms"] >= 0.0


@pytest.mark.asyncio
async def test_fast_gate_skips_llm_for_benign_utterance() -> None:
    """A plain Q&A turn with no escalation stem must NOT call the LLM at all."""
    classify_fn = _scripted([{"type": "medical", "confidence": 1.0}])
    clf = EscalationClassifier(classify_fn=classify_fn)
    v = await clf.classify("do they have a spa and a pool?")
    assert v["escalate"] is False
    assert v["model"] == "fast_gate"
    assert classify_fn.state["calls"] == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fast_gate_triggers_llm_when_stem_present() -> None:
    """Any stem (here Turkish 'iptal') wakes the real LLM classifier."""
    classify_fn = _scripted([{"type": "post_booking", "confidence": 0.9, "signal": "iptal"}])
    clf = EscalationClassifier(classify_fn=classify_fn)
    v = await clf.classify("Rezervasyonumu iptal edebilir miyim?")
    assert v["escalate"] is True
    assert v["escalation_type"] == "post_booking"
    assert len(classify_fn.state["calls"]) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fast_gate_fails_open_when_stems_file_missing(monkeypatch) -> None:
    """Missing/broken stems file disables the gate — every turn runs the LLM."""
    monkeypatch.setenv("ESCALATION_STEMS_PATH", "/nonexistent/stems.json")
    classify_fn = _scripted([{"type": "none", "confidence": 0.1}])
    clf = EscalationClassifier(classify_fn=classify_fn)
    v = await clf.classify("do they have a spa and a pool?")
    assert v["escalate"] is False
    assert len(classify_fn.state["calls"]) == 1  # type: ignore[attr-defined]
    monkeypatch.delenv("ESCALATION_STEMS_PATH")


@pytest.mark.asyncio
async def test_redis_cache_short_circuits_repeat_calls() -> None:
    cache: dict[str, str] = {}

    async def cache_get(k: str) -> str | None:
        return cache.get(k)

    async def cache_set(k: str, v: str, ttl: int) -> None:
        cache[k] = v

    classify_fn = _scripted([{"type": "medical", "confidence": 0.95, "signal": "ambulans"}])
    clf = EscalationClassifier(
        classify_fn=classify_fn,
        cache_get=cache_get,
        cache_set=cache_set,
    )
    v1 = await clf.classify("Ambulans çağırın!")
    v2 = await clf.classify("Ambulans çağırın!")
    assert v1["escalate"] is True and v2["escalate"] is True
    # Second call should be a cache hit -> classify_fn called only once.
    assert len(classify_fn.state["calls"]) == 1  # type: ignore[attr-defined]
    # Cached value carries latency_ms=0 (hit marker).
    assert v2["latency_ms"] == 0.0
    # Cache key has the right prefix.
    assert any(k.startswith(CACHE_KEY_PREFIX) for k in cache)


@pytest.mark.asyncio
async def test_signal_is_optional_and_normalized() -> None:
    clf = EscalationClassifier(
        classify_fn=_scripted([{"type": "urgency", "confidence": 0.7, "signal": "   "}]),
    )
    v = await clf.classify("Hemen lazım!")
    assert v["escalate"] is True
    assert v["signal"] is None
