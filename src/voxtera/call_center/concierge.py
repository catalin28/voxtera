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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiohttp
from loguru import logger

from voxtera.call_center.clients import anthropic_client as _anthropic
from voxtera.call_center.compound import CompoundAndDiscovery
from voxtera.call_center.kb_config import DEFAULT_MAX_REQUIREMENTS
from voxtera.call_center.prompts import load_prompt

DEFAULT_MODEL = os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001")

REASON_EMPTY_UTTERANCE = "empty_utterance"
REASON_NO_REGION_SCOPE = "no_region_scope"
REASON_DECOMPOSE_ERROR = "decompose_error"
REASON_RENDER_ERROR = "render_error"

DecomposeFn = Callable[[str, str], Awaitable[dict[str, Any]]]
RenderFn = Callable[[dict[str, Any]], Awaitable[str]]
# Streaming render: same payload, but yields answer text deltas as they are
# generated (for the voice pipeline → sentence-level TTS).
RenderStreamFn = Callable[[dict[str, Any]], AsyncIterator[str]]


# Prompts live in src/voxtera/call_center/prompts/*.md so they can be edited
# without touching Python source (per project convention).
_DECOMPOSE_SYSTEM = load_prompt("concierge_decompose_legacy")
_RENDER_SYSTEM = load_prompt("concierge_render")


def _with_persona(
    task_prompt_name: str,
    *,
    include_images: bool = True,
    include_menus: bool = False,
    hotel_id: str | None = None,
) -> str:
    """Shared persona + task prompt. The persona (tone, spoken format, language)
    lives ONCE in concierge_persona.md and is prepended to every answer-writing
    prompt — edit the persona there, not in the task files.

    When ``include_images`` is True (default for text/WhatsApp channel) and the
    image catalog is non-empty, the catalog block is appended so the LLM knows
    which images it can surface via ``[IMG:<id>]`` tags. Voice render skips this
    (images can't be shown in audio) by passing ``include_images=False``.
    """
    persona = load_prompt("concierge_persona", hotel_id)
    task = load_prompt(task_prompt_name, hotel_id)
    base = persona + "\n\n" + task
    if include_images:
        try:
            from voxtera.whatsapp.image_catalog import system_prompt_block

            block = system_prompt_block()
            if block:
                base = base + "\n\n" + block
        except Exception as e:  # noqa: BLE001 — catalog must never break the pipeline
            logger.debug("image_catalog unavailable (skipping): {}", e)
    if include_menus:
        try:
            from voxtera.whatsapp.menu_catalog import system_prompt_block as menu_block

            block = menu_block()
            if block:
                base = base + "\n\n" + block
        except Exception as e:  # noqa: BLE001 — catalog must never break the pipeline
            logger.debug("menu_catalog unavailable (skipping): {}", e)
    return base


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
            return self._short_circuit(
                utterance,
                region,
                REASON_EMPTY_UTTERANCE,
                "I didn't catch that — could you say it again?",
                t_start,
                timings,
            )
        if not region:
            return self._short_circuit(
                utterance,
                region,
                REASON_NO_REGION_SCOPE,
                "Which region are you looking at?",
                t_start,
                timings,
            )

        t0 = time.perf_counter()
        try:
            decomposition = await self._decompose_fn(utterance, region)
        except Exception as e:  # noqa: BLE001
            timings["decompose_ms"] = _ms(time.perf_counter() - t0)
            logger.warning("Concierge decompose failed: {}", e)
            return self._short_circuit(
                utterance,
                region,
                REASON_DECOMPOSE_ERROR,
                "Sorry, I couldn't process that request just now.",
                t_start,
                timings,
            )
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
            answer = await self._render_fn(
                {
                    "utterance": utterance,
                    "region": region,
                    "decomposition": decomposition,
                    "retrieval": retrieval,
                }
            )
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
            region,
            len(requirements),
            retrieval.get("reason"),
            timings,
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
    def _short_circuit(
        utterance: str,
        region: str,
        reason: str,
        answer: str,
        t_start: float,
        timings: dict[str, float],
    ) -> dict[str, Any]:
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
        client = _anthropic()  # shared, connection pool kept warm
        msg = await client.messages.create(
            model=model,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": load_prompt("concierge_decompose_legacy"),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"Region: {region}\nUtterance: {utterance}",
                }
            ],
        )
        raw = _first_text_block(msg)
        return _parse_strict_json(raw)

    return decompose


