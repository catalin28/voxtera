"""Mock smoke harness for the Phase 3 ConciergeAgent.

Fully offline — both LLM steps are stubbed with deterministic
functions, and the CompoundAndDiscovery layer is replaced by a fake
that returns scripted retrieval payloads. Verifies that the agent
correctly:

  - short-circuits on empty utterance / empty region
  - routes decomposition output into compound.discover()
  - passes through the retrieval reason (None / partial / no_match)
  - tolerates decompose/render failures without crashing

Run:
    .\\.venv\\Scripts\\python.exe scripts\\smoke_concierge.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from voxtera.call_center.concierge import (
    REASON_DECOMPOSE_ERROR,
    REASON_EMPTY_UTTERANCE,
    REASON_NO_REGION_SCOPE,
    ConciergeAgent,
)

REGION = "Turkish Riviera"


class _FakeCompound:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def discover(self, **_kw: Any) -> dict[str, Any]:
        return self.payload


def _hit(hotel_id: str, score: float, requirements: list[str]) -> dict[str, Any]:
    return {
        "hotel_id": hotel_id,
        "score": score,
        "payload": {"hotel_name": hotel_id.replace("_", " ").title()},
        "evidence": {
            r: {"text_en": f"{hotel_id} has {r}.", "score": score}
            for r in requirements
        },
    }


def _retrieval(reason: str | None, hotels: list[dict[str, Any]],
               requirements: list[str], missing: list[str] | None = None) -> dict[str, Any]:
    return {
        "region": REGION,
        "requirements": requirements,
        "normalized_requirements": requirements,
        "top_score": hotels[0]["score"] if hotels else 0.0,
        "count": len(hotels),
        "hotels": hotels,
        "missing_requirements": missing or [],
        "reason": reason,
    }


SCENARIOS = [
    # (label, utterance, region, decompose_out, retrieval_payload,
    #  expect_reason, expect_compound_called)
    ("happy: spa+diving full match",
     "I want a spa and scuba diving in Antalya", REGION,
     {"requirements": ["spa wellness", "scuba diving"], "language": "en"},
     _retrieval(None, [_hit("hotel_aqua", 0.82, ["spa wellness", "scuba diving"])],
                ["spa wellness", "scuba diving"]),
     None, True),

    ("partial: missing diving",
     "spa + diving please", REGION,
     {"requirements": ["spa", "scuba diving"], "language": "en"},
     _retrieval("partial_match_only", [_hit("hotel_calm", 0.80, ["spa"])],
                ["spa", "scuba diving"], missing=["scuba diving"]),
     "partial_match_only", True),

    ("no_match: nothing fits",
     "underwater spaceship", REGION,
     {"requirements": ["underwater spaceship"], "language": "en"},
     _retrieval("no_match_above_threshold", [], ["underwater spaceship"]),
     "no_match_above_threshold", True),

    ("short-circuit: empty utterance",
     "   ", REGION,
     None,  # decompose should NOT be called
     None,
     REASON_EMPTY_UTTERANCE, False),

    ("short-circuit: empty region",
     "I want a spa", "  ",
     None, None,
     REASON_NO_REGION_SCOPE, False),

    ("failure: decompose raises -> short-circuits before compound",
     "I want a spa", REGION,
     RuntimeError("anthropic 500"),
     _retrieval(None, [_hit("never", 0.9, ["x"])], ["x"]),
     REASON_DECOMPOSE_ERROR, False),
]


async def main() -> int:
    header = f"{'Scenario':<48}{'Reason':<30}{'Compound?':<11}{'Verdict'}"
    print(header)
    print("-" * len(header))

    passed = failed = 0
    for label, utterance, region, decompose_out, retrieval_payload, exp_reason, exp_compound in SCENARIOS:
        compound_called = {"v": False}

        class _SpyCompound(_FakeCompound):
            async def discover(self, **kw: Any) -> dict[str, Any]:
                compound_called["v"] = True
                return await super().discover(**kw)

        compound = _SpyCompound(retrieval_payload or {"hotels": [], "reason": None,
                                                       "missing_requirements": [],
                                                       "count": 0, "top_score": 0.0,
                                                       "region": region, "requirements": [],
                                                       "normalized_requirements": []})

        async def decompose(_u: str, _r: str, _out: Any = decompose_out) -> dict[str, Any]:
            if isinstance(_out, BaseException):
                raise _out
            return _out or {"requirements": []}

        async def render(_payload: dict[str, Any]) -> str:
            return "stub-answer"

        agent = ConciergeAgent(
            compound=compound, decompose_fn=decompose, render_fn=render,
        )
        result = await agent.answer(utterance=utterance, region=region)

        ok_reason = result["reason"] == exp_reason
        ok_compound = compound_called["v"] == exp_compound
        verdict = "PASS" if (ok_reason and ok_compound) else "FAIL"
        if ok_reason and ok_compound:
            passed += 1
        else:
            failed += 1
        print(f"{label:<48}{str(result['reason']):<30}{str(compound_called['v']):<11}{verdict}")

    print("-" * len(header))
    print(f"\nResults: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
