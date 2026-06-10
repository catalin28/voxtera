"""Travel-agent brain — answer each turn via the shared /api/concierge endpoint.

A Pipecat :class:`FrameProcessor` that stands in for the LLM service when
``settings.bot_brain == "travel_agent"`` (``BOT_BRAIN=travel_agent``). On each
turn it forwards the STT transcript to the launcher's ``POST /api/concierge`` —
**the exact same endpoint and warm pipeline the chat UI uses** — and speaks the
answer.

Why HTTP to the shared endpoint instead of running ConciergePipeline in-process:
the voice bot is a separate subprocess from the launcher (serve.py), where the
warm concierge pipeline lives. Rebuilding the pipeline inside the bot created a
*second copy* of the wiring, and any drift between the two copies is a bug (e.g.
region "" vs None scoping). Delegating to the one endpoint guarantees voice and
chat behave identically — one pipeline, one region handler, one source of truth.
The only thing given up is token-streaming the answer into TTS; for short spoken
answers the instant filler already masks the gap, and the HTTP hop is localhost.

Integration contract — wired into the pipeline *in place of* the LLM service:

  * Consumes the ``LLMContextFrame`` the context aggregator emits, reads the
    latest user utterance, and POSTs it to ``/api/concierge``.
  * Emits the answer as ``LLMFullResponseStartFrame`` → ``LLMTextFrame`` →
    ``LLMFullResponseEndFrame`` — the same frames an LLM service emits — so TTS,
    :class:`~voxtera.observability.DemoEventBroadcaster` (orb ``bot-reply``), and
    the assistant context aggregator all work unchanged.

Region + session: ``region`` is forwarded verbatim to ``/api/concierge`` (which
owns the ""/None scoping semantics); ``session_id`` is carried forward so the
concierge keeps multi-turn context. Both come from the environment
(``CONCIERGE_REGION`` / ``VOXTERA_SESSION_ID``) set by ``start-session``.
"""

from __future__ import annotations

import json
import os
from typing import Any

import aiohttp
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Daily data-channel frame — the only path from the server-side bot to the
# browser during a voice call. Used to ship the concierge result (already
# fetched once) to the page so it can render the same debug + evidence cards the
# chat UI shows. Optional: absent in local (non-Daily) transport.
try:
    from pipecat.transports.daily.transport import DailyOutputTransportMessageFrame
except Exception:  # noqa: BLE001 — local transport has no Daily frame
    DailyOutputTransportMessageFrame = None  # type: ignore[assignment,misc]


def _trim_retrieval(retrieval: Any) -> Any:
    """Shrink the retrieval payload for the Daily data channel (size-limited).

    Keeps exactly what the page's renderHotels/renderWeb need: hotel name +
    location + score + per-requirement evidence (text capped), the partial-match
    reason/missing list, and the web sources. Drops everything else.
    """
    if not isinstance(retrieval, dict):
        return retrieval
    hotels_out = []
    for h in (retrieval.get("hotels") or [])[:5]:
        payload = h.get("payload") or {}
        evidence = {}
        for req, chunk in (h.get("evidence") or {}).items():
            text = ""
            if isinstance(chunk, dict):
                text = chunk.get("text") or (chunk.get("payload") or {}).get("text") or ""
            evidence[req] = {"text": (text or "")[:220]}
        hotels_out.append(
            {
                "hotel_id": h.get("hotel_id"),
                "score": h.get("score"),
                "payload": {
                    "hotel_name": payload.get("hotel_name") or payload.get("name"),
                    "region": payload.get("region"),
                    "district": payload.get("district"),
                    "country": payload.get("country"),
                },
                "evidence": evidence,
            }
        )
    out: dict[str, Any] = {
        "hotels": hotels_out,
        "reason": retrieval.get("reason"),
        "missing_requirements": retrieval.get("missing_requirements") or [],
    }
    web = retrieval.get("web")
    if isinstance(web, dict):
        out["web"] = {
            "query": web.get("query"),
            "sources": [
                {"title": s.get("title"), "url": s.get("url")}
                for s in (web.get("sources") or [])[:3]
                if isinstance(s, dict)
            ],
        }
    return out


