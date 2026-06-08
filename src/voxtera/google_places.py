"""Google Places API (New) — find a hotel and read its public reviews.

This module exposes one async entry point — :func:`find_hotel_reviews` —
that takes a hotel name (plus optional locality hints), resolves it to
a Google Places ``places/<id>``, and returns Google's public rating
plus up to five most-recent reviews. Like :mod:`voxtera.youtube`, it is
deliberately decoupled from the bot wiring; the LLM tool registration
happens in :mod:`voxtera.actions.integration`.

What it is for: guests increasingly ask "what do people say about this
hotel?" or "is it actually as nice as the photos?". Google Reviews is
the largest public review surface and the data is permissively usable
via the official Places API. Returning the rating + 5 recent reviews
matches what guests see when they tap a hotel pin in Google Maps, so
it is the right primary signal.

Provider: Google's Places API (New) v1 — the GA replacement for the
legacy `findplacefromtext` / `details` endpoints. Auth: API key passed
as ``X-Goog-Api-Key``. Pricing: ~$17 per 1k Place Details calls; first
$200/mo free credit covers ~11k calls. Enable "Places API (New)" in
the Google Cloud project that owns ``GOOGLE_PLACES_API_KEY``.

Configuration: ``GOOGLE_PLACES_API_KEY`` in the environment. The
client is **safe to import** before the key is set — the missing-key
case raises :class:`PlacesError` only at call time, so the module can
ship before billing is configured.

Conventions: async throughout, no blocking calls, secrets from env,
loguru logging, type hints everywhere. Field masks are kept minimal so
we only pay for what we actually display.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import aiohttp
from loguru import logger

# Places API (New) — v1. The legacy "maps.googleapis.com/maps/api/place/*"
# endpoints still work but are on the deprecation path; New v1 is what
# Google steers all new integrations to.
PLACES_SEARCH_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL_TEMPLATE = "https://places.googleapis.com/v1/{place_name}"

# Field masks — we ask for only what we render. Each extra field can
# bump the SKU tier, so this is also a cost control.
_SEARCH_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount"
_DETAILS_FIELD_MASK = "id,displayName,formattedAddress,rating,userRatingCount,reviews,googleMapsUri"

_DEFAULT_TIMEOUT_S = 6.0
_MAX_REVIEWS_RETURNED = 5  # Google itself returns up to 5 most-recent.


class PlacesError(RuntimeError):
    """Raised when a Places lookup cannot be completed (auth, transport, or no match)."""


@dataclass
class Review:
    author_name: str
    rating: int  # 1..5
    text: str
    relative_time: str  # "2 weeks ago" — already localized by Google
    language: str       # ISO code Google detected for the text


@dataclass
class HotelReviewsResult:
    place_id: str  # "places/<opaque-id>" — the New API uses this resource name
    display_name: str
    formatted_address: str
    rating: float | None              # 1.0..5.0 aggregate, None if no ratings yet
    user_rating_count: int            # total ratings backing the aggregate
    google_maps_uri: str              # canonical Google Maps URL for the place
    reviews: list[Review] = field(default_factory=list)
    elapsed_ms: float = 0.0


async def find_hotel_reviews(
    hotel_name: str,
    *,
    region_hint: str | None = None,
    language: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    api_key: str | None = None,
    session: aiohttp.ClientSession | None = None,
) -> HotelReviewsResult:
    """Resolve a hotel name to a Google Place and return its reviews.

    The lookup is a two-step dance because Places (New) separates
    "find a place by free-text" from "fetch full details for a known
    place id". Each step is one HTTP round-trip; both share the
    provided ``session`` when given.

    ``region_hint`` is appended to the search text to disambiguate
    common hotel names (e.g. "Hilton" without a city → useless). It is
    optional but strongly recommended.
    """
    key = (api_key or os.environ.get("GOOGLE_PLACES_API_KEY") or "").strip()
    if not key:
        raise PlacesError("GOOGLE_PLACES_API_KEY is not set")

    t0 = time.perf_counter()
    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        place_name = await _search_first_place(
            sess, hotel_name, region_hint=region_hint, language=language,
            timeout_s=timeout_s, api_key=key,
        )
        if not place_name:
            raise PlacesError(f"no Google place matched {hotel_name!r}")
        result = await _fetch_place_details(
            sess, place_name, language=language, timeout_s=timeout_s, api_key=key,
        )
    finally:
        if own_session:
            await sess.close()

    result.elapsed_ms = (time.perf_counter() - t0) * 1000.0
    logger.info("[places] hotel={!r} -> place={} rating={} reviews={} took={:.0f}ms",
                hotel_name, result.place_id, result.rating, len(result.reviews), result.elapsed_ms)
    return result


# ----- internal -----
async def _search_first_place(
    sess: aiohttp.ClientSession,
    hotel_name: str,
    *,
    region_hint: str | None,
    language: str | None,
    timeout_s: float,
    api_key: str,
) -> str | None:
    text_query = hotel_name.strip()
    if region_hint:
        text_query = f"{text_query} {region_hint.strip()}"

    body: dict = {
        "textQuery": text_query,
        # The New API supports a "Lodging" type filter that dramatically
        # improves precision when the guest names a hotel.
        "includedType": "lodging",
        "maxResultCount": 1,
    }
    if language:
        body["languageCode"] = language[:2].lower()

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _SEARCH_FIELD_MASK,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with sess.post(PLACES_SEARCH_TEXT_URL, json=body, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                snippet = (await resp.text())[:200]
                raise PlacesError(f"Places searchText HTTP {resp.status}: {snippet}")
            payload = await resp.json()
    except aiohttp.ClientError as exc:
        raise PlacesError(f"Places transport error (searchText): {exc}") from exc

    places = payload.get("places") or []
    if not places:
        return None
    # The New API returns ids as "places/<opaque>"; field is "id" on the inner object.
    pid = (places[0].get("id") or "").strip()
    return f"places/{pid}" if pid else None


async def _fetch_place_details(
    sess: aiohttp.ClientSession,
    place_name: str,  # "places/<id>" resource name
    *,
    language: str | None,
    timeout_s: float,
    api_key: str,
) -> HotelReviewsResult:
    url = PLACES_DETAILS_URL_TEMPLATE.format(place_name=place_name)
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": _DETAILS_FIELD_MASK,
    }
    params: dict = {}
    if language:
        params["languageCode"] = language[:2].lower()

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with sess.get(url, params=params, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                snippet = (await resp.text())[:200]
                raise PlacesError(f"Places details HTTP {resp.status}: {snippet}")
            payload = await resp.json()
    except aiohttp.ClientError as exc:
        raise PlacesError(f"Places transport error (details): {exc}") from exc

    return _parse_details(place_name, payload)


def _parse_details(place_name: str, payload: dict) -> HotelReviewsResult:
    display = (payload.get("displayName") or {}).get("text") or ""
    address = payload.get("formattedAddress") or ""
    rating = payload.get("rating")
    user_count = int(payload.get("userRatingCount") or 0)
    maps_uri = payload.get("googleMapsUri") or ""

    reviews: list[Review] = []
    for r in (payload.get("reviews") or [])[:_MAX_REVIEWS_RETURNED]:
        text_obj = r.get("text") or {}
        author = (r.get("authorAttribution") or {}).get("displayName") or "Anonymous"
        reviews.append(Review(
            author_name=str(author),
            rating=int(r.get("rating") or 0),
            text=str(text_obj.get("text") or "").strip(),
            relative_time=str(r.get("relativePublishTimeDescription") or "").strip(),
            language=str(text_obj.get("languageCode") or "").strip(),
        ))
    return HotelReviewsResult(
        place_id=place_name,
        display_name=display,
        formatted_address=address,
        rating=float(rating) if rating is not None else None,
        user_rating_count=user_count,
        google_maps_uri=maps_uri,
        reviews=reviews,
    )
