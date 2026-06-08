"""Unit tests for the Phase 3 ConciergeAgent (decompose -> compound -> render).

All LLM calls are stubbed via ``decompose_fn`` / ``render_fn`` injection,
and the underlying CompoundAndDiscovery is injected as a fake so these
tests run fully offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera.call_center.concierge import (
    REASON_DECOMPOSE_ERROR,
    REASON_EMPTY_UTTERANCE,
    REASON_NO_REGION_SCOPE,
    REASON_RENDER_ERROR,
    ConciergeAgent,
)

REGION = "antalya"


class _FakeCompound:
    """Stand-in for CompoundAndDiscovery — records the call and returns a scripted result."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def discover(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._result


def _ok_retrieval() -> dict[str, Any]:
    return {
        "region": REGION,
        "requirements": ["spa", "scuba diving"],
        "normalized_requirements": ["spa", "scuba diving"],
        "top_score": 0.81,
        "count": 1,
        "hotels": [{
            "hotel_id": "hotel_aqua",
            "score": 0.81,
            "payload": {"hotel_name": "Hotel Aqua"},
            "evidence": {
                "spa": {"text_en": "Full-service spa with hammam.", "score": 0.80},
                "scuba diving": {"text_en": "PADI dive center on site.", "score": 0.82},
            },
        }],
        "missing_requirements": [],
        "reason": None,
    }


# ---------------- Short-circuits ----------------

class TestShortCircuits:
    async def test_empty_utterance(self) -> None:
        agent = ConciergeAgent(
            compound=_FakeCompound(_ok_retrieval()),
            decompose_fn=_unused, render_fn=_unused,
        )
        result = await agent.answer(utterance="   ", region=REGION)
        assert result["reason"] == REASON_EMPTY_UTTERANCE
        assert result["decomposition"] is None
        assert result["retrieval"] is None
        assert result["answer"]

    async def test_empty_region(self) -> None:
        agent = ConciergeAgent(
            compound=_FakeCompound(_ok_retrieval()),
            decompose_fn=_unused, render_fn=_unused,
        )
        result = await agent.answer(utterance="I want a spa", region="")
        assert result["reason"] == REASON_NO_REGION_SCOPE


# ---------------- Happy path ----------------

class TestHappyPath:
    async def test_decompose_then_compound_then_render(self) -> None:
        fake_compound = _FakeCompound(_ok_retrieval())

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            return {
                "requirements": ["spa", "scuba diving"],
                "activity_tags": ["diving"],
                "category_hint": None,
                "language": "en",
            }

        seen: dict[str, Any] = {}

        async def render(payload: dict[str, Any]) -> str:
            seen["payload"] = payload
            return "Hotel Aqua matches both your spa and scuba needs."

        agent = ConciergeAgent(
            compound=fake_compound, decompose_fn=decompose, render_fn=render,
        )
        out = await agent.answer(
            utterance="I want a spa and scuba diving in Antalya",
            region=REGION,
        )

        assert out["reason"] is None
        assert out["answer"].startswith("Hotel Aqua")
        # Decomposition flowed into compound.discover correctly.
        assert fake_compound.calls == [{
            "region": REGION,
            "requirements": ["spa", "scuba diving"],
            "activity_tags": ["diving"],
            "category_hint": None,
        }]
        # Render saw the trimmed retrieval payload.
        assert seen["payload"]["retrieval"]["hotels"][0]["hotel_id"] == "hotel_aqua"
        assert seen["payload"]["decomposition"]["language"] == "en"


# ---------------- Failure modes ----------------

