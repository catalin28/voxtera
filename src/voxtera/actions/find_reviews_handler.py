"""Function handler for the ``find_hotel_reviews`` LLM tool.

When the model invokes ``find_hotel_reviews``, Pipecat calls the handler
returned by :func:`make_find_reviews_handler`, which:

1. Extracts ``hotel_name`` plus the optional ``region_hint`` and
   ``language`` arguments.
2. Calls :func:`voxtera.google_places.find_hotel_reviews` (async
   Google Places API New).
3. Returns the aggregate rating + up to five most-recent reviews as a
   compact summary for the model to read out.

On any failure (missing key, no match, transport error) the handler
returns a graceful "unavailable" payload — explicitly NOT instructing
the model to invent or paraphrase reviews it does not have.
"""

from __future__ import annotations

from loguru import logger
from pipecat.services.llm_service import FunctionCallHandler, FunctionCallParams

from voxtera.google_places import HotelReviewsResult, PlacesError, find_hotel_reviews


def _format_result_for_llm(result: HotelReviewsResult) -> str:
    parts: list[str] = []
    header = result.display_name or "Unknown property"
    if result.rating is not None:
        header += f" — {result.rating:.1f}★ ({result.user_rating_count} ratings)"
    else:
        header += " — (no aggregate rating yet)"
    parts.append(header)

    if result.formatted_address:
        parts.append(result.formatted_address)
    if result.google_maps_uri:
        parts.append(f"Google Maps: {result.google_maps_uri}")

    if not result.reviews:
        parts.append("\nNo individual reviews returned.")
    else:
        parts.append("\nMost recent reviews:")
        for i, r in enumerate(result.reviews, 1):
            parts.append(f"  {i}. {r.author_name} — {r.rating}★ ({r.relative_time})")
            if r.text:
                # Cap each review so the model context stays tight; the
                # model only needs the gist.
                parts.append(f"     {r.text[:280]}")

    parts.append(f"\n(Places lookup took {result.elapsed_ms:.0f}ms)")
    return "\n".join(parts)


def make_find_reviews_handler() -> FunctionCallHandler:
    """Build a Pipecat function handler for the ``find_hotel_reviews`` tool."""

    async def handler(params: FunctionCallParams) -> None:
        args = params.arguments or {}
        hotel_name = (args.get("hotel_name") or "").strip()
        region_hint = (args.get("region_hint") or "").strip() or None
        language = (args.get("language") or "").strip() or None

        if not hotel_name:
            logger.warning("[find-reviews] tool called with empty hotel_name")
            await params.result_callback(
                {
                    "status": "error",
                    "guidance": (
                        "Hotel name was missing. Ask the guest which property's "
                        "reviews they'd like to hear."
                    ),
                }
            )
            return

        try:
            result = await find_hotel_reviews(
                hotel_name, region_hint=region_hint, language=language,
            )
        except PlacesError as exc:
            logger.warning("[find-reviews] lookup failed: {}", exc)
            await params.result_callback(
                {
                    "status": "unavailable",
                    "guidance": (
                        "Public reviews could not be retrieved right now. "
                        "Apologise briefly to the guest. Do NOT make up review "
                        "quotes or scores."
                    ),
                }
            )
            return

        formatted = _format_result_for_llm(result)
        logger.info(
            "[find-reviews] tool result: rating={} reviews={}",
            result.rating, len(result.reviews),
        )

        await params.result_callback(
            {
                "status": "success",
                "reviews": formatted,
                "guidance": (
                    "Summarise the rating and the overall theme of the recent "
                    "reviews in 1-2 spoken sentences. Quote at most one short "
                    "phrase. Do NOT read URLs or full review text aloud."
                ),
            }
        )

    return handler
