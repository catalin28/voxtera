"""ConciergePipeline — Phase 3 orchestrator (Slice B).

Wires together the five Slice-A modules into the canonical call-flow:

    classify_escalation
        → load_session
        → decompose
        → triage  (may short-circuit with a clarification question)
        → route   (may short-circuit asking for geography / hotel resolution)
        → execute_path  (compound retrieval for KB paths; placeholders
                         for web / destination / hybrid until later phases)
        → render
        → session.append_turn + session.save

Each step is dependency-injected so unit tests run fully offline.

Decision contract (returned by ``ConciergePipeline.run``):

    {
      "session_id":     str,
      "utterance":      str,
      "path":           str,           # "escalate" | "clarify" | router PATH_*
      "reason":         str,           # short audit string
      "escalation":     dict | None,   # classifier verdict when escalated
      "clarification":  dict | None,   # {question, slot} when triage asks
      "decomposition":  dict | None,
      "router":         dict | None,
      "retrieval":      dict | None,
      "answer":         str | None,
      "timings":        dict[str, float],
    }

Why a new class and not an in-place refactor of ConciergeAgent?
ConciergeAgent ships the Phase-2c surface (decompose -> compound -> render)
that the existing /api/concierge demo and tests depend on. The new
pipeline composes those primitives differently (5 modules instead of 2),
so keeping them as siblings avoids breaking the legacy surface while
the demo UI is being wired up.
"""

from __future__ import annotations

import time
from typing import Any

from loguru import logger

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.compound import CompoundAndDiscovery
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.router import (
    PATH_BROAD,
    PATH_DESTINATION,
    PATH_ESCALATE,
    PATH_HOTEL_RESOLVE,
    PATH_HYBRID,
    PATH_NEEDS_GEOGRAPHY,
    PATH_SCOPED,
    PATH_WEB,
    SourceRouter,
)
from voxtera.call_center.session import SessionStore, new_session_id
from voxtera.call_center.triage import Triage

# Paths that ConciergePipeline currently knows how to fully answer.
# Other paths (web, destination, hybrid) return acknowledged placeholders
# until the dedicated retrievers ship in later phases.
_KB_PATHS = {PATH_SCOPED, PATH_BROAD}


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def _placeholder_answer(path: str, language: str) -> str:
    """Localised acknowledgement for paths whose retrievers ship later."""
    msgs = {
        PATH_DESTINATION: {
            "en": "I can answer destination questions like that — full destination KB ships in the next release.",
            "tr": "Bu tür destinasyon sorularını yakında tam olarak yanıtlayabileceğim.",
        },
        PATH_WEB: {
            "en": "That needs a live web lookup — the web layer goes live in the next release.",
            "tr": "Bu canlı bir web aramasi gerektiriyor — web katmanı yakında devreye alınacak.",
        },
        PATH_HYBRID: {
            "en": "That mixes hotel data and a live web check — the hybrid path lights up in the next release.",
            "tr": "Bu otel verisi ile canlı web aramasını birleştiriyor — hibrit yol yakında aktif olacak.",
        },
        PATH_HOTEL_RESOLVE: {
            "en": "Which hotel exactly are you asking about?",
            "tr": "Tam olarak hangi otelden bahsediyorsunuz?",
        },
        PATH_NEEDS_GEOGRAPHY: {
            "en": "Which destination are you thinking of?",
            "tr": "Hangi destinasyonu düşünüyorsunuz?",
        },
    }
    by_lang = msgs.get(path, {"en": "Let me check on that."})
    return by_lang.get(language) or by_lang.get("en") or "Let me check on that."


