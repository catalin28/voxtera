"""Web retrieval for the call-center concierge (Phase 4).

Wraps the existing Tavily web search (``voxtera.search.web_search``) for the
two dynamic paths the router emits:

  - PATH_WEB    — events, local operators, weather, real-time practical info
  - PATH_HYBRID — hotel KB + a live web lookup (e.g. "dive shops near my hotel")

Tavily returns a synthesized ``answer`` plus ranked source snippets, which is
exactly what a voice/text concierge needs — a short grounded reply with
citations rather than a page to read aloud.

The search backend is dependency-injected (``search_fn``) so tests run fully
offline. The default backend calls Tavily and needs ``TAVILY_API_KEY``.

Decision contract (returned by ``WebRetriever.discover``):

    {
      "query":   str,            # the constructed search query
      "answer":  str | None,     # Tavily's synthesized answer
      "sources": list[dict],     # [{title, url, snippet, score}]
      "reason":  str | None,     # None on success; else "no_web_result" | "web_error"
      "elapsed_ms": float,
    }
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

REASON_NO_RESULT = "no_web_result"
REASON_ERROR = "web_error"

# search_fn(query, *, max_results) -> object with .answer (str|None) and
# .hits (list of objects with .title/.url/.content/.score). Matches
# voxtera.search.web_search's SearchResult.
SearchFn = Callable[..., Awaitable[Any]]


class WebRetriever:
    """Live web lookup for the web / hybrid concierge paths."""

    def __init__(
        self,
        *,
        search_fn: SearchFn | None = None,
        max_results: int = 5,
    ) -> None:
        self._search_fn = search_fn
        self._max_results = max_results

    async def discover(
        self,
        utterance: str,
        decomposition: dict[str, Any] | None = None,
        *,
        hotel_name: str | None = None,
        location: str | None = None,
        query: str | None = None,
        include_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        decomposition = decomposition or {}
        # A `query` passed in is the dialog-aware rewrite (built by an LLM from the
        # full conversation) — it wins, because it resolves pronouns and the real
        # hotel name without depending on ES or the decomposition. Fall back to the
        # heuristic only when no rewrite was provided.
        query = (query or "").strip() or self._build_query(
            utterance, decomposition, hotel_name=hotel_name, location=location
        )
        if not query:
            return self._empty(query, REASON_NO_RESULT)

        try:
            result = await self._search(query, include_domains=include_domains)
        except Exception as e:  # noqa: BLE001 — degrade gracefully, never crash the turn
            logger.warning("WebRetriever search failed for {!r}: {}", query, e)
            return self._empty(query, REASON_ERROR)

        answer = getattr(result, "answer", None)
        hits = getattr(result, "hits", None) or []
        sources = [
            {
                "title": getattr(h, "title", ""),
                "url": getattr(h, "url", ""),
                "snippet": (getattr(h, "content", "") or "")[:800],
                "score": round(float(getattr(h, "score", 0.0) or 0.0), 3),
            }
            for h in hits
        ]
        if not answer and not sources:
            return self._empty(query, REASON_NO_RESULT)

        return {
            "query": query,
            "answer": (answer or "").strip() or None,
            "sources": sources,
            "reason": None,
            "elapsed_ms": round(float(getattr(result, "elapsed_ms", 0.0) or 0.0), 1),
        }

    # --- internals ---

    async def _search(self, query: str, *, include_domains: list[str] | None = None) -> Any:
        if self._search_fn is not None:
            try:
                return await self._search_fn(
                    query, max_results=self._max_results, include_domains=include_domains
                )
            except TypeError:
                # Test doubles may not accept include_domains — degrade gracefully.
                return await self._search_fn(query, max_results=self._max_results)
        # Default backend: the shared Tavily client. Imported lazily so tests
        # that inject search_fn don't need the dependency or an API key.
        from voxtera.search import web_search

        # "advanced" pulls fuller page content per source, so the synth step can
        # catch nuance (e.g. spa on-site vs scuba arranged via nearby operators)
        # that "basic" flattens. Costs a bit more latency/credits — worth it for a
        # correct answer.
        return await web_search(
            query,
            max_results=self._max_results,
            search_depth="advanced",
            include_domains=include_domains,
        )

    @staticmethod
    def _build_query(
        utterance: str,
        decomposition: dict[str, Any],
        *,
        hotel_name: str | None = None,
        location: str | None = None,
    ) -> str:
        """Build the search string.

        When the question is about a specific hotel, the raw utterance is useless
        for the web — it often uses a pronoun ("is there scuba diving THERE?") and
        omits the hotel name entirely, so Tavily returns generic chatter. In that
        case build the query from the RESOLVED hotel name + location + the
        requirement phrases instead of the spoken words. A `hotel_name` passed by
        the pipeline (the resolved KB name) wins over the decomposer's raw mention.
        """
        hotel = (hotel_name or decomposition.get("hotel_mention") or "").strip()
        geo = (location or decomposition.get("city") or decomposition.get("region") or "").strip()
        reqs = [r.strip() for r in (decomposition.get("requirements") or []) if r and r.strip()]

        if hotel:
            parts = [hotel]
            if geo and geo.lower() not in hotel.lower():
                parts.append(geo)
            # Prefer the structured requirements ("scuba diving", "spa"); fall back
            # to the raw utterance only if there are none.
            if reqs:
                parts.extend(reqs)
            else:
                parts.append((utterance or "").strip())
            return " ".join(p for p in parts if p).strip()

        q = (utterance or "").strip()
        if not q:
            return ""
        if geo and geo.lower() not in q.lower():
            q = f"{q} {geo}"
        return q

    @staticmethod
    def _empty(query: str, reason: str) -> dict[str, Any]:
        return {
            "query": query,
            "answer": None,
            "sources": [],
            "reason": reason,
            "elapsed_ms": 0.0,
        }
