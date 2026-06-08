"""Independent scraper for paraflytravel.com hotels.

Produces a ``hotels.json`` file in the exact seed schema consumed by
``voxtera.call_center.server`` (the admin ``/es/load`` and ``/qdrant/load``
endpoints) — i.e. ``data/seed/hotels.json``. Each record carries the
Elasticsearch ``hotels`` index fields *and* a nested ``chunks`` list of
``{category, text, text_en}`` for the Qdrant ``hotel_kb`` collection.

It does **only** scraping: no Elasticsearch/Qdrant connection and no
embedding. Feed the output to the existing ingestion pipeline.

Why a headless browser: the listing pages are an Angular SPA backed by a
token-gated third-party engine (lidyateknoloji.com). The fully-formed hotel
objects live in the page's Angular scope (``hotelResults``); replaying the
backend API is fragile, so we drive Chromium and read the scope directly.

Data flow:
    1. Discover every ``/otel-listesi/<slug>`` category/region list URL from
       the sitemap (or use --lists to override).
    2. For each list: load, click "Daha fazla göster" until all hotels are
       loaded, then read the hotel objects out of the Angular scope.
    3. Deduplicate hotels by site id across all lists; the set of lists a
       hotel appears in feeds its activity_tags / beachfront / adults_only.
    4. (default) Visit each hotel detail page and pull the resolved Turkish
       facility names, room types and food/drink info from the DOM.
    5. Map everything to the seed schema and write hotels.json (+ .jsonl).
       Resumable via a checkpoint file.

Requirements (not in the project's base deps):
    pip install playwright
    playwright install chromium

Usage:
    # Everything, with detail pages (long run — thousands of hotels):
    uv run python scripts/scrape_parafly_hotels.py

    # Just the honeymoon list, first 25 hotels, for a quick validation:
    uv run python scripts/scrape_parafly_hotels.py \
        --lists https://www.paraflytravel.com/otel-listesi/balayi-oteller \
        --max-hotels 25 --out-dir data/scraped

    # Structured data only (no detail-page visits — faster, weaker chunks):
    uv run python scripts/scrape_parafly_hotels.py --no-details
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import aiohttp
from loguru import logger

try:
    from playwright.async_api import Page, async_playwright
    from playwright.async_api import TimeoutError as PWTimeout
except ImportError:  # pragma: no cover - dependency hint
    logger.error(
        "Playwright is required. Install it with:\n"
        "    pip install playwright\n"
        "    playwright install chromium"
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE = "https://www.paraflytravel.com"
SITEMAP_URL = f"{BASE}/sitemap.xml"
LIST_PREFIX = f"{BASE}/otel-listesi/"

# Known hotel-chain brand tokens (kept in sync with call_center.index_config
# BRAND_KEYWORDS). Used to fill the `chain` field from the hotel name.
BRAND_KEYWORDS: list[str] = [
    "rixos",
    "maxx royal",
    "maxx",
    "royal",
    "cornelia",
    "voyage",
    "gloria",
    "xanadu",
    "limak",
    "atlantis",
    "selectum",
    "regnum",
    "carya",
    "akra",
    "hilton",
    "sheraton",
    "marriott",
    "hyatt",
    "radisson",
    "kempinski",
    "fairmont",
    "dedeman",
    "wyndham",
    "crystal",
    "calista",
    "ela",
    "susesi",
    "titanic",
    "kaya",
    "swandor",
    "barut",
    "concorde",
    "delphin",
    "granada",
    "papillon",
    "alva donna",
    "sueno",
    "adam eve",
    "nirvana",
    "ic hotels",
]

# Category/region list slug -> activity tags. Drives activity_tags plus the
# beachfront / adults_only / board_type hints. A hotel inherits the union of
# tags from every list it appears in. Unmapped slugs are treated as region
# slugs (no tags) and ignored for tagging.
SLUG_TAGS: dict[str, list[str]] = {
    "balayi-oteller": ["honeymoon", "romantic"],
    "spa-wellness-oteli": ["spa", "wellness"],
    "aqua-parkli-oteller": ["aquapark", "water_park", "family"],
    "doga-otelleri": ["nature"],
    "doga-oteller": ["nature"],
    "termal-oteller": ["thermal", "spa", "wellness"],
    "kayak-oteli": ["ski", "winter"],
    "kis-oteller": ["winter", "ski"],
    "cocuk-dostu-oteller": ["family", "kids_club"],
    "2-cocuk-ucretsiz-oteller-872": ["family", "kids_club"],
    "butik-oteller": ["boutique"],
    "deluxe-oteller": ["luxury"],
    "sehir-oteli": ["city"],
    "muhafazakar-oteller": ["conservative", "halal", "adults_segregated"],
    "kibris": ["beach"],
    "erken-rezervasyon": [],
    "2026-erken-rezervasyon-oteller": [],
    "istanbula-yakin-oteller": ["city"],
}

# Slugs that imply the hotel is on / near the sea -> beachfront hint.
BEACH_SLUGS = {
    "kibris",
    "bodrum",
    "marmaris",
    "fethiye",
    "belek",
    "alanya",
    "kas",
    "didim",
    "cesme",
    "kusadasi",
    "ayvalik",
    "alacatii",
    "antalya",
    "assos-126716",
}

# Conservative / adults-segregated theme.
ADULTS_SEGREGATED_SLUGS = {"muhafazakar-oteller"}

# Turkish meal_type -> board_type slug (best effort; meal_type is often blank
# on the listing because no dates are selected).
BOARD_MAP: dict[str, str] = {
    "ultra her şey dahil": "ultra_all_inclusive",
    "ultra herşey dahil": "ultra_all_inclusive",
    "her şey dahil": "all_inclusive",
    "herşey dahil": "all_inclusive",
    "yarım pansiyon": "half_board",
    "tam pansiyon": "full_board",
    "oda kahvaltı": "bed_and_breakfast",
    "sadece oda": "room_only",
}

# Coastal provinces -> "Turkish Riviera" coarse bucket (matches the
# call_center REGION_ALIASES philosophy). Everything else keeps its
# mid-destination token (e.g. "Kapadokya") or the province name.
RIVIERA_PROVINCES = {
    "antalya",
    "muğla",
    "mugla",
    "aydın",
    "aydin",
    "izmir",
    "i̇zmir",
    "mersin",
}

# Detail-page Turkish section heading -> chunk category. Matched as a
# case-insensitive substring against rendered headings.
SECTION_CATEGORY: list[tuple[str, str]] = [
    ("otel hakkında", "overview"),
    ("genel bilgi", "overview"),
    ("hakkında", "overview"),
    ("oda", "rooms"),
    ("yeme", "food_beverage"),
    ("içme", "food_beverage"),
    ("restoran", "food_beverage"),
    ("bar", "food_beverage"),
    ("spa", "wellness"),
    ("wellness", "wellness"),
    ("sağlık", "wellness"),
    ("çocuk", "children"),
    ("aktivite", "activities"),
    ("eğlence", "activities"),
    ("spor", "activities"),
    ("plaj", "amenities"),
    ("havuz", "amenities"),
    ("özellik", "amenities"),
    ("tesis", "amenities"),
    ("hizmet", "amenities"),
    ("konum", "location"),
    ("ulaşım", "location"),
    ("çevre", "location"),
]

VALID_CATEGORIES = {
    "overview",
    "rooms",
    "amenities",
    "food_beverage",
    "wellness",
    "policies",
    "children",
    "activities",
    "accessibility",
    "location",
    "atmosphere",
    "packages",
}

# Strong UI / booking-widget / login / campaign markers. A detail-page section
# body containing any of these is interface chrome (e.g. the room-search form
# rendered under the "Odalar" heading), not hotel prose — drop it.
NOISE_MARKERS = [
    "oda ekle",
    "güncelle",
    "misafirler",
    "giriş çıkış",
    "+ oda",
    "yetişkin 1 çocuk",
    "üye girişi",
    "facebook ile",
    "google ile",
    "şifremi unuttum",
    "indirim kodu",
    "i̇ndirim kodu",
    "kodu kopyala",
    "talep et",
    "müşteri hizmetleri",
    "filtrele",
    "sırala",
    "daha fazla göster",
    "rezervasyon için",
]


def _is_noise(text: str) -> bool:
    """True if a detail-section body is UI chrome rather than hotel content."""
    low = text.lower()
    return any(m in low for m in NOISE_MARKERS)


# ---------------------------------------------------------------------------
# JavaScript run inside the page (reads the Angular scope)
# ---------------------------------------------------------------------------

# Returns {totalCount, hotels:[...projected fields...]} from the listing scope.
JS_READ_LISTING = r"""
() => {
  const want = ['id','name','slug','address','destination','destination_slug',
    'old_destination_slug','country_code','location','phone','email','meal_type',
    'checkin_from','checkout_to','nr_rooms','nr_restaurants','nr_bars','stars',
    'year_built','max_free_child_age','min_free_child_age','themes','facilities',
    'hotelDetailBaseUrl','web_site','zip_code'];
  let out = {totalCount: null, hotels: []};
  const seen = new Set();
  const els = document.querySelectorAll('.ng-scope,[ng-controller]');
  for (const el of els) {
    let s;
    try { s = window.angular.element(el).scope(); } catch(e) { continue; }
    while (s) {
      if (typeof s.totalCount !== 'undefined' && s.totalCount) out.totalCount = s.totalCount;
      if (s.hotelResults && s.hotelResults.length) {
        for (const h of s.hotelResults) {
          if (!h || h.id == null || seen.has(String(h.id))) continue;
          seen.add(String(h.id));
          const o = {};
          for (const k of want) o[k] = (h[k] === undefined ? null : h[k]);
          out.hotels.push(o);
        }
      }
      s = s.$parent;
    }
  }
  return out;
}
"""

# Returns the rendered hotel-detail content: section (heading,text) pairs plus
# any facility/amenity chips, scraped from the DOM (names already resolved by
# the app). Best effort across template variants.
JS_READ_DETAIL = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const sections = [];
  const heads = document.querySelectorAll('h1,h2,h3,h4,.section-title,.tab-title,.accordion-title');
  for (const h of heads) {
    const title = clean(h.textContent);
    if (!title || title.length > 80) continue;
    // gather text from following siblings until the next heading
    let parts = [];
    let n = h.nextElementSibling;
    let hops = 0;
    while (n && hops < 6) {
      if (/^H[1-4]$/.test(n.tagName)) break;
      const t = clean(n.textContent);
      if (t && t.length > 30) parts.push(t);
      n = n.nextElementSibling; hops++;
    }
    // also look inside the heading's parent container
    if (!parts.length && h.parentElement) {
      const t = clean(h.parentElement.textContent).replace(title, '');
      if (t.length > 40) parts.push(t);
    }
    const body = parts.join(' ').slice(0, 1500);
    if (body) sections.push({title, body});
  }
  // facility / amenity chips
  const chips = [];
  const sel = '.facility,.amenity,.feature,.facilities li,.amenities li,'
    + '[class*="facility"] li,[class*="amenity"] li,.hotel-features li,.property-features li';
  document.querySelectorAll(sel).forEach(e => {
    const t = clean(e.textContent);
    if (t && t.length < 60) chips.push(t);
  });
  return {sections, chips: [...new Set(chips)].slice(0, 120)};
}
"""

# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------


async def discover_list_urls(session: aiohttp.ClientSession) -> list[str]:
    """Return every /otel-listesi/<slug> URL from the sitemap, deduplicated."""
    logger.info("Fetching sitemap {}", SITEMAP_URL)
    async with session.get(SITEMAP_URL, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        xml = await r.text()

    urls: list[str] = []
    seen: set[str] = set()
    try:
        root = ET.fromstring(xml)
        for loc in root.iter():
            if loc.tag.endswith("loc") and loc.text and loc.text.startswith(LIST_PREFIX):
                u = loc.text.strip()
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
    except ET.ParseError:
        # Fall back to a regex sweep if the XML is malformed.
        for m in re.finditer(r"https://www\.paraflytravel\.com/otel-listesi/[^\s<]+", xml):
            u = m.group(0)
            if u not in seen:
                seen.add(u)
                urls.append(u)

    logger.info("Discovered {} category/region list URLs", len(urls))
    return urls


def slug_of(list_url: str) -> str:
    """Return the trailing slug of an /otel-listesi/<slug> URL."""
    return list_url.rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Listing scrape (one category list -> hotel objects)
# ---------------------------------------------------------------------------


async def scrape_list(
    page: Page, list_url: str, *, max_pages: int, max_hotels: int | None, delay: float
) -> list[dict[str, Any]]:
    """Load a listing URL, paginate fully, return raw hotel objects."""
    logger.info("List: {}", list_url)
    try:
        await page.goto(list_url, wait_until="domcontentloaded", timeout=60_000)
    except PWTimeout:
        logger.warning("Timeout loading {} — skipping", list_url)
        return []

    # Wait for the Angular scope to populate hotelResults.
    loaded = 0
    for _ in range(40):
        await asyncio.sleep(0.5)
        try:
            data = await page.evaluate(JS_READ_LISTING)
        except Exception:  # noqa: BLE001
            data = {"hotels": []}
        loaded = len(data.get("hotels", []))
        if loaded:
            break
    if not loaded:
        logger.warning("No hotels appeared for {} — skipping", list_url)
        return []

    total = data.get("totalCount") or 0
    logger.info("  {} loaded, totalCount={}", loaded, total)

    # Click "Daha fazla göster" until the count stops growing.
    stagnant = 0
    for page_i in range(max_pages):
        if max_hotels and loaded >= max_hotels:
            break
        if total and loaded >= total:
            break
        clicked = await _click_show_more(page)
        if not clicked:
            break
        await asyncio.sleep(delay)
        try:
            data = await page.evaluate(JS_READ_LISTING)
        except Exception:  # noqa: BLE001
            break
        new_loaded = len(data.get("hotels", []))
        if new_loaded <= loaded:
            stagnant += 1
            if stagnant >= 3:
                break
        else:
            stagnant = 0
        loaded = new_loaded
        if page_i % 10 == 0:
            logger.info("  …{} loaded", loaded)

    hotels = data.get("hotels", [])
    if max_hotels:
        hotels = hotels[:max_hotels]
    logger.info("  -> {} hotels from this list", len(hotels))
    return hotels


async def _click_show_more(page: Page) -> bool:
    """Click the 'Daha fazla göster' button. Return False if absent/disabled."""
    try:
        btn = page.locator(
            "xpath=//button[contains(., 'Daha fazla')]|//a[contains(., 'Daha fazla')]"
        ).first
        if await btn.count() == 0:
            return False
        if not await btn.is_visible():
            return False
        await btn.scroll_into_view_if_needed(timeout=5_000)
        await btn.click(timeout=5_000)
        return True
    except Exception:  # noqa: BLE001 — button vanished / not clickable == end of list
        return False


# ---------------------------------------------------------------------------
# Detail scrape (one hotel -> resolved Turkish text)
# ---------------------------------------------------------------------------


async def scrape_detail(page: Page, detail_url: str, delay: float) -> dict[str, Any]:
    """Load a hotel detail page, return {sections:[...], chips:[...]}.

    The hotel-detail body is fetched asynchronously after the document loads,
    so we poll until mapped content (food/amenities/rooms/etc.) appears rather
    than relying on a fixed sleep, which races the render.
    """
    try:
        await page.goto(detail_url, wait_until="domcontentloaded", timeout=45_000)
    except PWTimeout:
        logger.warning("  detail timeout: {}", detail_url)
        return {"sections": [], "chips": []}

    # Note: we deliberately do NOT wait for "networkidle" — the SPA keeps
    # socket.io / push connections open, so it never goes idle. The detail body
    # renders progressively, so we poll a fixed number of times and keep the
    # richest snapshot (most mappable content) rather than racing a single read.
    best: dict[str, Any] = {"sections": [], "chips": []}
    best_score = -1
    stable = 0
    for _ in range(int(args_poll_count())):
        await asyncio.sleep(max(delay, 0.6))
        try:
            cur = await page.evaluate(JS_READ_DETAIL)
        except Exception as e:  # noqa: BLE001
            logger.warning("  detail parse error {}: {}", detail_url, e)
            continue
        score = _detail_score(cur)
        if score > best_score:
            best, best_score, stable = cur, score, 0
        else:
            stable += 1
            # content has settled and we already have something mappable
            if stable >= 2 and _has_mappable_section(best):
                break
    return best


# Number of detail-page polls; module-level so it can be tuned via env if a
# user wants to trade speed for capture completeness on slow connections.
_DETAIL_POLLS = 10


def args_poll_count() -> int:
    return _DETAIL_POLLS


def _detail_score(d: dict[str, Any]) -> int:
    """Rank a detail snapshot by how much *mappable*, non-noise content it has."""
    score = 0
    for sec in d.get("sections", []):
        title = (sec.get("title") or "").lower()
        body = (sec.get("body") or "").strip()
        if not body or _is_noise(body):
            continue
        if any(kw in title for kw, _ in SECTION_CATEGORY):
            score += 2 + min(len(body) // 200, 5)
    score += len([c for c in d.get("chips", []) if not _is_noise(c)])
    return score


def _has_mappable_section(d: dict[str, Any]) -> bool:
    return _detail_score(d) > 0


# ---------------------------------------------------------------------------
# Mapping raw -> seed schema
# ---------------------------------------------------------------------------


def _first_int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def price_tier_from_stars(stars: int) -> str:
    """Heuristic price tier (no live price available without a dated search)."""
    if stars >= 6:
        return "luxury"  # boutique / special class
    if stars == 5:
        return "luxury"
    if stars == 4:
        return "premium"
    if stars == 3:
        return "mid"
    if stars >= 1:
        return "budget"
    return "standard"


def split_destination(destination: str | None) -> tuple[str, str, str]:
    """Return (city, district, region_token) from a 'A, B, C' destination."""
    parts = [p.strip() for p in (destination or "").split(",") if p.strip()]
    if not parts:
        return "", "", ""
    city = parts[0]
    district = parts[-1] if len(parts) > 1 else ""
    region_token = parts[1] if len(parts) > 2 else (parts[-1] if len(parts) > 1 else parts[0])
    return city, district, region_token


def region_bucket(city: str, region_token: str) -> str:
    """Coarse region label matching the seed convention."""
    if city.strip().lower() in RIVIERA_PROVINCES:
        return "Turkish Riviera"
    return region_token or city


def detect_chain(name: str) -> str:
    low = name.lower()
    for brand in BRAND_KEYWORDS:
        if brand in low:
            return brand.title()
    return ""


def board_from_meal(meal_type: str | None) -> str:
    if not meal_type:
        return ""
    low = meal_type.strip().lower()
    for tr, slug in BOARD_MAP.items():
        if tr in low:
            return slug
    return ""


def build_chunks(raw: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, str]]:
    """Build Turkish KB chunks (text_en left empty per scrape-only mode)."""
    chunks: list[dict[str, str]] = []
    name = (raw.get("name") or "").strip()
    destination = (raw.get("destination") or "").strip()
    address = (raw.get("address") or "").strip()
    stars = _first_int(raw.get("stars"))
    nr_rooms = _first_int(raw.get("nr_rooms"))
    nr_rest = _first_int(raw.get("nr_restaurants"))
    nr_bars = _first_int(raw.get("nr_bars"))
    checkin = (raw.get("checkin_from") or "").strip()
    checkout = (raw.get("checkout_to") or "").strip()
    year_built = raw.get("year_built")

    # --- overview (always, from structured data) ---
    ov = [f"{name}, {destination} bölgesinde yer alan"]
    ov[-1] += f" {stars} yıldızlı bir oteldir." if stars else " bir oteldir."
    if year_built:
        ov.append(f"Tesis {year_built} yılında yapılmıştır.")
    if nr_rooms:
        ov.append(f"Otelde toplam {nr_rooms} oda bulunmaktadır.")
    if checkin or checkout:
        ov.append(
            f"Giriş saati {checkin or '-'}, çıkış saati {checkout or '-'} olarak uygulanmaktadır."
        )
    chunks.append({"category": "overview", "text": " ".join(ov).strip(), "text_en": ""})

    # --- location (structured) ---
    if address or destination:
        loc = []
        if address:
            loc.append(f"{name} adresi: {address}.")
        if destination:
            loc.append(f"Otel {destination} konumundadır.")
        chunks.append({"category": "location", "text": " ".join(loc).strip(), "text_en": ""})

    # --- food_beverage (structured counts) ---
    if nr_rest or nr_bars:
        fb = []
        if nr_rest:
            fb.append(f"Otel bünyesinde {nr_rest} restoran")
        if nr_bars:
            fb.append(("ve " if fb else "") + f"{nr_bars} bar")
        chunks.append(
            {
                "category": "food_beverage",
                "text": (" ".join(fb) + " bulunmaktadır.").strip(),
                "text_en": "",
            }
        )

    # --- amenities (from detail facility chips) ---
    chips = [c for c in (detail.get("chips") or []) if not _is_noise(c)]
    if chips:
        chunks.append(
            {
                "category": "amenities",
                "text": f"{name} otelinde sunulan olanaklar: " + ", ".join(chips[:80]) + ".",
                "text_en": "",
            }
        )

    # --- detail sections mapped to categories ---
    by_cat: dict[str, list[str]] = {}
    for sec in detail.get("sections") or []:
        title = (sec.get("title") or "").lower()
        body = (sec.get("body") or "").strip()
        if not body or _is_noise(body):
            continue
        cat = next((c for kw, c in SECTION_CATEGORY if kw in title), None)
        if not cat:
            continue
        by_cat.setdefault(cat, []).append(body)
    existing = {c["category"] for c in chunks}
    for cat, bodies in by_cat.items():
        text = " ".join(bodies).strip()[:2000]
        if not text:
            continue
        if cat in existing:
            # append detail prose to the structured chunk
            for c in chunks:
                if c["category"] == cat:
                    c["text"] = (c["text"] + " " + text).strip()[:2200]
                    break
        else:
            chunks.append({"category": cat, "text": text, "text_en": ""})

    # keep only valid categories with non-trivial text
    return [c for c in chunks if c["category"] in VALID_CATEGORIES and len(c["text"]) > 20]


def to_seed_record(
    raw: dict[str, Any], list_slugs: set[str], detail: dict[str, Any]
) -> dict[str, Any]:
    """Map a raw scraped hotel + its list memberships to the seed schema."""
    name = (raw.get("name") or "").strip()
    hotel_id = f"parafly_{raw.get('id')}"
    stars = _first_int(raw.get("stars"))
    city, district, region_token = split_destination(raw.get("destination"))

    # activity tags from the lists the hotel appears in
    tags: set[str] = set()
    for sl in list_slugs:
        tags.update(SLUG_TAGS.get(sl, []))
    beachfront = bool(list_slugs & BEACH_SLUGS)
    adults_only = bool(list_slugs & ADULTS_SEGREGATED_SLUGS)

    loc = raw.get("location") or []
    coordinates = [loc[0], loc[1]] if isinstance(loc, list) and len(loc) >= 2 else [0, 0]

    country = (
        "Turkey"
        if (raw.get("country_code") or "tr").lower() == "tr"
        else (raw.get("country_code") or "").upper()
    )

    record = {
        "hotel_id": hotel_id,
        "name": name,
        "aliases": _aliases(name),
        "chain": detect_chain(name),
        "city": city,
        "district": district,
        "region": region_bucket(city, region_token),
        "country": country,
        "price_tier": price_tier_from_stars(stars),
        "coordinates": coordinates,
        "activity_tags": sorted(tags),
        "beachfront": beachfront,
        "adults_only": adults_only,
        "kids_age_min": _first_int(raw.get("min_free_child_age")),
        "kids_age_max": _first_int(raw.get("max_free_child_age")),
        "board_type": board_from_meal(raw.get("meal_type")),
        "star_rating": stars,
        # provenance — handy for debugging / re-scrapes, ignored by the ingester
        "source_url": raw.get("hotelDetailBaseUrl") or "",
        "source_lists": sorted(list_slugs),
        "chunks": build_chunks(raw, detail),
    }
    return record


def _aliases(name: str) -> list[str]:
    """A couple of light name variants to help the resolver."""
    al: set[str] = set()
    stripped = re.sub(r"\b(otel|hotel|resort|spa|suites?|hotels?)\b", "", name, flags=re.I).strip()
    if stripped and stripped.lower() != name.lower():
        al.add(re.sub(r"\s+", " ", stripped))
    return sorted(al)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

MAX_BROWSER_RETRIES = 5  # re-launch browser up to N times on crash


async def _launch_browser(pw, *, headful: bool):
    """Launch a fresh browser context and page."""
    browser = await pw.chromium.launch(headless=not headful)
    ctx = await browser.new_context(
        locale="tr-TR",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    )
    page = await ctx.new_page()
    return browser, ctx, page


def _save_phase1(out_dir: Path, raw_by_id: dict, lists_by_id: dict) -> None:
    """Persist Phase 1 listing data so it survives crashes."""
    phase1_path = out_dir / ".phase1_listings.json"
    # Convert sets to lists for JSON serialization
    serializable_lists = {k: sorted(v) for k, v in lists_by_id.items()}
    phase1_path.write_text(
        json.dumps({"raw_by_id": raw_by_id, "lists_by_id": serializable_lists}, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Phase 1 saved: {} hotels -> {}", len(raw_by_id), phase1_path.name)


def _load_phase1(out_dir: Path) -> tuple[dict[str, dict], dict[str, set]] | None:
    """Load Phase 1 listing data from a previous run, if available."""
    phase1_path = out_dir / ".phase1_listings.json"
    if not phase1_path.exists():
        return None
    try:
        data = json.loads(phase1_path.read_text(encoding="utf-8"))
        raw_by_id = data["raw_by_id"]
        lists_by_id = {k: set(v) for k, v in data["lists_by_id"].items()}
        logger.info("Loaded Phase 1 cache: {} hotels from {}", len(raw_by_id), phase1_path.name)
        return raw_by_id, lists_by_id
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Could not parse {} — will re-scrape listings: {}", phase1_path.name, e)
        return None


async def run(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "hotels.json"
    jsonl_path = out_dir / "hotels.jsonl"
    ckpt_path = out_dir / ".scrape_checkpoint.json"

    # Resume state
    done_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    if args.resume and json_path.exists():
        try:
            records = json.loads(json_path.read_text(encoding="utf-8"))
            done_ids = {r["hotel_id"] for r in records}
            logger.info("Resuming: {} hotels already in {}", len(done_ids), json_path.name)
        except json.JSONDecodeError:
            logger.warning("Could not parse existing {} — starting fresh", json_path.name)

    # --- Phase 1: listings (try to load from cache first) ---
    raw_by_id: dict[str, dict[str, Any]] = {}
    lists_by_id: dict[str, set[str]] = {}

    phase1_cached = _load_phase1(out_dir) if args.resume else None
    if phase1_cached:
        raw_by_id, lists_by_id = phase1_cached
        # Apply max_hotels cap
        if args.max_hotels and len(raw_by_id) > args.max_hotels:
            keep = list(raw_by_id)[: args.max_hotels]
            raw_by_id = {k: raw_by_id[k] for k in keep}
    else:
        async with aiohttp.ClientSession() as http:
            list_urls = args.lists or await discover_list_urls(http)

        async with async_playwright() as pw:
            browser, ctx, page = await _launch_browser(pw, headful=args.headful)
            try:
                for list_url in list_urls:
                    sl = slug_of(list_url)
                    hotels = await scrape_list(
                        page,
                        list_url,
                        max_pages=args.max_pages,
                        max_hotels=args.max_hotels,
                        delay=args.delay,
                    )
                    for h in hotels:
                        hid = str(h.get("id"))
                        raw_by_id.setdefault(hid, h)
                        lists_by_id.setdefault(hid, set()).add(sl)
                    # Save Phase 1 progress after each list
                    _save_phase1(out_dir, raw_by_id, lists_by_id)
                    if args.max_hotels and len(raw_by_id) >= args.max_hotels:
                        logger.info("Reached --max-hotels={} unique hotels", args.max_hotels)
                        break
            finally:
                await browser.close()

        if args.max_hotels:
            keep = list(raw_by_id)[: args.max_hotels]
            raw_by_id = {k: raw_by_id[k] for k in keep}
        # Final Phase 1 save
        _save_phase1(out_dir, raw_by_id, lists_by_id)

    logger.info("Total unique hotels to process: {}", len(raw_by_id))

    # --- Phase 2: details + mapping (with browser crash recovery) ---
    async with async_playwright() as pw:
        browser, ctx, page = await _launch_browser(pw, headful=args.headful)
        browser_retries = 0
        processed = 0

        hotel_items = list(raw_by_id.items())
        idx = 0
        while idx < len(hotel_items):
            hid, raw = hotel_items[idx]
            rec_id = f"parafly_{hid}"
            if rec_id in done_ids:
                idx += 1
                continue

            detail: dict[str, Any] = {"sections": [], "chips": []}
            if args.with_details:
                url = raw.get("hotelDetailBaseUrl") or _build_detail_url(raw)
                if url:
                    try:
                        detail = await scrape_detail(page, url, args.delay)
                    except Exception as e:
                        # Browser crashed — try to recover
                        logger.warning("Browser error on {}: {} — attempting recovery", url, e)
                        with contextlib.suppress(Exception):
                            await browser.close()
                        browser_retries += 1
                        if browser_retries > MAX_BROWSER_RETRIES:
                            logger.error(
                                "Max browser retries ({}) exceeded — saving and exiting",
                                MAX_BROWSER_RETRIES,
                            )
                            break
                        logger.info(
                            "Re-launching browser (retry {}/{})",
                            browser_retries,
                            MAX_BROWSER_RETRIES,
                        )
                        await asyncio.sleep(2 * browser_retries)  # backoff
                        browser, ctx, page = await _launch_browser(pw, headful=args.headful)
                        continue  # retry the same hotel

            record = to_seed_record(raw, lists_by_id.get(hid, set()), detail)
            records.append(record)
            done_ids.add(rec_id)
            processed += 1
            idx += 1

            # checkpoint every N
            if processed % args.checkpoint_every == 0:
                _write_outputs(json_path, jsonl_path, records)
                ckpt_path.write_text(json.dumps(sorted(done_ids)), encoding="utf-8")
                logger.info("Checkpoint: {} hotels written ({} total)", processed, len(records))

        with contextlib.suppress(Exception):
            await browser.close()

    _write_outputs(json_path, jsonl_path, records)
    ckpt_path.write_text(json.dumps(sorted(done_ids)), encoding="utf-8")
    n_chunks = sum(len(r["chunks"]) for r in records)
    logger.success(
        "Done. {} hotels, {} chunks -> {} (and {})",
        len(records),
        n_chunks,
        json_path,
        jsonl_path.name,
    )


def _build_detail_url(raw: dict[str, Any]) -> str:
    slug = raw.get("slug")
    dslug = raw.get("destination_slug") or raw.get("old_destination_slug")
    if slug and dslug:
        return f"{BASE}/{dslug}-otelleri/{slug}"
    if slug:
        return f"{BASE}/otel/{slug}"
    return ""


def _write_outputs(json_path: Path, jsonl_path: Path, records: list[dict[str, Any]]) -> None:
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape paraflytravel.com hotels into the seed schema.")
    p.add_argument(
        "--lists",
        nargs="*",
        default=None,
        help="One or more /otel-listesi/<slug> URLs. Default: all from the sitemap.",
    )
    p.add_argument("--out-dir", default="data/scraped", help="Output directory.")
    p.add_argument(
        "--max-hotels",
        type=int,
        default=None,
        help="Cap total unique hotels (for quick test runs).",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=200,
        help="Max 'show more' clicks per list (safety bound).",
    )
    details = p.add_mutually_exclusive_group()
    details.add_argument(
        "--with-details",
        dest="with_details",
        action="store_true",
        default=True,
        help="Visit each hotel detail page for richer chunks (default).",
    )
    details.add_argument(
        "--no-details",
        dest="with_details",
        action="store_false",
        help="Skip detail pages — structured chunks only (faster).",
    )
    p.add_argument("--delay", type=float, default=0.7, help="Politeness delay (seconds).")
    p.add_argument("--headful", action="store_true", help="Show the browser window.")
    p.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        default=True,
        help="Ignore any existing output and start fresh.",
    )
    p.add_argument("--checkpoint-every", type=int, default=25, help="Flush output every N hotels.")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