def _build_render_user_msg(payload: dict[str, Any]) -> str:
    """Build the render user message from a retrieval payload.

    Shared by the streaming and non-streaming render paths so the prompt the
    model sees is byte-identical regardless of how the answer is consumed.
    """
    # Trim retrieval to the fields the model actually needs to stay grounded.
    retrieval = payload.get("retrieval") or {}
    hotels_out = []
    for h in retrieval.get("hotels", []):
        ev = {
            req: (chunk.get("text_en") or chunk.get("text") or "")[:280]
            for req, chunk in (h.get("evidence") or {}).items()
        }
        # A passage returned for MORE THAN ONE requirement is a generic blob
        # (e.g. a single "activities" list backing both "yoga" and "historical
        # sites") — it does not specifically confirm any of them.
        from collections import Counter as _Counter

        text_counts = _Counter(t for t in ev.values() if t)
        generic_reqs = sorted({req for req, t in ev.items() if t and text_counts[t] > 1})
        hotels_out.append(
            {
                "hotel_id": h["hotel_id"],
                "name": (h.get("payload") or {}).get("hotel_name"),
                # The hotel's ACTUAL location — so the model states where it really
                # is and never invents a city or nearby landmarks.
                "location": {
                    "district": (h.get("payload") or {}).get("district"),
                    "region": (h.get("payload") or {}).get("region"),
                    "country": (h.get("payload") or {}).get("country"),
                },
                "score": round(float(h.get("score", 0.0)), 3),
                "evidence": ev,
                # Requirements whose only evidence is a reused generic passage —
                # do NOT claim the hotel offers these.
                "unconfirmed_generic": generic_reqs,
            }
        )
    trimmed = {
        "reason": retrieval.get("reason"),
        "missing_requirements": retrieval.get("missing_requirements", []),
        "hotels": hotels_out,
    }
    language = (payload.get("decomposition") or {}).get("language") or "en"
    transcript = (payload.get("transcript") or "").strip()
    convo = f"Conversation so far:\n{transcript}\n\n" if transcript else ""
    return (
        f"Detected language: {language}\n"
        # This is the region the guest ASKED about — NOT necessarily where a
        # returned hotel is. Use each hotel's own 'location' for facts.
        f"Guest's requested region (may differ from a hotel's real "
        f"location): {payload.get('region')}\n"
        f"{convo}"
        f"Guest utterance: {payload.get('utterance')}\n\n"
        f"Retrieval result (JSON):\n{json.dumps(trimmed, ensure_ascii=False)}"
    )


def _build_anthropic_render_stream(model: str) -> RenderStreamFn:
    """Build a streaming render_fn that yields answer text deltas as Anthropic
    generates them.

    Used by the voice pipeline so TTS can start speaking the first sentence
    while the rest of the answer is still being written, instead of waiting for
    the full (≤512-token) reply. The non-streaming :func:`_build_anthropic_render`
    consumes this same generator, so both paths share one code path and one
    prompt.
    """

    async def render_stream(payload: dict[str, Any]):
        user_msg = _build_render_user_msg(payload)
        # Voice channel sends "brief": true → a shorter, spoken-style answer from
        # the external travel_agent_voice_render_brief.md prompt (editable, like
        # the rest). Chat omits it and gets the full concierge_render.md.
        # max_tokens is higher than a one-liner on purpose — "selling a dream,
        # not a fridge".
        brief = bool(payload.get("brief"))
        prompt_name = "travel_agent_voice_render_brief" if brief else "concierge_render"
        max_tokens = 320 if brief else 512
        # Voice brief replies go to TTS — images can't be shown in audio.
        # Text (WhatsApp chat) replies include the image catalog so the LLM
        # can embed [IMG:<id>] tags when a visual adds value.
        include_images = not brief
        hotel_id = (payload.get("hotel_id") or "").strip() or None
        client = _anthropic()  # shared, connection pool kept warm
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": _with_persona(
                        prompt_name, include_images=include_images, hotel_id=hotel_id
                    ),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            async for delta in stream.text_stream:
                yield delta
            final = await stream.get_final_message()
        usage = _extract_usage(final)
        # render latency tracks output_tokens; stop_reason == "max_tokens" means
        # the answer is being truncated at the 512 cap. cache_read confirms the
        # render system prompt cache is hitting.
        logger.info(
            "concierge.render usage in={} out={} cache_read={} stop={}",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            getattr(final, "stop_reason", None),
        )

    return render_stream


