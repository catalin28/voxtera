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

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from voxtera.call_center.classifier import EscalationClassifier
from voxtera.call_center.compound import CompoundAndDiscovery
from voxtera.call_center.decompose import QueryDecomposer
from voxtera.call_center.resolver import HotelResolver
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

# Fallback requirements for a scoped query about a resolved hotel that arrived
# with no specific ask ("tell me about X", "how about X?"). The decomposer is
# inconsistent here — sometimes it emits ["hotel overview", "amenities", ...],
# sometimes []. An empty list makes CompoundAndDiscovery short-circuit with
# `empty_requirements` and the concierge fails closed even though the hotel is
# known. Injecting a generic overview makes the scoped path robust to that.
_SCOPED_DEFAULT_REQUIREMENTS = ("hotel overview", "amenities", "facilities")


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


def _no_match_answer(language: str, region: str) -> str:
    """Deterministic fail-closed reply when retrieval returned 0 hotels."""
    where = f" in {region}" if region else ""
    where_tr = f" {region} bölgesinde" if region else ""
    msgs = {
        "en": (
            f"I couldn't find a hotel{where} that matches every part of that request. "
            "Could you loosen one of the requirements, or tell me a bit more about what matters most?"
        ),
        "tr": (
            f"İsteğinizin her parçasına uyan bir otel{where_tr} bulamadım. "
            "Bir kriteri biraz esnetebilir misiniz veya en önemli olanı söyleyebilir misiniz?"
        ),
    }
    return msgs.get(language) or msgs["en"]


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
        resolver: HotelResolver | None = None,
    ) -> None:
        self._sessions = session_store
        self._classifier = classifier
        self._decomposer = decomposer
        self._triage = triage or Triage()
        self._router = router or SourceRouter()
        self._compound = compound
        self._render_fn = render_fn
        # Optional injected resolver (offline tests / custom backends). When
        # None we build the real Elasticsearch-backed resolver per call.
        self._resolver = resolver

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
                sid=sid,
                utterance=utterance,
                path="empty",
                reason="empty_utterance",
                answer="I didn't catch that — could you say it again?",
                t_start=t_start,
                timings=timings,
            )

        # 1+2+3 fan-out: classify runs in parallel with (load_session -> decompose).
        # Decompose needs session context (pending_slots merge, active_region),
        # so it can't run truly independent of session_load; but classify is
        # fully independent and is the biggest single LLM cost on the critical
        # path. Running them concurrently saves ~classify_ms on the happy
        # (non-escalate) path. On escalate we discard the decompose result —
        # the wasted token spend is negligible and the latency win is large.
        t_concurrent = time.perf_counter()

        async def _classify_leg() -> dict[str, Any]:
            t0 = time.perf_counter()
            v = await self._classifier.classify(utterance)
            timings["classify_ms"] = _ms(time.perf_counter() - t0)
            return v

        async def _session_decompose_leg() -> tuple[dict[str, Any], dict[str, Any], str]:
            t0 = time.perf_counter()
            sess = await self._sessions.load(sid)
            timings["session_load_ms"] = _ms(time.perf_counter() - t0)
            if region:
                sess["active_region"] = region
            decompose_input_local = utterance
            if sess.get("pending_slots"):
                prior_utt = None
                for turn in reversed(sess.get("history") or []):
                    if turn.get("is_clarification") and turn.get("utterance"):
                        prior_utt = turn["utterance"]
                        break
                if prior_utt:
                    decompose_input_local = f"{prior_utt}\nFollow-up: {utterance}"
                sess["pending_slots"] = []
            ctx_local = {
                "active_region": sess.get("active_region"),
                "active_hotel_id": sess.get("active_hotel_id"),
                "language": sess.get("language"),
            }
            t0 = time.perf_counter()
            decomp = await self._decomposer.decompose(decompose_input_local, ctx_local)
            timings["decompose_ms"] = _ms(time.perf_counter() - t0)
            return sess, decomp, decompose_input_local

        verdict, (session, decomposition, _decompose_input) = await asyncio.gather(
            _classify_leg(),
            _session_decompose_leg(),
        )
        timings["concurrent_pre_ms"] = _ms(time.perf_counter() - t_concurrent)

        if verdict.get("escalate"):
            answer = "Let me connect you to a colleague who can help with that right away."
            return self._finish(
                sid=sid,
                utterance=utterance,
                path=PATH_ESCALATE,
                reason="escalation_classifier",
                escalation=verdict,
                answer=answer,
                t_start=t_start,
                timings=timings,
            )

        # Promote a newly extracted region into the session so subsequent
        # turns don't have to re-state it.
        new_region = decomposition.get("region") or decomposition.get("city")
        if new_region:
            session["active_region"] = new_region

        # Backfill session language from the first decomposed turn.
        if decomposition.get("language") and not session.get("language"):
            session["language"] = decomposition["language"]

        # 4. Triage — may ask one clarification question and short-circuit.
        t0 = time.perf_counter()
        triage_decision = self._triage.assess(decomposition, session)
        timings["triage_ms"] = _ms(time.perf_counter() - t0)
        if triage_decision.get("ask"):
            session["pending_slots"] = list(triage_decision.get("pending_slots") or [])
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
                sid=sid,
                utterance=utterance,
                path="clarify",
                reason=triage_decision.get("reason", "clarification"),
                decomposition=decomposition,
                clarification={
                    "question": triage_decision.get("question"),
                    "slot": triage_decision.get("slot"),
                    "language": triage_decision.get("language"),
                },
                answer=triage_decision.get("question"),
                t_start=t_start,
                timings=timings,
            )

        # 5. Route.
        t0 = time.perf_counter()
        decision = self._router.route(decomposition, session)
        timings["route_ms"] = _ms(time.perf_counter() - t0)
        path = decision.get("path", PATH_BROAD)

        # A broad/multi-hotel recommendation means the caller has moved off any
        # specific hotel — drop the stale active_hotel_id so it can't shadow a
        # later scoped follow-up (and so the next turn isn't poisoned by it).
        if path == PATH_BROAD:
            session.pop("active_hotel_id", None)

        # 6. Execute path.
        retrieval: dict[str, Any] | None = None
        answer: str | None = None

        # 6a. Hotel resolution — resolve the name mention to a hotel_id,
        #     then proceed as a scoped KB query filtered to that hotel.
        if path == PATH_HOTEL_RESOLVE:
            hotel_mention = (decomposition.get("hotel_mention") or "").strip()
            if hotel_mention:
                t0 = time.perf_counter()
                if self._resolver is not None:
                    resolution = await self._resolver.resolve(hotel_mention)
                else:
                    import aiohttp as _aio

                    async with _aio.ClientSession() as _resolve_http:
                        resolution = await HotelResolver(session=_resolve_http).resolve(
                            hotel_mention
                        )
                timings["resolve_ms"] = _ms(time.perf_counter() - t0)
                if resolution.get("decision") == "auto_resolve" and resolution.get("hotel_id"):
                    # Promote to scoped query with the resolved hotel_id.
                    decomposition["hotel_id"] = resolution["hotel_id"]
                    session["active_hotel_id"] = resolution["hotel_id"]
                    path = PATH_SCOPED
                    decision["path"] = PATH_SCOPED
                    decision["reason"] = "hotel_resolved_inline"

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
            sid=sid,
            utterance=utterance,
            path=path,
            reason=decision.get("reason", path),
            decomposition=decomposition,
            router=decision,
            retrieval=retrieval,
            answer=answer,
            t_start=t_start,
            timings=timings,
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
                "hotels": [],
                "missing_requirements": [],
            }
        region = decomposition.get("region") or session.get("active_region") or ""
        requirements = list(decomposition.get("requirements") or [])
        # Source the resolved hotel id from EITHER the decomposition (set by the
        # inline resolver on the PATH_HOTEL_RESOLVE branch) OR the session
        # (set on a prior turn; router then returns PATH_SCOPED directly with
        # reason "hotel_resolved"). Reading only `decomposition` silently drops
        # the scope on the session-resolved path, degrading a scoped lookup to a
        # generic broad search and returning the wrong hotel. See _run_kb scope
        # filter below.
        hotel_id = (
            (decomposition.get("hotel_id") or session.get("active_hotel_id"))
            if path == PATH_SCOPED
            else None
        )
        # Scoped query about a known hotel but no specific requirement → fall
        # back to a generic overview instead of failing closed (empty_requirements).
        if path == PATH_SCOPED and hotel_id and not requirements:
            requirements = list(_SCOPED_DEFAULT_REQUIREMENTS)
            logger.info(
                "scoped query for hotel_id={!r} had no requirements — injecting overview default",
                hotel_id,
            )
        if path == PATH_SCOPED and not hotel_id:
            # Scoped path reached without a resolved hotel id — the query will
            # degrade to a generic broad search over `requirements`. Surface it
            # rather than silently answering about the wrong hotel.
            logger.warning(
                "scoped path with unresolved hotel_id (mention={!r}) — "
                "retrieval will not be hotel-scoped",
                decomposition.get("hotel_mention"),
            )
        result = await self._compound.discover(
            region=region,
            requirements=requirements,
            activity_tags=None,
            category_hint=None,
            hotel_id=hotel_id,
        )
        # For scoped queries, filter results to the resolved hotel only.
        if hotel_id and path == PATH_SCOPED:
            result["hotels"] = [
                h for h in result.get("hotels", []) if h.get("hotel_id") == hotel_id
            ]
        return result

    async def _render(
        self,
        utterance: str,
        decomposition: dict[str, Any],
        retrieval: dict[str, Any] | None,
        session: dict[str, Any],
    ) -> str:
        """Call the injected render_fn, or return a defensive fallback.

        Fails closed when retrieval produced zero hotels — the LLM has
        no evidence to ground on and tends to invent geography ("scoped
        to Paris") if asked to generate prose anyway.
        """
        hotels = (retrieval or {}).get("hotels") or []
        if not hotels:
            lang = (decomposition.get("language") or session.get("language") or "en").lower()
            region = (
                decomposition.get("region")
                or decomposition.get("city")
                or session.get("active_region")
                or ""
            )
            return _no_match_answer(lang, region)
        if self._render_fn is None:
            names = ", ".join(
                (h.get("payload") or {}).get("hotel_name", h.get("hotel_id")) for h in hotels[:3]
            )
            return f"Top matches: {names}."
        try:
            return await self._render_fn(
                {
                    "utterance": utterance,
                    "region": decomposition.get("region") or session.get("active_region"),
                    "decomposition": decomposition,
                    "retrieval": retrieval or {},
                }
            )
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
        result = {
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
        self._log_query(result)
        return result

    def _log_query(self, result: dict[str, Any]) -> None:
        """Append a structured NDJSON record to the concierge query log."""
        try:
            log_dir = Path(os.environ.get("CONCIERGE_LOG_DIR", "logs"))
            log_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            log_file = log_dir / f"concierge-{today}.jsonl"
            # Compact retrieval: keep hotel_ids + scores but drop full evidence text
            retrieval = result.get("retrieval") or {}
            hotels_summary = [
                {
                    "hotel_id": h.get("hotel_id"),
                    "score": round(float(h.get("score", 0)), 3),
                    "name": (h.get("payload") or {}).get("hotel_name"),
                }
                for h in retrieval.get("hotels", [])
            ]
            record = {
                "ts": datetime.now(UTC).isoformat(),
                "session_id": result.get("session_id"),
                "utterance": result.get("utterance"),
                "path": result.get("path"),
                "reason": result.get("reason"),
                "decomposition": result.get("decomposition"),
                "router": result.get("router"),
                "retrieval_summary": {
                    "hotels": hotels_summary,
                    "count": len(hotels_summary),
                    "region": retrieval.get("region"),
                    "missing_requirements": retrieval.get("missing_requirements", []),
                    "reason": retrieval.get("reason"),
                },
                "answer_length": len(result.get("answer") or ""),
                "timings": result.get("timings"),
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug("concierge log write failed: {}", e)
