"""Tavily web search — standalone search capability for Voxtera.

This module provides the core async web-search function only. It is
deliberately **not** wired into the bot pipeline: turning it into an LLM
`web_search` tool (schema + `wire_actions`-style registration + latency
masking) is a separate step — see ``docs/WEB_SEARCH_PLAN.md``.

What it is for: the hotel concierge often gets live, time-sensitive questions
that neither the curated RAG knowledge base nor the model's own training can
answer — today's weather, this week's events, holiday opening hours, transit
disruptions. This function lets it look those up on the web.

Provider: Tavily (https://tavily.com). Chosen because it is built for LLM
agents — a single call returns a synthesized ``answer`` plus ranked source
snippets, which suits a voice bot that must speak a short reply rather than
read a web page aloud. Free tier: 1,000 searches/month.

Configuration: set ``TAVILY_API_KEY`` in the environment (see the "Web search"
section of ``.env``).

Conventions: async throughout (no blocking calls), all secrets from env,
loguru logging, type hints on every signature.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import aiohttp
from loguru import logger

# Tavily REST search endpoint. Auth is a Bearer token; body is JSON.
TAVILY_SEARCH_URL = "https://api.tavily.com/search"

# Hard ceiling on the HTTP round-trip. A voice turn cannot wait forever — if
# Tavily is slow, fail fast so the caller can apologise and move on.
_DEFAULT_TIMEOUT_S = 8.0


class WebSearchError(RuntimeError):
    """Raised when a web search cannot be completed.

    Carries a human-readable reason. Callers (eventually the LLM tool handler)
    should catch this and degrade gracefully rather than crash the voice loop.
    """


@dataclass
class SearchHit:
    """A single ranked search result from the web."""

    title: str
    url: str
    content: str  # short relevance-scored snippet, not the full page
    score: float


@dataclass
class SearchResult:
    """The outcome of a web search.

    ``answer`` is Tavily's synthesized one-paragraph answer to the query — the
    most useful field for a voice reply. ``hits`` are the underlying sources,
    handy for attribution or when the answer is empty.
    """

    query: str
    answer: str | None
    hits: list[SearchHit] = field(default_factory=list)
    elapsed_ms: float = 0.0


async def web_search(
    query: str,
    *,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
    include_domains: list[str] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    api_key: str | None = None,
) -> SearchResult:
    """Run a web search via the Tavily API and return structured results.

    Args:
        query: The search query — typically the guest's question, rephrased
            into a clean search string by the LLM.
        max_results: How many source results to return (Tavily's default is 5).
        search_depth: ``"basic"`` (1 Tavily credit, faster — the right default
            for a latency-sensitive voice turn) or ``"advanced"`` (2 credits,
            deeper retrieval).
        include_answer: Ask Tavily to also return a synthesized short answer.
            Kept on by default — it is what a voice reply is built from.
        timeout_s: Hard timeout for the whole HTTP round-trip.
        api_key: Override the ``TAVILY_API_KEY`` environment variable. Mainly
            useful for tests; production reads the env var.

    Returns:
        A :class:`SearchResult` with the synthesized answer (if any) and the
        ranked source hits.

    Raises:
        WebSearchError: missing API key, empty query, timeout, transport
            failure, non-200 response, or an unparseable response body.
    """
    key = (api_key or os.environ.get("TAVILY_API_KEY", "")).strip()
    if not key:
        raise WebSearchError(
            "TAVILY_API_KEY is not set — add your key to the 'Web search' " "section of .env."
        )

    cleaned = query.strip()
    if not cleaned:
        raise WebSearchError("Refusing to search: the query is empty.")

    payload = {
        "query": cleaned,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        "topic": "general",
    }
    # Restrict to specific sites (e.g. review domains: TripAdvisor, Booking, …)
    # so a "reviews" question pulls from review sources, not the open web.
    if include_domains:
        payload["include_domains"] = list(include_domains)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    logger.info("[web-search] query={!r} depth={}", cleaned, search_depth)
    t0 = time.monotonic()

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(TAVILY_SEARCH_URL, json=payload, headers=headers) as resp,
        ):
            status = resp.status
            raw = await resp.text()
    except TimeoutError as exc:
        # aiohttp raises asyncio.TimeoutError (== builtin TimeoutError on 3.11+).
        raise WebSearchError(f"Web search timed out after {timeout_s:.0f}s.") from exc
    except aiohttp.ClientError as exc:
        raise WebSearchError(f"Web search request failed: {exc}") from exc

    elapsed_ms = (time.monotonic() - t0) * 1000.0

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebSearchError(f"Tavily response was not valid JSON (HTTP {status}).") from exc

    if status != 200:
        detail = body.get("detail") if isinstance(body, dict) else raw[:200]
        raise WebSearchError(f"Tavily returned HTTP {status}: {detail}")

    if not isinstance(body, dict):
        raise WebSearchError("Tavily response was not a JSON object.")

    hits = [
        SearchHit(
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            content=str(item.get("content", "")),
            score=float(item.get("score", 0.0) or 0.0),
        )
        for item in body.get("results", [])
        if isinstance(item, dict)
    ]

    answer = body.get("answer")
    result = SearchResult(
        query=cleaned,
        answer=str(answer).strip() if answer else None,
        hits=hits,
        elapsed_ms=elapsed_ms,
    )
    logger.info(
        "[web-search] {} hit(s) in {:.0f}ms (answer={})",
        len(hits),
        elapsed_ms,
        "yes" if result.answer else "no",
    )
    return result
