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
from pathlib import Path
from typing import Any

from loguru import logger

from voxtera.call_center.prompts import load_prompt

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


# Loaded at import time from src/voxtera/call_center/prompts/escalation_classifier.md
# so non-engineers (and the future admin UI) can tune wording without editing code.
_SYSTEM_PROMPT = load_prompt("escalation_classifier")

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


# ── Fast gate: skip the LLM for obviously benign turns ──────────────────────
# Escalations are rare and lexically loud ("book", "cancel", "şikayet",
# "doktor", "acil"...). If the utterance contains NONE of the stems from the
# external JSON, we skip the LLM classify entirely (~1-1.9s saved on the
# typical Q&A turn). If ANY stem matches, the LLM still makes the real
# decision — the gate never decides an escalation itself, it only decides
# whether the question is worth asking. The multilingual decomposer
# (query_type="escalate") remains an independent second net for phrasings the
# stem list doesn't know. The list lives in prompts/escalation_stems.json so
# it can be edited without touching code; edits are picked up automatically
# (mtime check). If the file is missing or corrupt we FAIL OPEN: the gate
# disables itself and every turn runs the LLM, exactly as before.
_STEMS_DEFAULT_PATH = Path(__file__).resolve().parent / "prompts" / "escalation_stems.json"
_stems_cache: dict[str, Any] = {"path": None, "mtime": None, "stems": None}


def _load_escalation_stems() -> tuple[str, ...] | None:
    """Load (and hot-reload on change) the stem list. None = gate disabled."""
    path_str = os.environ.get("ESCALATION_STEMS_PATH", "").strip()
    path = Path(path_str) if path_str else _STEMS_DEFAULT_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if _stems_cache["stems"] is not None or _stems_cache["path"] != str(path):
            logger.warning("escalation stems file missing ({}) — fast gate disabled", path)
        _stems_cache.update({"path": str(path), "mtime": None, "stems": None})
        return None
    if _stems_cache["path"] == str(path) and _stems_cache["mtime"] == mtime:
        return _stems_cache["stems"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stems = tuple(
            s.strip().lower()
            for key, values in data.items()
            if not key.startswith("_") and isinstance(values, list)
            for s in values
            if isinstance(s, str) and s.strip()
        )
        if not stems:
            raise ValueError("no stems found")
        _stems_cache.update({"path": str(path), "mtime": mtime, "stems": stems})
        logger.info("escalation fast-gate loaded {} stems from {}", len(stems), path.name)
        return stems
    except Exception as e:  # noqa: BLE001 — fail open, never break classification
        logger.warning("escalation stems file unreadable ({}): {} — fast gate disabled", path, e)
        _stems_cache.update({"path": str(path), "mtime": mtime, "stems": None})
        return None


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

        # Fast gate: no escalation stem anywhere in the utterance → skip the LLM.
        stems = _load_escalation_stems()
        if stems is not None:
            low = utterance.lower()
            if not any(stem in low for stem in stems):
                return _empty_verdict("fast_gate", 0.0)

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
                    _cache_key(utterance),
                    json.dumps(verdict),
                    self._cache_ttl,
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
        signal = signal.strip() or None if isinstance(signal, str) else None

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
