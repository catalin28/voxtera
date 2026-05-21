"""Manual test: run a live Tavily web search and print the result.

Run from the project root:

    uv run python scripts/test_web_search.py
    uv run python scripts/test_web_search.py "is the Louvre open on Mondays?"

Requires ``TAVILY_API_KEY`` in ``.env`` (see the "Web search" section).

Expected outcome:
    * The synthesized answer and a few ranked source results print to the
      terminal, followed by ``✓ Search OK.``
    * Exit code 0 on success, 1 on any failure.

Note: this hits the real Tavily API and spends one search credit per run.
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger

from voxtera.search import WebSearchError, web_search

# Used when no query is passed on the command line. A weather question is a
# good smoke test: it can only be answered with live web data — never from the
# hotel RAG knowledge base or the model's training.
_DEFAULT_QUERY = "What is the weather in Paris right now?"


async def _run() -> int:
    """Run one web search and report the outcome. Returns a process exit code."""
    load_dotenv()

    query = " ".join(sys.argv[1:]).strip() or _DEFAULT_QUERY
    logger.info("Testing web search with query: {!r}", query)

    try:
        result = await web_search(query)
    except WebSearchError as exc:
        logger.error("✗ Search failed: {}", exc)
        return 1

    print()
    print(f"Query:   {result.query}")
    print(f"Latency: {result.elapsed_ms:.0f} ms")
    print()
    if result.answer:
        print("Synthesized answer:")
        print(f"  {result.answer}")
    else:
        print("Synthesized answer: (none returned)")
    print()
    print(f"Sources ({len(result.hits)}):")
    for i, hit in enumerate(result.hits, start=1):
        snippet = hit.content[:160].replace("\n", " ")
        print(f"  {i}. [{hit.score:.2f}] {hit.title}")
        print(f"     {hit.url}")
        print(f"     {snippet}")
    print()

    if not result.hits and not result.answer:
        logger.error("✗ Search returned no answer and no results.")
        return 1

    logger.info("✓ Search OK.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