def _build_anthropic_render(model: str) -> RenderFn:
    """Build a render_fn that calls Anthropic with the retrieval payload and
    returns the full answer string.

    Used by the synchronous ``/api/concierge`` JSON endpoint. Internally it
    drains the streaming render so the two paths never diverge.
    """
    render_stream = _build_anthropic_render_stream(model)

    async def render(payload: dict[str, Any]) -> str:
        parts: list[str] = []
        async for delta in render_stream(payload):
            parts.append(delta)
        return "".join(parts).strip()

    return render


_WEB_SYNTH_SYSTEM = load_prompt("concierge_web_synth")
_CONVERSE_SYSTEM = load_prompt("concierge_converse")
_WEB_QUERY_SYSTEM = load_prompt("concierge_web_query")


def _build_anthropic_web_query(model: str) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build a query_fn that rewrites the conversation into ONE self-contained web
    search query — independent of the ES resolver and the decomposition."""

    async def web_query(payload: dict[str, Any]) -> str:
        # D19: when the pipeline knows the active hotel's true location, it is
        # injected here so "near the hotel" anchors to that place — not to a
        # city the conversation merely discussed.
        anchor = (payload.get("anchor") or "").strip()
        user_msg = (
            (f"{anchor}\n\n" if anchor else "")
            + f"Conversation so far:\n{(payload.get('transcript') or '(none yet)')}\n\n"
            f"Guest's current message: {payload.get('utterance')}"
        )
        client = _anthropic()
        msg = await client.messages.create(
            model=model,
            max_tokens=80,
            system=[
                {
                    "type": "text",
                    "text": load_prompt("concierge_web_query"),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        return _first_text_block(msg).strip()

    return web_query


def _build_anthropic_converse(model: str) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build a converse_fn that answers a conversational/meta/recall turn from
    the transcript (no retrieval)."""

    async def converse(payload: dict[str, Any]) -> str:
        user_msg = (
            f"Detected language: {payload.get('language') or 'en'}\n\n"
            f"Conversation so far:\n"
            f"{(payload.get('transcript') or '(this is the first message)')}\n\n"
            f"Guest's current message: {payload.get('utterance')}"
        )
        hotel_id = (payload.get("hotel_id") or "").strip() or None
        client = _anthropic()
        msg = await client.messages.create(
            model=model,
            max_tokens=220,
            system=[
                {
                    "type": "text",
                    "text": _with_persona("concierge_converse", hotel_id=hotel_id),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        return _first_text_block(msg).strip()

    return converse


def _build_anthropic_web_synth(model: str) -> Callable[[dict[str, Any]], Awaitable[str]]:
    """Build a synth_fn that rewrites a raw web-search blob into one clean,
    non-contradictory spoken answer (voice channel)."""

    async def synth(payload: dict[str, Any]) -> str:
        web = payload.get("web") or {}
        # Feed MORE, LONGER snippets — the model must ground its answer in the
        # evidence (and catch nuance like "spa on-site, scuba arranged nearby"),
        # not parrot the shallow aggregated blob.
        snippets = []
        for s in (web.get("sources") or [])[:6]:
            txt = s.get("snippet") or s.get("content") or ""
            if txt:
                snippets.append(f"- {txt[:600]}")
        hotel_facts = (payload.get("hotel_facts") or "").strip()
        hotel_block = (
            f"\n\nWhat the hotel's OWN guide says (on-site facts — weave these in, "
            f"lead with them):\n{hotel_facts}\n"
            if hotel_facts
            else ""
        )
        transcript = (payload.get("transcript") or "").strip()
        convo_block = (
            f"\n\nConversation so far (vary your openings — do NOT start the way "
            f"your previous replies started):\n{transcript}\n"
            if transcript
            else ""
        )
        user_msg = (
            f"Detected language: {payload.get('language') or 'en'}\n"
            f"Guest question: {payload.get('question')}\n"
            f"{convo_block}"
            f"{hotel_block}\n"
            f"Aggregated web answer (shallow — verify against snippets): "
            f"{web.get('answer') or '(none)'}\n\n"
            f"Web source snippets (the real evidence):\n" + ("\n".join(snippets) or "(none)")
        )
        client = _anthropic()
        msg = await client.messages.create(
            model=model,
            max_tokens=420,
            system=[
                {
                    "type": "text",
                    "text": _with_persona("concierge_web_synth"),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
        )
        return _first_text_block(msg).strip()

    return synth


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
