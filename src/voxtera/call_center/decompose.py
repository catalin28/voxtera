"""Query Decomposition — full structured extraction for the call-center concierge (Phase 3).

Replaces the small 5-field decomposition shipped earlier with the
complete schema defined in ``Voxtera_RAG_Architecture_v0.3.md §9``.
Single Claude call produces all extracted fields plus a `query_type`
drawn from the 27-type taxonomy (``§4.1``) and a `source_required`
list that the Source Router consumes.

Decision contract (returned by ``QueryDecomposer.decompose``):

    {
      # Geography
      "hotel_mention":      str | None,
      "city":               str | None,
      "region":             str | None,
      "district":           str | None,

      # Intent + routing
      "intent":             str,           # one of INTENTS
      "query_type":         str,           # one of QUERY_TYPES
      "query_type_id":      int | None,    # 1-27 for traceability
      "source_required":    list[str],     # subset of SOURCES

      # Requirements
      "requirements":       list[str],     # short noun phrases for semantic search
      "requirements_logic": str,           # "AND" or "OR"
      "on_site_required":   list[bool],    # per-requirement, same length as requirements

      # Traveller profile
      "traveller_type":     str | None,    # one of TRAVELLER_TYPES
      "children_ages":      list[int] | None,
      "adults_count":       int | None,

      # Budget + vibe + non-negotiables
      "budget_tier":        str | None,    # one of BUDGET_TIERS
      "budget_signal":      str | None,
      "vibe_preferences":   list[str],
      "dietary_religious":  list[str],
      "accessibility_needs":list[str],

      # Time + urgency
      "time_reference":     str | None,
      "returning_visitor":  bool,
      "urgency":            str,           # one of URGENCY_LEVELS

      # Output metadata
      "language":           str,           # ISO-639-1
      "model":              str,
      "latency_ms":         float,
    }

The Claude call is dependency-injected via ``decompose_fn`` so unit
tests run fully offline. The default backend uses ``claude-haiku-4-5``
(same model the existing concierge uses).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from voxtera.call_center.clients import anthropic_client as _anthropic
from voxtera.call_center.clients import openai_client as _openai
from voxtera.call_center.prompts import load_prompt

DEFAULT_MODEL = os.environ.get("DECOMPOSE_MODEL", "claude-haiku-4-5-20251001")

# --- Enumerations (mirror Voxtera_RAG_Architecture_v0.3 §9.1 / §4.1) ----

INTENTS = {
    "amenities",
    "activities",
    "food",
    "policy",
    "atmosphere",
    "comparison",
    "recommendation",
    "event",
    "local_operator",
    "weather",
    "practical_info",
    "children",
    "destination_info",
    "etiquette",
    "landmarks",
    "visa",
}

QUERY_TYPES = {
    "scoped",  # hotel-specific fact (1)
    "broad",  # activity/recommendation across hotels (2,5,6,7,8,9)
    "compound",  # multi-requirement intersection
    "comparison",  # hotel A vs hotel B (3)
    "destination",  # destination KB (11-15)
    "web",  # live web (16-19)
    "hybrid",  # hotel KB + web (20-23)
    "escalate",  # human agent (24-27)
}

SOURCES = {"hotel_kb", "destination_kb", "web"}

TRAVELLER_TYPES = {"solo", "couple", "family", "group", "corporate"}

BUDGET_TIERS = {"budget", "mid", "upper", "luxury"}

URGENCY_LEVELS = {"normal", "urgent", "immediate_escalation"}

REQUIREMENTS_LOGIC = {"AND", "OR"}

# Max requirements we'll honour from the LLM output (defensive cap).
MAX_REQUIREMENTS = 8

# A hotel_id slug like "akra_kemer" / "rixos_premium_belek": all lowercase,
# words joined by underscores. Humans never type these; if it shows up in
# hotel_mention it's the model echoing carry-over session state.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")

# Anaphoric references to "the current hotel" — not a NAMED hotel. These must
# not land in hotel_mention (which would trigger a doomed hotel-resolve);
# follow-ups like "is the hotel on the beach?" should scope to the session's
# active hotel via the router, which needs hotel_mention to be null.
_GENERIC_HOTEL_REFS = frozenset(
    {
        "the hotel",
        "this hotel",
        "that hotel",
        "the resort",
        "this resort",
        "that resort",
        "the property",
        "my hotel",
        "our hotel",
        "the place",
        "it",
        "they",
        "there",
        "here",
    }
)

# --- Prompt ------------------------------------------------------------

# Loaded at import time from src/voxtera/call_center/prompts/query_decomposer.md
# so non-engineers (and the future admin UI) can tune wording without editing code.
_DECOMPOSE_SYSTEM = load_prompt("query_decomposer")

DecomposeFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _empty_payload(model: str, latency_ms: float, language: str = "en") -> dict[str, Any]:
    return {
        "hotel_mention": None,
        "city": None,
        "region": None,
        "district": None,
        "intent": "recommendation",
        "query_type": "broad",
        "query_type_id": None,
        "source_required": ["hotel_kb"],
        "requirements": [],
        "requirements_logic": "AND",
        "on_site_required": [],
        "traveller_type": None,
        "children_ages": None,
        "adults_count": None,
        "budget_tier": None,
        "budget_signal": None,
        "vibe_preferences": [],
        "dietary_religious": [],
        "accessibility_needs": [],
        "time_reference": None,
        "returning_visitor": False,
        "urgency": "normal",
        "language": language,
        "model": model,
        "latency_ms": latency_ms,
    }


class QueryDecomposer:
    """Full §9 structured decomposition + 27-type classification."""

    def __init__(
        self,
        *,
        decompose_fn: DecomposeFn | None = None,
        model: str = DEFAULT_MODEL,
        max_requirements: int = MAX_REQUIREMENTS,
    ) -> None:
        self._decompose_fn = decompose_fn or _build_decompose_fn(model)
        self._model = model
        self._max_requirements = max_requirements

    async def decompose(
        self,
        utterance: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run full decomposition. ``context`` carries session-derived hints
        such as active_region / active_hotel_id so the LLM can inherit
        carry-over geography across turns.
        """
        utterance = (utterance or "").strip()
        if not utterance:
            return _empty_payload(self._model, 0.0)

        ctx = context or {}
        t0 = time.perf_counter()
        try:
            raw = await self._decompose_fn(utterance, ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("QueryDecomposer.decompose failed: {}", e)
            return _empty_payload(self._model, round((time.perf_counter() - t0) * 1000, 1))
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return self._coerce(raw, latency_ms)

    def _coerce(self, raw: dict[str, Any], latency_ms: float) -> dict[str, Any]:
        """Validate + normalise the LLM payload into our strict contract."""
        out = _empty_payload(self._model, latency_ms, language=str(raw.get("language") or "en"))

        # --- Geography (free strings, just strip + None-coerce) ---
        for k in ("city", "region", "district", "budget_signal", "time_reference"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()

        # hotel_mention is special: it must be a hotel the caller NAMED in this
        # utterance. A slug-shaped value (e.g. "akra_kemer") is never something a
        # human types — it's the model echoing a carry-over hotel_id from context.
        # Drop it so it doesn't trigger a bogus hotel-resolve on a broad query.
        hm = raw.get("hotel_mention")
        if isinstance(hm, str) and hm.strip():
            s = hm.strip()
            if _SLUG_RE.match(s):
                logger.debug("dropping slug-shaped hotel_mention {!r} (carry-over echo)", s)
            elif s.lower() in _GENERIC_HOTEL_REFS:
                logger.debug("dropping generic hotel_mention {!r} (anaphora, scope via session)", s)
            else:
                out["hotel_mention"] = s

        # --- Intent (enum, fall back to "recommendation") ---
        intent = (raw.get("intent") or "").strip().lower()
        if intent in INTENTS:
            out["intent"] = intent

        # --- Query type + id ---
        qt = (raw.get("query_type") or "").strip().lower()
        if qt in QUERY_TYPES:
            out["query_type"] = qt
        try:
            qid = raw.get("query_type_id")
            if qid is not None:
                qid_int = int(qid)
                if 1 <= qid_int <= 27:
                    out["query_type_id"] = qid_int
        except (TypeError, ValueError):
            pass

        # --- source_required: derive from query_type if missing/invalid ---
        sources = raw.get("source_required")
        cleaned_sources: list[str] = []
        if isinstance(sources, list):
            for s in sources:
                if isinstance(s, str) and s.strip().lower() in SOURCES:
                    cleaned_sources.append(s.strip().lower())
        if not cleaned_sources:
            cleaned_sources = _default_sources_for(out["query_type"])
        out["source_required"] = list(dict.fromkeys(cleaned_sources))  # dedupe, keep order

        # --- Requirements + logic + on_site_required ---
        reqs_raw = raw.get("requirements") or []
        reqs = [r.strip() for r in reqs_raw if isinstance(r, str) and r.strip()]
        reqs = reqs[: self._max_requirements]
        out["requirements"] = reqs

        logic = (raw.get("requirements_logic") or "AND").strip().upper()
        out["requirements_logic"] = logic if logic in REQUIREMENTS_LOGIC else "AND"

        on_site = raw.get("on_site_required") or []
        if isinstance(on_site, list) and all(isinstance(b, bool) for b in on_site):
            # Trim or pad to match requirements length.
            on_site = list(on_site)[: len(reqs)]
            on_site += [True] * (len(reqs) - len(on_site))
        else:
            on_site = [True] * len(reqs)
        out["on_site_required"] = on_site

        # --- Traveller profile ---
        tt = (raw.get("traveller_type") or "").strip().lower()
        if tt in TRAVELLER_TYPES:
            out["traveller_type"] = tt

        ages = raw.get("children_ages")
        if isinstance(ages, list):
            cleaned_ages: list[int] = []
            for a in ages:
                try:
                    ai = int(a)
                    if 0 <= ai <= 25:
                        cleaned_ages.append(ai)
                except (TypeError, ValueError):
                    pass
            out["children_ages"] = cleaned_ages or None

        try:
            ac = raw.get("adults_count")
            if ac is not None:
                ac_i = int(ac)
                if 1 <= ac_i <= 50:
                    out["adults_count"] = ac_i
        except (TypeError, ValueError):
            pass

        # --- Budget tier ---
        bt = (raw.get("budget_tier") or "").strip().lower()
        if bt in BUDGET_TIERS:
            out["budget_tier"] = bt

        # --- List tags ---
        for k in ("vibe_preferences", "dietary_religious", "accessibility_needs"):
            v = raw.get(k) or []
            if isinstance(v, list):
                out[k] = [t.strip().lower() for t in v if isinstance(t, str) and t.strip()]

        # --- Returning visitor (bool) ---
        out["returning_visitor"] = bool(raw.get("returning_visitor"))

        # --- Urgency ---
        urg = (raw.get("urgency") or "normal").strip().lower()
        if urg in URGENCY_LEVELS:
            out["urgency"] = urg

        # --- Diagnostics: pass token usage + stop_reason through to the log ---
        if isinstance(raw.get("_usage"), dict):
            out["usage"] = raw["_usage"]
        if raw.get("_stop_reason") is not None:
            out["stop_reason"] = raw["_stop_reason"]

        return out


def _default_sources_for(query_type: str) -> list[str]:
    """Fallback source list when the LLM omits source_required."""
    if query_type in {"scoped", "broad", "compound", "comparison"}:
        return ["hotel_kb"]
    if query_type == "destination":
        return ["destination_kb"]
    if query_type == "web":
        return ["web"]
    if query_type == "hybrid":
        return ["hotel_kb", "web"]
    return []  # escalate


# ----------------- backend selection -----------------

# Model-name prefixes that route to the OpenAI Chat Completions backend.
# Everything else (claude-*) routes to Anthropic. This is what makes
# DECOMPOSE_MODEL=gpt-4.1-nano a one-env-var A/B switch.
_OPENAI_PREFIXES = ("gpt", "o1", "o3", "o4", "chatgpt")


def _build_decompose_fn(model: str) -> DecomposeFn:
    """Pick the backend by model name: gpt*/o* → OpenAI, else Anthropic."""
    if model.lower().startswith(_OPENAI_PREFIXES):
        logger.info("QueryDecomposer using OpenAI backend (model={})", model)
        return _build_openai_decompose(model)
    return _build_anthropic_decompose(model)


def _build_ctx_block(context: dict[str, Any]) -> str:
    """Short carry-over hint block prepended to the user message.

    Tells the LLM what geography/language survived from prior turns so it
    can inherit them. Shared by both backends so the two are comparable.

    NOTE: we deliberately do NOT print the session's active_hotel_id value.
    Feeding the carry-over hotel *id/name* into the prompt made the model echo
    it back as `hotel_mention` (and fabricate a `city` from the slug), hijacking
    fresh broad requests. Instead, when a hotel is active we add an instruction
    (no identifier) telling the model to mark follow-ups as scoped and leave
    hotel_mention null — the router supplies the actual id from the session.
    """
    ctx_lines = []
    if context.get("active_region"):
        ctx_lines.append(f"Carry-over region: {context['active_region']}")
    if context.get("active_hotel_id"):
        ctx_lines.append(
            "A specific hotel is already the subject of this conversation. If this "
            'message is a follow-up about that hotel — e.g. "do they have a pool?", '
            '"what restaurants are there?", "is it on the beach?", or it uses '
            '"they"/"it"/"there" — set query_type to "scoped" and leave '
            'hotel_mention null. Only use "broad" if the caller is asking for NEW '
            "hotel suggestions. Never copy the hotel name/id into hotel_mention."
        )
    if context.get("language"):
        ctx_lines.append(f"Session language: {context['language']}")
    return ("\n".join(ctx_lines) + "\n\n") if ctx_lines else ""


# ----------------- OpenAI-backed decomposer -----------------


def _build_openai_decompose(model: str) -> DecomposeFn:
    """Build a decompose_fn that calls OpenAI Chat Completions (strict JSON).

    Uses the SAME system prompt as the Anthropic path so the only variable
    in the A/B is the model. `response_format=json_object` guarantees
    parseable output. Lazily imports openai so injected-fn tests stay offline.
    """

    async def decompose(utterance: str, context: dict[str, Any]) -> dict[str, Any]:
        ctx_block = _build_ctx_block(context)
        client = _openai()  # shared, connection pool kept warm
        resp = await client.chat.completions.create(
            model=model,
            temperature=0.0,
            max_tokens=1024,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": f"{ctx_block}Utterance: {utterance}"},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        parsed = _parse_strict_json(content)
        usage = _extract_openai_usage(resp)
        parsed["_usage"] = usage
        parsed["_stop_reason"] = resp.choices[0].finish_reason
        logger.debug(
            "decompose[openai] usage in={} out={} cached={} stop={}",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            parsed["_stop_reason"],
        )
        return parsed

    return decompose


def _extract_openai_usage(resp: Any) -> dict[str, Any]:
    """Normalise OpenAI usage onto the same keys as the Anthropic helper."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    details = getattr(u, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details else None
    return {
        "input_tokens": getattr(u, "prompt_tokens", None),
        "output_tokens": getattr(u, "completion_tokens", None),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": None,
    }


# ----------------- default Anthropic-backed decomposer -----------------


def _build_anthropic_decompose(model: str) -> DecomposeFn:
    """Build a decompose_fn that calls Anthropic and parses strict JSON."""

    async def decompose(utterance: str, context: dict[str, Any]) -> dict[str, Any]:
        # Context block tells the LLM what carry-over hints already exist
        # (e.g. active_region from a prior turn). Keep it short.
        ctx_block = _build_ctx_block(context)

        client = _anthropic()  # shared, connection pool kept warm
        msg = await client.messages.create(
            model=model,
            max_tokens=1024,
            # NOTE: prompt caching is effectively a no-op here — the system
            # prompt is below Anthropic's minimum cacheable size on Haiku, so
            # cache_read stays 0 (see phase3 doc §3.3). Kept as harmless.
            system=[
                {
                    "type": "text",
                    "text": _DECOMPOSE_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"{ctx_block}Utterance: {utterance}",
                }
            ],
        )
        raw = _first_text_block(msg)
        parsed = _parse_strict_json(raw)
        # Thread token usage + stop_reason through so the pipeline can log them.
        # `output_tokens` is the real driver of decompose latency; `stop_reason`
        # == "max_tokens" would mean we're truncating at the 1024 cap.
        # `cache_read_input_tokens` > 0 confirms the prompt cache is hitting.
        usage = _extract_usage(msg)
        parsed["_usage"] = usage
        parsed["_stop_reason"] = getattr(msg, "stop_reason", None)
        logger.debug(
            "decompose usage in={} out={} cache_read={} stop={}",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            parsed["_stop_reason"],
        )
        return parsed

    return decompose


def _extract_usage(msg: Any) -> dict[str, Any]:
    """Pull token counts off an Anthropic Messages response, defensively."""
    u = getattr(msg, "usage", None)
    if u is None:
        return {}
    return {
        "input_tokens": getattr(u, "input_tokens", None),
        "output_tokens": getattr(u, "output_tokens", None),
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
    }


def _first_text_block(msg: Any) -> str:
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            return text
    return ""


def _parse_strict_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    return json.loads(text)
