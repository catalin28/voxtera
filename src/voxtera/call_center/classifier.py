"""Escalation Classifier — first-line guard before RAG (Phase 3).

Runs on every incoming utterance BEFORE decomposition/triage/retrieval.
If it fires, the concierge short-circuits and returns an escalation
payload instead of running the full RAG pipeline.

Escalation categories (per Voxtera_RAG_Architecture_v0.3 / Phase 3 plan):

    live_complaint   — caller is on-property with an active problem
    medical          — medical or safety emergency
    urgency          — time-pressured request (acute, now, right away)
    booking          — caller wants to make a reservation (different workflow)
    post_booking     — caller wants to modify an existing reservation

Decision contract (returned by ``EscalationClassifier.classify``):

    {
      "escalate":         bool,
      "escalation_type":  str | None,   # one of the categories above when escalate=True
      "confidence":       float,        # 0.0 - 1.0
      "signal":           str | None,   # short phrase from the utterance that triggered the call
      "model":            str,          # which model produced the verdict
      "latency_ms":       float,
    }

Implementation: a single OpenAI Chat Completion call to GPT-4.1-nano
with a strict-JSON prompt. The classifier is multilingual (works on
Turkish, English, Russian, German etc.) since the underlying model is.
A Redis-backed LRU cache keyed by ``sha1(utterance)`` short-circuits
repeat utterances at zero cost.

The OpenAI client and Redis client are dependency-injected so unit
tests run fully offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

DEFAULT_MODEL = os.environ.get("ESCALATION_MODEL", "gpt-4.1-nano")
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — escalation signals are stable
CACHE_KEY_PREFIX = "voxtera:cc:escalation:"

VALID_TYPES = {
    "live_complaint",
    "medical",
    "urgency",
    "booking",
    "post_booking",
}

# Conservative threshold: below this we treat the verdict as no-escalation
# even if the model returned a category. Tunable via env.
ESCALATE_MIN_CONFIDENCE = float(os.environ.get("ESCALATION_MIN_CONFIDENCE", "0.55"))


_SYSTEM_PROMPT = """You classify hotel call-center utterances.

Decide whether the caller's message should escalate OUT of the normal
recommendation / knowledge-base flow. Multilingual input (Turkish,
English, Russian, German, etc.).

Categories — pick AT MOST ONE:

  live_complaint  — Caller is ON the hotel property and has an active
                    problem right now (can't get into room, AC broken,
                    no hot water, noise, missing item, dirty room, ...).
  medical         — Medical, safety, or security emergency (fainted,
                    injury, ambulance, fire, theft in progress).
  urgency         — Time-pressured request ("right now", "immediately",
                    "acil", "şimdi", "сейчас же") that needs human attention.
  booking         — Caller wants to MAKE a new reservation
                    ("I want to book", "rezervasyon yapmak istiyorum").
  post_booking    — Caller wants to MODIFY/CANCEL an existing booking
                    ("change my reservation", "rezervasyonumu iptal etmek").
  none            — Anything else — recommendations, KB questions,
                    chit-chat, hypotheticals, future planning.

Output STRICT JSON, no prose, no markdown fences:

  {"type": "<category>", "confidence": 0.0-1.0, "signal": "<short phrase from input>"}

Rules:
  - If the caller is just ASKING about reservations in the abstract
    ("do you have rooms?") that is NOT booking — that is "none".
  - If the caller MENTIONS being at the hotel but is asking a normal
    KB question ("where is the spa?") that is NOT live_complaint.
  - Be conservative: if unsure, return {"type": "none", "confidence": 0.3, "signal": null}.
"""

ClassifyFn = Callable[[str], Awaitable[dict[str, Any]]]
CacheGet = Callable[[str], Awaitable[str | None]]
CacheSet = Callable[[str, str, int], Awaitable[None]]


def _cache_key(utterance: str) -> str:
    digest = hashlib.sha1(utterance.strip().lower().encode("utf-8")).hexdigest()
    return CACHE_KEY_PREFIX + digest


def _empty_verdict(model: str, latency_ms: float) -> dict[str, Any]:
    return {
        "escalate": False,
        "escalation_type": None,
        "confidence": 0.0,
        "signal": None,
        "model": model,
        "latency_ms": latency_ms,
    }


class EscalationClassifier:
    """LLM-backed escalation classifier with Redis-backed result cache."""

    def __init__(
        self,
        *,
        classify_fn: ClassifyFn | None = None,
        cache_get: CacheGet | None = None,
        cache_set: CacheSet | None = None,
        model: str = DEFAULT_MODEL,
        min_confidence: float = ESCALATE_MIN_CONFIDENCE,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        self._classify_fn = classify_fn or _build_openai_classify(model)
        self._cache_get = cache_get
        self._cache_set = cache_set
        self._model = model
        self._min_confidence = min_confidence
        self._cache_ttl = cache_ttl_seconds

    async def classify(self, utterance: str) -> dict[str, Any]:
        utterance = (utterance or "").strip()
        if not utterance:
            return _empty_verdict(self._model, 0.0)

        # Cache lookup.
        if self._cache_get is not None:
            try:
                cached = await self._cache_get(_cache_key(utterance))
                if cached:
                    obj = json.loads(cached)
                    obj["latency_ms"] = 0.0  # cache hit
                    return obj
            except Exception as e:  # noqa: BLE001
                logger.warning("EscalationClassifier cache_get failed: {}", e)

        t0 = time.perf_counter()
        try:
            raw = await self._classify_fn(utterance)
        except Exception as e:  # noqa: BLE001
            logger.warning("EscalationClassifier classify_fn failed: {}", e)
            return _empty_verdict(self._model, round((time.perf_counter() - t0) * 1000, 1))
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        verdict = self._coerce(raw, latency_ms)

        # Cache the verdict (even non-escalation) so repeat utterances are free.
        if self._cache_set is not None:
            try:
                await self._cache_set(
                    _cache_key(utterance), json.dumps(verdict), self._cache_ttl,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("EscalationClassifier cache_set failed: {}", e)

        return verdict

    def _coerce(self, raw: dict[str, Any], latency_ms: float) -> dict[str, Any]:
        category = (raw.get("type") or "none").strip().lower()
        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        signal = raw.get("signal")
        if isinstance(signal, str):
            signal = signal.strip() or None
        else:
            signal = None

        if category not in VALID_TYPES:
            return {
                "escalate": False,
                "escalation_type": None,
                "confidence": confidence,
                "signal": signal,
                "model": self._model,
                "latency_ms": latency_ms,
            }

        escalate = confidence >= self._min_confidence
        return {
            "escalate": escalate,
            "escalation_type": category if escalate else None,
            "confidence": confidence,
            "signal": signal,
            "model": self._model,
            "latency_ms": latency_ms,
        }


# ----------------- default OpenAI-backed classifier -----------------

def _build_openai_classify(model: str) -> ClassifyFn:
    """Build a classify_fn that calls OpenAI and parses strict JSON.

    Lazily imports openai so tests that inject classify_fn don't require
    the SDK or an API key.
    """

    async def classify(utterance: str) -> dict[str, Any]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI()  # picks up OPENAI_API_KEY
        resp = await client.chat.completions.create(
            model=model,
            temperature=0.0,
            max_tokens=80,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": utterance},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"type": "none", "confidence": 0.0, "signal": None}

    return classify
