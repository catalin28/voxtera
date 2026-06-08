"""Function handler for the ``find_hotel_videos`` LLM tool.

When the model invokes ``find_hotel_videos``, Pipecat calls the handler
returned by :func:`make_find_videos_handler`, which:

1. Extracts ``hotel_name`` plus the optional ``intent``, ``region``,
   ``language``, and ``max_results`` arguments.
2. Calls :func:`voxtera.youtube.youtube_search` (async YouTube Data v3).
3. Formats the ranked video list for the model so it can choose 1-2
   to mention out loud.

On failure (timeout, missing key, HTTP error) the handler returns a
graceful "unavailable" payload with explicit instructions NOT to invent
a video URL.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallHandler, FunctionCallParams

from voxtera.youtube import VideoSearchResult, YouTubeSearchError, youtube_search


def _format_result_for_llm(result: VideoSearchResult) -> str:
    if not result.hits:
        return f"No YouTube videos found for query: {result.query!r}."

    parts = [f"YouTube results for {result.query!r} (intent={result.intent}):"]
    for i, hit in enumerate(result.hits, 1):
        parts.append(f"  {i}. {hit.title} — {hit.channel_title} ({hit.published_at[:10]})")
        parts.append(f"     {hit.url}")
        if hit.description:
            # Cap description so the model context stays tight.
            parts.append(f"     {hit.description[:200]}")
    parts.append(f"\n(YouTube search took {result.elapsed_ms:.0f}ms)")
    return "\n".join(parts)


def make_find_videos_handler() -> FunctionCallHandler:
    """Build a Pipecat function handler for the ``find_hotel_videos`` tool."""

    async def handler(params: FunctionCallParams) -> None:
        args = params.arguments or {}
        hotel_name = (args.get("hotel_name") or "").strip()
        intent = (args.get("intent") or "tour").strip().lower() or "tour"
        region = (args.get("region") or "").strip() or None
        language = (args.get("language") or "").strip() or None
        # The model may pass max_results as int or string; coerce safely.
        try:
            max_results = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        max_results = max(1, min(max_results, 10))

        if not hotel_name:
            logger.warning("[find-videos] tool called with empty hotel_name")
            await params.result_callback(
                {
                    "status": "error",
                    "guidance": (
                        "Hotel name was missing. Ask the guest which property "
                        "they'd like to see videos of."
                    ),
                }
            )
            return

        try:
            result = await youtube_search(
                hotel_name,
                intent=intent,
                region=region,
                max_results=max_results,
                language=language,
            )
        except YouTubeSearchError as exc:
            logger.warning("[find-videos] search failed: {}", exc)
            await params.result_callback(
                {
                    "status": "unavailable",
                    "guidance": (
                        "The video lookup could not be completed right now. "
                        "Apologise briefly to the guest. Do NOT invent a YouTube "
                        "link — say you'll follow up if needed."
                    ),
                }
            )
            return

        formatted = _format_result_for_llm(result)
        logger.info("[find-videos] tool result: {} hits, intent={}", len(result.hits), result.intent)

        await params.result_callback(
            {
                "status": "success" if result.hits else "no_results",
                "videos": formatted,
                "guidance": (
                    "Pick the single best-matching video and offer it to the "
                    "guest with a one-line description (e.g. 'I found a 3-minute "
                    "room tour from <channel> — would you like the link?'). Do "
                    "NOT read the URL aloud; if they say yes, send it via the "
                    "chat or SMS channel."
                ),
            }
        )

    return handler
