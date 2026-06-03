"""Concierge agent (Phase 3) — utterance -> requirements -> compound -> answer.

End-to-end wiring of the Phase 2c CompoundAndDiscovery surface to a
guest-facing question/answer flow:

    1. Decompose the guest utterance + region into a structured set of
       requirements (free-form strings), optional activity tags, and an
       optional category hint via a single LLM call.
    2. Run CompoundAndDiscovery to intersect hotels across all
       requirements.
    3. Render a natural-language answer from the retrieved evidence via
       a second LLM call. The render step honours the detected
       language and is honest about `partial_match_only` /
       `no_match_above_threshold` outcomes.

Both LLM steps are dependency-injected (`decompose_fn`, `render_fn`)
so unit tests can drive the agent deterministically without network
access. The production defaults call Anthropic Claude (the same model
family the voice pipeline uses; see ``controllers.LLM_MODEL``).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from loguru import logger

from voxtera.call_center.compound import CompoundAndDiscovery
from voxtera.call_center.kb_config import DEFAULT_MAX_REQUIREMENTS

DEFAULT_MODEL = os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001")

REASON_EMPTY_UTTERANCE = "empty_utterance"
REASON_NO_REGION_SCOPE = "no_region_scope"
REASON_DECOMPOSE_ERROR = "decompose_error"
REASON_RENDER_ERROR = "render_error"

DecomposeFn = Callable[[str, str], Awaitable[dict[str, Any]]]
RenderFn = Callable[[dict[str, Any]], Awaitable[str]]


_DECOMPOSE_SYSTEM = """You convert hotel-guest utterances into a structured search plan.

Given an utterance and a region scope, return STRICT JSON with this shape:

  {
    "requirements": ["short noun-phrase 1", "short noun-phrase 2", ...],
    "activity_tags": ["tag1", "tag2"] or null,
    "category_hint": "wellness" | "food_beverage" | "rooms" | "activities" | "policies" | null,
    "language": "en" | "tr" | "ru" | "de" | "fr" | "es" | ...
  }

Rules:
- Each requirement MUST be a short noun phrase suitable for semantic search
  (e.g. "spa wellness massage", "kids club children programs", "ocean view balcony").
  Do NOT include filler words like "I want", "we'd like", "for my wife".
- Split independent requirements ("a spa AND scuba diving" -> 2 entries).
- Use activity_tags ONLY when an obvious filterable tag applies (diving, golf, kids).
- Use category_hint ONLY when the user is clearly asking about ONE specific category.
- Detect the language of the utterance (ISO-639-1).
- Return AT MOST 5 requirements.
- Output ONLY the JSON object, no prose, no markdown fences."""


_RENDER_SYSTEM = """You are a multilingual hotel concierge.

You will receive:
  - the original guest utterance
  - the detected language (answer in this language)
  - the region scope
  - the structured retrieval result from a hotel knowledge base