def _last_user_text(context: Any) -> str:
    """Pull the most recent user utterance from an ``LLMContext``.

    Content may be a plain string or a list of content parts (OpenAI-style);
    both shapes are handled. Returns "" if no user message is found.
    """
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001 — fall back to the raw attribute
        messages = getattr(context, "messages", None) or []

    for msg in reversed(messages):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "user":
            continue
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif isinstance(part, str):
                    parts.append(part)
            return " ".join(p for p in parts if p).strip()
        return ""
    return ""


def _resolve_concierge_url() -> str:
    """Resolve the /api/concierge/stream URL the bot should call.

    Streaming variant of /api/concierge: same pipeline, but the render is
    streamed token-by-token so TTS can start mid-render. Prefers an explicit
    ``VOXTERA_CONCIERGE_URL``; otherwise defaults to the standalone concierge
    service (P0.2 — ``voxtera.concierge_service`` on ``CONCIERGE_PORT``,
    default 8300, on the same host).
    """
    explicit = os.environ.get("VOXTERA_CONCIERGE_URL")
    if explicit:
        return explicit
    port = os.environ.get("CONCIERGE_PORT", "8300")
    return f"http://127.0.0.1:{port}/api/concierge/stream"


class TravelAgentBrain(FrameProcessor):
    """Answers turns via the shared /api/concierge endpoint, emitting LLM frames."""

    def __init__(
        self,
        *,
        region: str | None = None,
        session_id: str | None = None,
        hotel_id: str | None = None,
    ) -> None:
        super().__init__()
        # Region scope is forwarded verbatim to /api/concierge, which owns the
        # ""/None semantics ("" = all regions; a name = scope to it). We default
        # an absent env to "" (all regions) — never None — so the endpoint clears
        # any stale scope, exactly like the chat UI's "All regions" selection.
        if region is not None:
            self._region = region
        else:
            self._region = os.environ.get("CONCIERGE_REGION", "")
        # Property scope (P1.4 "one brain"): set → the concierge answers as
        # THIS hotel's concierge from its own guide (per-hotel SQLite RAG);
        # unset → cross-hotel travel agent. This is the travel↔hotel demo
        # switch: same brain, same pipeline, one parameter.
        self._hotel_id = (
            hotel_id if hotel_id is not None else os.environ.get("CONCIERGE_HOTEL_ID", "")
        ).strip() or None
        self._session_id = session_id or os.environ.get("VOXTERA_SESSION_ID") or None
        self._concierge_url = _resolve_concierge_url()
        self._http: aiohttp.ClientSession | None = None
        logger.info(
            "[travel-agent-brain] initialised (region={!r}, hotel={!r}, session={}, url={})",
            self._region,
            (self._hotel_id or "—"),
            (self._session_id or "—"),
            self._concierge_url,
        )

    async def _stream_concierge(self, utterance: str, on_delta):
        """POST to /api/concierge/stream and consume the NDJSON stream.

        Calls ``on_delta(text)`` for each render delta as it arrives (so TTS can
        start mid-render) and returns the final result dict from the ``done``
        event. Same request shape as chat plus ``brief: true`` for the voice
        render. Raises on a stream ``error`` event or HTTP failure.
        """
        if self._http is None:
            self._http = aiohttp.ClientSession()
        payload = {
            "utterance": utterance,
            "region": self._region,
            "session_id": self._session_id,
            # Property scope: answer as this hotel's concierge (None = travel agent).
            "hotel_id": self._hotel_id,
            # Voice channel: short, spoken-style answer (travel_agent_voice_render_brief.md).
            "brief": True,
        }
        timeout = aiohttp.ClientTimeout(total=130)
        result: dict[str, Any] = {}
        async with self._http.post(self._concierge_url, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "text":
                    chunk = evt.get("chunk") or ""
                    if chunk:
                        await on_delta(chunk)
                elif etype == "done":
                    result = evt.get("result") or {}
                elif etype == "error":
                    raise RuntimeError(evt.get("error") or "concierge stream error")
        return result

    async def _send_debug(self, result: dict[str, Any]) -> None:
        """Send the concierge result to the browser over the Daily data channel.

        Carries the fields the page's renderDebug/renderHotels/renderWeb need.
        No-op in local transport (no Daily frame). Best-effort: a failure here
        must never affect the spoken answer.
        """
        if DailyOutputTransportMessageFrame is None:
            return
        try:
            data = {
                "utterance": result.get("utterance"),
                "answer": result.get("answer"),
                "path": result.get("path"),
                "reason": result.get("reason"),
                "decomposition": result.get("decomposition"),
                "timings": result.get("timings"),
                "trace": result.get("trace"),
                "clarification": result.get("clarification"),
                "escalation": result.get("escalation"),
                "retrieval": _trim_retrieval(result.get("retrieval")),
            }
            msg = {"type": "voxtera-event", "event": "concierge-result", "data": data}
            await self.push_frame(
                DailyOutputTransportMessageFrame(message=msg), FrameDirection.DOWNSTREAM
            )
        except Exception as exc:  # noqa: BLE001 — debug channel must not break the call
            logger.warning("[travel-agent-brain] debug app-message failed: {}", exc)

    def _spoken_text(self, result: dict[str, Any]) -> str:
        """Pick what to say from a concierge result.

        Prefer the rendered answer; fall back to a clarification question when
        triage short-circuited before producing an answer.
        """
        spoken = (result.get("answer") or "").strip()
        if not spoken:
            clar = result.get("clarification") or {}
            spoken = (clar.get("question") or "").strip()
        return spoken

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Only intercept the LLM run signal flowing downstream. Everything else
        # (system frames, interruptions, etc.) passes straight through.
        if not (isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM):
            await self.push_frame(frame, direction)
            return

        utterance = _last_user_text(frame.context)
        # Like the LLM service, the LLMContextFrame is a signal we consume — it
        # is NOT forwarded. We always bracket the turn with Start/End so the
        # assistant aggregator and broadcaster see a complete response.
        await self.push_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        streamed_any = False
        try:
            if not utterance:
                logger.warning("[travel-agent-brain] empty utterance — skipping concierge call")
            else:
                # Stream render deltas straight to TTS as they arrive, so the bot
                # starts speaking the first sentence mid-render instead of after.
                async def _on_delta(chunk: str) -> None:
                    nonlocal streamed_any
                    streamed_any = True
                    await self.push_frame(LLMTextFrame(chunk), FrameDirection.DOWNSTREAM)

                result = await self._stream_concierge(utterance, _on_delta)
                # Carry the concierge session forward for multi-turn context.
                self._session_id = result.get("session_id") or self._session_id
                if result.get("timings"):
                    logger.info(
                        "[travel-agent-brain] path={} streamed={} timings={}",
                        result.get("path"),
                        streamed_any,
                        result.get("timings"),
                    )
                # Non-render paths (web / conversational / clarify / no-match) don't
                # stream, so nothing was spoken — emit the final answer once.
                if not streamed_any:
                    spoken = self._spoken_text(result)
                    if spoken:
                        await self.push_frame(LLMTextFrame(spoken), FrameDirection.DOWNSTREAM)
                    else:
                        logger.warning("[travel-agent-brain] concierge returned no answer/clarif")
                # Ship the result to the browser (debug drawer + evidence cards).
                # Sent BEFORE the LLMFullResponseEndFrame below, so it reaches the
                # page just ahead of the bot-reply that creates the bubble.
                await self._send_debug(result)
        except Exception as exc:  # noqa: BLE001 — never crash the call on a bad turn
            logger.exception("[travel-agent-brain] concierge call failed: {}", exc)
            if not streamed_any:
                await self.push_frame(
                    LLMTextFrame("Sorry, I ran into a problem finding that. Could you try again?"),
                    FrameDirection.DOWNSTREAM,
                )
        finally:
            await self.push_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
