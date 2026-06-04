"""YouTube Data API v3 — find videos for a hotel.

This module provides a single async function — :func:`youtube_search` —
that calls YouTube's `search.list` endpoint and returns ranked video
results. Like :mod:`voxtera.search`, it is deliberately not wired into
the bot pipeline here; the LLM tool wiring lives in
:mod:`voxtera.actions.integration`.

What it is for: guests often ask "do you have a video of this hotel?",
"can I see the rooms?", or "is there a tour?". The hotel KB has text
chunks but not video. YouTube fills that gap with rich, free,
guest-friendly content from official hotel channels, travel vloggers,
and review sites.

Provider: Google's YouTube Data API v3. Auth is a single API key (no
OAuth). Quota: 10,000 units/day free; `search.list` costs 100 units →
100 searches/day free, then $5 per extra 1k queries. Dashboard:
https://console.cloud.google.com/apis/dashboard

Configuration: set ``YOUTUBE_API_KEY`` in the environment (see the
"YouTube videos" section of ``.env``).

Conventions: async throughout, no blocking calls, all secrets from
env, loguru logging, type hints everywhere.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import aiohttp
from loguru import logger

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# Hard ceiling on the HTTP round-trip. A voice turn cannot wait forever —
# if YouTube is slow, fail fast so the caller can apologise and move on.
_DEFAULT_TIMEOUT_S = 6.0
_DEFAULT_MAX_RESULTS = 5

# Intent → query template + ordering. Kept here so handler code stays
# focused on shaping results for the LLM rather than building queries.
_INTENT_TEMPLATES: dict[str, tuple[str, str]] = {
    # intent: (query suffix, order)  — see Google's `order` param docs
    "tour":   ("hotel tour walkthrough", "relevance"),
    "rooms":  ("hotel room tour", "relevance"),
    "review": ("hotel review", "relevance"),
    "news":   ("hotel", "date"),  # recency-first for news
}
_DEFAULT_INTENT = "tour"


class YouTubeSearchError(RuntimeError):
    """Raised when a YouTube search cannot be completed.

    Carries a human-readable reason. Callers (eventually the LLM tool
    handler) should catch this and degrade gracefully rather than crash
    the voice loop.
    """


@dataclass
class VideoHit:
    """A single ranked video result."""

    video_id: str
    title: str
    channel_title: str
    published_at: str  # ISO-8601 UTC; leave as string for the LLM to read
    description: str
    thumbnail_url: str

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class VideoSearchResult:
    """The outcome of a YouTube search."""

    query: str
    intent: str
    hits: list[VideoHit] = field(default_factory=list)
    elapsed_ms: float = 0.0


def build_query(hotel_name: str, intent: str = _DEFAULT_INTENT, *, region: str | None = None) -> str:
    """Compose a hotel-aware search query for a given intent.

    Exposed as a module function so the handler (and tests) can call it
    without instantiating any HTTP client.
    """
    suffix, _order = _INTENT_TEMPLATES.get(intent, _INTENT_TEMPLATES[_DEFAULT_INTENT])
    parts = [hotel_name.strip(), suffix]
    if region:
        parts.append(region.strip())
    return " ".join(p for p in parts if p)


def _order_for_intent(intent: str) -> str:
    return _INTENT_TEMPLATES.get(intent, _INTENT_TEMPLATES[_DEFAULT_INTENT])[1]


async def youtube_search(
    hotel_name: str,
    *,
    intent: str = _DEFAULT_INTENT,
    region: str | None = None,
    max_results: int = _DEFAULT_MAX_RESULTS,
    language: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    api_key: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> VideoSearchResult:
    """Run a YouTube search and return ranked video results.

    The function is self-contained: pass an existing ``session`` to share
    a connection pool with the rest of the bot, or omit it and a new one
    is created and disposed per call (handy for one-shot scripts and
    tests).
    """
    key = (api_key or os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if not key:
        raise YouTubeSearchError("YOUTUBE_API_KEY is not set")

    query = build_query(hotel_name, intent=intent, region=region)
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": str(max(1, min(max_results, 25))),
        "order": _order_for_intent(intent),
        "safeSearch": "moderate",
        "key": key,
    }
    if language:
        params["relevanceLanguage"] = language[:2].lower()

    t0 = time.perf_counter()
    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        try:
            async with sess.get(YOUTUBE_SEARCH_URL, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("[youtube] HTTP {} for query={!r}: {}", resp.status, query, body[:200])
                    raise YouTubeSearchError(f"YouTube API returned HTTP {resp.status}")
                payload = await resp.json()
        except aiohttp.ClientError as exc:
            raise YouTubeSearchError(f"YouTube transport error: {exc}") from exc
    finally:
        if own_session:
            await sess.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    hits = _parse_hits(payload)
    logger.info("[youtube] query={!r} intent={} hits={} took={:.0f}ms",
                query, intent, len(hits), elapsed_ms)
    return VideoSearchResult(query=query, intent=intent, hits=hits, elapsed_ms=elapsed_ms)


def _parse_hits(payload: dict) -> list[VideoHit]:
    items = payload.get("items") or []
    hits: list[VideoHit] = []
    for item in items:
        # YouTube's response wraps the video id in {"id": {"kind": "...", "videoId": "..."}}
        # but `kind: youtube#video` is guaranteed because we asked for type=video.
        video_id = ((item.get("id") or {}).get("videoId") or "").strip()
        if not video_id:
            continue
        snip = item.get("snippet") or {}
        thumbs = (snip.get("thumbnails") or {})
        # Prefer the highest-res thumbnail we asked for (snippet always gives default+medium+high).
        thumb_url = (
            (thumbs.get("high") or {}).get("url")
            or (thumbs.get("medium") or {}).get("url")
            or (thumbs.get("default") or {}).get("url")
            or ""
        )
        hits.append(VideoHit(
            video_id=video_id,
            title=(snip.get("title") or "").strip(),
            channel_title=(snip.get("channelTitle") or "").strip(),
            published_at=(snip.get("publishedAt") or "").strip(),
            description=(snip.get("description") or "").strip(),
            thumbnail_url=thumb_url,
        ))
    return hits