Write a SINGLE concise answer (2-4 sentences) that:
  - Names the hotels that match, with one short reason per hotel grounded in
    the evidence chunks.
  - If reason == "partial_match_only", explicitly acknowledges the missing
    requirements ("but none of them have X").
  - If reason == "no_match_above_threshold" or "empty_requirements", says so
    plainly without inventing hotels.
  - If reason == "no_region_scope", asks the guest which region they have in mind.
  - NEVER invent hotel names or amenities not present in the evidence.
  - Do NOT use markdown, lists, or section headers — plain conversational text.
  - Answer in the detected language."""


class ConciergeAgent:
    """Phase 3 — orchestrates decompose -> compound retrieve -> render."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession | None = None,
        compound: CompoundAndDiscovery | None = None,
        decompose_fn: DecomposeFn | None = None,
        render_fn: RenderFn | None = None,
        max_requirements: int = DEFAULT_MAX_REQUIREMENTS,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._session = session
        self._compound = compound or CompoundAndDiscovery(session=session)
        self._decompose_fn = decompose_fn or _build_anthropic_decompose(model)
        self._render_fn = render_fn or _build_anthropic_render(model)
        self._max_requirements = max_requirements

    async def answer(self, *, utterance: str, region: str) -> dict[str, Any]:
        utterance = (utterance or "").strip()
        region = (region or "").strip()
        t_start = time.perf_counter()
        timings: dict[str, float] = {}

        if not utterance:
            return self._short_circuit(utterance, region, REASON_EMPTY_UTTERANCE,
                                       "I didn't catch that — could you say it again?",
                                       t_start, timings)
        if not region:
            return self._short_circuit(utterance, region, REASON_NO_REGION_SCOPE,
                                       "Which region are you looking at?",
                                       t_start, timings)

        t0 = time.perf_counter()
        try:
            decomposition = await self._decompose_fn(utterance, region)
        except Exception as e:  # noqa: BLE001
            timings["decompose_ms"] = _ms(time.perf_counter() - t0)
            logger.warning("Concierge decompose failed: {}", e)
            return self._short_circuit(utterance, region, REASON_DECOMPOSE_ERROR,
                                       "Sorry, I couldn't process that request just now.",
                                       t_start, timings)
        timings["decompose_ms"] = _ms(time.perf_counter() - t0)

        requirements = list(decomposition.get("requirements") or [])[: self._max_requirements]
        tags = decomposition.get("activity_tags") or None
        category = decomposition.get("category_hint") or None

        t0 = time.perf_counter()
        retrieval = await self._compound.discover(
            region=region,
            requirements=requirements,
            activity_tags=tags,
            category_hint=category,
        )
        timings["retrieve_ms"] = _ms(time.perf_counter() - t0)

        t0 = time.perf_counter()
        try:
            answer = await self._render_fn({
                "utterance": utterance,
                "region": region,
                "decomposition": decomposition,
                "retrieval": retrieval,
            })
        except Exception as e:  # noqa: BLE001
            timings["render_ms"] = _ms(time.perf_counter() - t0)
            timings["total_ms"] = _ms(time.perf_counter() - t_start)
            logger.warning("Concierge render failed: {} (timings={})", e, timings)
            return {
                "utterance": utterance,
                "region": region,
                "decomposition": decomposition,
                "retrieval": retrieval,
                "answer": "Sorry, I had trouble forming a reply. Please try again.",
                "reason": REASON_RENDER_ERROR,
                "timings": timings,
            }
        timings["render_ms"] = _ms(time.perf_counter() - t0)
        timings["total_ms"] = _ms(time.perf_counter() - t_start)

        logger.info(
            "concierge.answer region={!r} reqs={} reason={} timings={}",
            region, len(requirements), retrieval.get("reason"), timings,
        )
        return {
            "utterance": utterance,
            "region": region,
            "decomposition": decomposition,
            "retrieval": retrieval,
            "answer": answer,
            "reason": retrieval.get("reason"),
            "timings": timings,
        }

    @staticmethod
    def _short_circuit(utterance: str, region: str, reason: str, answer: str,
                       t_start: float, timings: dict[str, float]) -> dict[str, Any]:
        timings["total_ms"] = _ms(time.perf_counter() - t_start)
        return {
            "utterance": utterance,
            "region": region,
            "decomposition": None,
            "retrieval": None,
            "answer": answer,
            "reason": reason,
            "timings": timings,
        }


def _ms(seconds: float) -> float:
    """Convert seconds -> milliseconds rounded to 1 decimal place."""
    return round(seconds * 1000.0, 1)


# ----------------- default Anthropic-backed steps -----------------

def _build_anthropic_decompose(model: str) -> DecomposeFn:
    """Build a decompose_fn that calls Anthropic and parses strict JSON.

    Lazily imports anthropic so unit tests that inject decompose_fn don't
    require the SDK or an API key.
    """

    async def decompose(utterance: str, region: str) -> dict[str, Any]:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()  # picks up ANTHROPIC_API_KEY
        msg = await client.messages.create(
            model=model,
            max_tokens=512,
            system=_DECOMPOSE_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Region: {region}\nUtterance: {utterance}",
            }],
        )
        raw = _first_text_block(msg)
        return _parse_strict_json(raw)

    return decompose


def _build_anthropic_render(model: str) -> RenderFn:
    """Build a render_fn that calls Anthropic with the retrieval payload."""

    async def render(payload: dict[str, Any]) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
        # Trim retrieval to the fields the model actually needs to stay grounded.
        retrieval = payload.get("retrieval") or {}
        trimmed = {
            "reason": retrieval.get("reason"),
            "missing_requirements": retrieval.get("missing_requirements", []),
            "hotels": [
                {
                    "hotel_id": h["hotel_id"],
                    "name": (h.get("payload") or {}).get("hotel_name"),
                    "score": round(float(h.get("score", 0.0)), 3),
                    "evidence": {
                        req: (chunk.get("text_en") or chunk.get("text") or "")[:280]
                        for req, chunk in (h.get("evidence") or {}).items()
                    },
                }
                for h in retrieval.get("hotels", [])
            ],
        }
        language = (payload.get("decomposition") or {}).get("language") or "en"
        user_msg = (
            f"Detected language: {language}\n"
            f"Region: {payload.get('region')}\n"
            f"Guest utterance: {payload.get('utterance')}\n\n"
            f"Retrieval result (JSON):\n{json.dumps(trimmed, ensure_ascii=False)}"
        )
        msg = await client.messages.create(
            model=model,
            max_tokens=512,
            system=_RENDER_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return _first_text_block(msg).strip()

    return render


def _first_text_block(msg: Any) -> str:
    """Extract the first text block from an Anthropic Messages response."""
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    return ""


def _parse_strict_json(raw: str) -> dict[str, Any]:
    """Parse a JSON object, stripping optional ```json fences defensively."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop the first fence line and the closing fence.
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    return json.loads(text)