class TestFailures:
    async def test_decompose_exception_short_circuits(self) -> None:
        fake_compound = _FakeCompound(_ok_retrieval())

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            raise RuntimeError("anthropic 500")

        agent = ConciergeAgent(
            compound=fake_compound, decompose_fn=decompose, render_fn=_unused,
        )
        out = await agent.answer(utterance="I want a spa", region=REGION)
        assert out["reason"] == REASON_DECOMPOSE_ERROR
        assert fake_compound.calls == []  # compound was not called

    async def test_render_exception_keeps_retrieval(self) -> None:
        fake_compound = _FakeCompound(_ok_retrieval())

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            return {"requirements": ["spa"], "language": "en"}

        async def render(_p: dict[str, Any]) -> str:
            raise RuntimeError("anthropic timeout")

        agent = ConciergeAgent(
            compound=fake_compound, decompose_fn=decompose, render_fn=render,
        )
        out = await agent.answer(utterance="I want a spa", region=REGION)
        assert out["reason"] == REASON_RENDER_ERROR
        assert out["retrieval"]["hotels"], "retrieval should still be exposed for debugging"


# ---------------- Retrieval-reason passthrough ----------------

class TestReasonPassthrough:
    async def test_passes_partial_match(self) -> None:
        partial = {
            **_ok_retrieval(),
            "reason": "partial_match_only",
            "missing_requirements": ["scuba diving"],
        }
        fake_compound = _FakeCompound(partial)

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            return {"requirements": ["spa", "scuba diving"], "language": "en"}

        async def render(_p: dict[str, Any]) -> str:
            return "Found spa but no diving."

        agent = ConciergeAgent(compound=fake_compound, decompose_fn=decompose, render_fn=render)
        out = await agent.answer(utterance="spa + diving", region=REGION)
        assert out["reason"] == "partial_match_only"
        assert out["retrieval"]["missing_requirements"] == ["scuba diving"]


# ---------------- Decompose output sanitization ----------------

class TestDecompositionLimits:
    async def test_caps_requirements_to_max(self) -> None:
        fake_compound = _FakeCompound(_ok_retrieval())

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            return {
                "requirements": ["a", "b", "c", "d", "e", "f", "g"],
                "language": "en",
            }

        async def render(_p: dict[str, Any]) -> str:
            return "ok"

        agent = ConciergeAgent(
            compound=fake_compound, decompose_fn=decompose, render_fn=render,
            max_requirements=3,
        )
        await agent.answer(utterance="many things", region=REGION)
        assert fake_compound.calls[0]["requirements"] == ["a", "b", "c"]

    async def test_missing_optional_fields_default_to_none(self) -> None:
        fake_compound = _FakeCompound(_ok_retrieval())

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            return {"requirements": ["spa"]}  # no language, tags, category

        async def render(_p: dict[str, Any]) -> str:
            return "ok"

        agent = ConciergeAgent(
            compound=fake_compound, decompose_fn=decompose, render_fn=render,
        )
        await agent.answer(utterance="spa", region=REGION)
        call = fake_compound.calls[0]
        assert call["activity_tags"] is None
        assert call["category_hint"] is None


# ---------------- Timings instrumentation (Phase 3c) ----------------

class TestTimings:
    async def test_happy_path_records_all_three_stages(self) -> None:
        fake_compound = _FakeCompound(_ok_retrieval())

        async def decompose(_u: str, _r: str) -> dict[str, Any]:
            return {"requirements": ["spa"], "language": "en"}

        async def render(_p: dict[str, Any]) -> str:
            return "ok"

        agent = ConciergeAgent(compound=fake_compound, decompose_fn=decompose, render_fn=render)
        out = await agent.answer(utterance="spa please", region=REGION)
        t = out["timings"]
        assert {"decompose_ms", "retrieve_ms", "render_ms", "total_ms"} <= set(t.keys())
        for v in t.values():
            assert isinstance(v, float) and v >= 0.0
        # total should be >= sum of stage timings minus rounding slop
        assert t["total_ms"] + 1.0 >= t["decompose_ms"] + t["retrieve_ms"] + t["render_ms"]

    async def test_short_circuit_records_only_total(self) -> None:
        agent = ConciergeAgent(
            compound=_FakeCompound(_ok_retrieval()),
            decompose_fn=_unused, render_fn=_unused,
        )
        out = await agent.answer(utterance="", region=REGION)
        assert out["timings"] == {"total_ms": pytest.approx(out["timings"]["total_ms"])}
        assert "decompose_ms" not in out["timings"]


# ---------------- helper ----------------

async def _unused(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("should not be called")


pytestmark = pytest.mark.asyncio
