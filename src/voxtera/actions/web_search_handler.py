"""Function handler for the ``web_search`` LLM tool.

When Claude invokes the ``web_search`` tool, Pipecat calls the handler
returned by :func:`make_web_search_handler`. The handler:

1. Extracts the ``query`` argument.
2. Calls :func:`voxtera.search.web_search` (async Tavily call).
3. Formats the result (answer + ranked source snippets) as the tool result
   returned to Claude, so it can synthesize a concise spoken reply.

On failure (timeout, missing key, HTTP error), the handler returns a
graceful fallback message so Claude can apologize without crashing.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallHandler, FunctionCallParams

from voxtera.search import SearchResult, WebSearchError, web_search


def _format_result_for_llm(result: SearchResult) -> str:
    """Format a SearchResult into a compact string for the LLM tool result."""
    parts: list[str] = []

    if result.answer:
        parts.append(f"Synthesized answer: {result.answer}")

    if result.hits:
        parts.append("\nSources (ranked by relevance):")
        for i, hit in enumerate(result.hits, 1):
            parts.append(f"  {i}. [{hit.title}]({hit.url})")
            if hit.content:
                # Keep snippets concise — the LLM only needs enough to compose
                # a short spoken reply.
                snippet = hit.content[:300]
                parts.append(f"     {snippet}")

    parts.append(f"\n(Search took {result.elapsed_ms:.0f}ms)")
    return "\n".join(parts)


def make_web_search_handler() -> FunctionCallHandler:
    """Build a Pipecat function handler for the ``web_search`` tool.

    The returned handler matches Pipecat's ``FunctionCallHandler`` protocol:
    a single async function taking :class:`FunctionCallParams` and calling
    ``params.result_callback`` exactly once.
    """

    async def handler(params: FunctionCallParams) -> None:
        args = params.arguments or {}
        query = (args.get("query") or "").strip()

        if not query:
            logger.warning("[web-search] tool called with empty query")
            await params.result_callback(
                {
                    "status": "error",
                    "guidance": (
                        "The search query was empty. Ask the guest to clarify "
                        "what they would like you to look up."
                    ),
                }
            )
            return

        try:
            result = await web_search(query)
        except WebSearchError as exc:
            logger.warning("[web-search] search failed: {}", exc)
            await params.result_callback(
                {
                    "status": "unavailable",
                    "guidance": (
                        "The web search could not be completed right now. "
                        "Apologize briefly to the guest and say you couldn't "
                        "confirm that information at the moment. Do NOT make "
                        "up an answer."
                    ),
                }
            )
            return

        formatted = _format_result_for_llm(result)
        logger.info(
            "[web-search] tool result: {} hits, answer={}",
            len(result.hits),
            "yes" if result.answer else "no",
        )

        await params.result_callback(
            {
                "status": "success",
                "search_results": formatted,
                "guidance": (
                    "Use the search results above to compose a SHORT spoken "
                    "reply in the guest's language. Prefer official/authoritative "
                    "sources over forum posts or social media when they disagree. "
                    "Do NOT read URLs aloud. Keep it to 1-2 sentences."
                ),
            }
        )

    return handler