class ConciergePipeline:
    """End-to-end orchestrator for the call-center concierge."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        classifier: EscalationClassifier,
        decomposer: QueryDecomposer,
        triage: Triage | None = None,
        router: SourceRouter | None = None,
        compound: CompoundAndDiscovery | None = None,
        render_fn: Any | None = None,
    ) -> None:
        self._sessions = session_store
        self._classifier = classifier
        self._decomposer = decomposer
        self._triage = triage or Triage()
        self._router = router or SourceRouter()
        self._compound = compound
        self._render_fn = render_fn

    async def run(
        self,
        *,
        utterance: str,
        session_id: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        utterance = (utterance or "").strip()
        sid = session_id or new_session_id()
        t_start = time.perf_counter()
        timings: dict[str, float] = {}

        if not utterance:
            return self._finish(
                sid=sid, utterance=utterance,
                path="empty", reason="empty_utterance",
                answer="I didn't catch that — could you say it again?",
                t_start=t_start, timings=timings,
            )

        # 1. Escalation guard.
        t0 = time.perf_counter()
        verdict = await self._classifier.classify(utterance)
        timings["classify_ms"] = _ms(time.perf_counter() - t0)
        if verdict.get("escalate"):
            answer = "Let me connect you to a colleague who can help with that right away."
            return self._finish(
                sid=sid, utterance=utterance,
                path=PATH_ESCALATE, reason="escalation_classifier",
                escalation=verdict, answer=answer,
                t_start=t_start, timings=timings,
            )

        # 2. Load session (or skeleton on cache miss).
        t0 = time.perf_counter()
        session = await self._sessions.load(sid)
        timings["session_load_ms"] = _ms(time.perf_counter() - t0)
        # Region from request becomes the active region for this turn if absent.
        if region and not session.get("active_region"):
            session["active_region"] = region

        # 3. Decompose.
        ctx = {
            "active_region": session.get("active_region"),
            "active_hotel_id": session.get("active_hotel_id"),
            "language": session.get("language"),
        }
        t0 = time.perf_counter()
        decomposition = await self._decomposer.decompose(utterance, ctx)
        timings["decompose_ms"] = _ms(time.perf_counter() - t0)

        # Backfill session language from the first decomposed turn.
        if decomposition.get("language") and not session.get("language"):
            session["language"] = decomposition["language"]

        # 4. Triage — may ask one clarification question and short-circuit.
        t0 = time.perf_counter()
        triage_decision = self._triage.assess(decomposition, session)
        timings["triage_ms"] = _ms(time.perf_counter() - t0)
        if triage_decision.get("ask"):
            await self._sessions.append_turn(
                session,
                utterance=utterance,
                decomposition=decomposition,
                reason=triage_decision.get("reason", "clarification"),
                answer=triage_decision.get("question"),
                is_clarification=True,
            )
            await self._sessions.save(session)
            return self._finish(
                sid=sid, utterance=utterance,
                path="clarify", reason=triage_decision.get("reason", "clarification"),
                decomposition=decomposition,
                clarification={
                    "question": triage_decision.get("question"),
                    "slot": triage_decision.get("slot"),
                    "language": triage_decision.get("language"),
                },
                answer=triage_decision.get("question"),
                t_start=t_start, timings=timings,
            )

        # 5. Route.
        t0 = time.perf_counter()
        decision = self._router.route(decomposition, session)
        timings["route_ms"] = _ms(time.perf_counter() - t0)
        path = decision.get("path", PATH_BROAD)

        # 6. Execute path.
        retrieval: dict[str, Any] | None = None
        answer: str | None = None

        if path in _KB_PATHS:
            t0 = time.perf_counter()
            retrieval = await self._run_kb(decomposition, session, path)
            timings["retrieve_ms"] = _ms(time.perf_counter() - t0)
            t0 = time.perf_counter()
            answer = await self._render(utterance, decomposition, retrieval, session)
            timings["render_ms"] = _ms(time.perf_counter() - t0)
        else:
            # Paths whose retrievers ship in later phases — return an
            # honest acknowledgement instead of pretending to answer.
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            answer = _placeholder_answer(path, lang)

        # 7. Persist turn.
        await self._sessions.append_turn(
            session,
            utterance=utterance,
            decomposition=decomposition,
            reason=decision.get("reason", path),
            answer=answer,
            is_clarification=False,
        )
        await self._sessions.save(session)

        return self._finish(
            sid=sid, utterance=utterance,
            path=path, reason=decision.get("reason", path),
            decomposition=decomposition, router=decision,
            retrieval=retrieval, answer=answer,
            t_start=t_start, timings=timings,
        )

    # ---------- internal helpers ----------

    async def _run_kb(
        self,
        decomposition: dict[str, Any],
        session: dict[str, Any],
        path: str,
    ) -> dict[str, Any]:
        """Dispatch to CompoundAndDiscovery for scoped / broad Qdrant retrieval."""
        if self._compound is None:
            return {
                "reason": "no_retriever_configured",
                "hotels": [], "missing_requirements": [],
            }
        region = (
            decomposition.get("region")
            or session.get("active_region")
            or ""
        )
        requirements = list(decomposition.get("requirements") or [])
        # For scoped queries the hotel is the filter, but the existing
        # CompoundAndDiscovery surface filters by region; scoped narrowing
        # by hotel_id is wired through downstream (Phase 4 work).
        return await self._compound.discover(
            region=region,
            requirements=requirements,
            activity_tags=None,
            category_hint=None,
        )

    async def _render(
        self,
        utterance: str,
        decomposition: dict[str, Any],
        retrieval: dict[str, Any] | None,
        session: dict[str, Any],
    ) -> str:
        """Call the injected render_fn, or return a defensive fallback."""
        if self._render_fn is None:
            count = len((retrieval or {}).get("hotels") or [])
            if count:
                names = ", ".join(
                    (h.get("payload") or {}).get("hotel_name", h.get("hotel_id"))
                    for h in retrieval["hotels"][:3]  # type: ignore[index]
                )
                return f"Top matches: {names}."
            return "I couldn't find a good match for that — could you tell me more?"
        try:
            return await self._render_fn({
                "utterance": utterance,
                "region": decomposition.get("region") or session.get("active_region"),
                "decomposition": decomposition,
                "retrieval": retrieval or {},
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("ConciergePipeline render failed: {}", e)
            return "Sorry, I had trouble forming a reply."

    def _finish(
        self,
        *,
        sid: str,
        utterance: str,
        path: str,
        reason: str,
        t_start: float,
        timings: dict[str, float],
        decomposition: dict[str, Any] | None = None,
        router: dict[str, Any] | None = None,
        retrieval: dict[str, Any] | None = None,
        escalation: dict[str, Any] | None = None,
        clarification: dict[str, Any] | None = None,
        answer: str | None = None,
    ) -> dict[str, Any]:
        timings["total_ms"] = _ms(time.perf_counter() - t_start)
        return {
            "session_id": sid,
            "utterance": utterance,
            "path": path,
            "reason": reason,
            "escalation": escalation,
            "clarification": clarification,
            "decomposition": decomposition,
            "router": router,
            "retrieval": retrieval,
            "answer": answer,
            "timings": timings,
        }
