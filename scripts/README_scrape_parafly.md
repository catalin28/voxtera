# Parafly hotel scraper

`scrape_parafly_hotels.py` — an independent scraper that pulls hotels from
paraflytravel.com and writes them in the **exact seed schema** consumed by
`voxtera.call_center.server` (`data/seed/hotels.json`). It only scrapes: no
Elasticsearch/Qdrant connection and no embedding — feed its output to the
existing ingestion pipeline.

## Install

```bash
pip install playwright
playwright install chromium
```

## Run

```bash
# Everything: every /otel-listesi/<slug> list in the sitemap, with detail pages.
# Long run (the site has a few thousand hotels). Resumable.
uv run python scripts/scrape_parafly_hotels.py

# Quick validation: honeymoon list, first 25 hotels.
uv run python scripts/scrape_parafly_hotels.py \
  --lists https://www.paraflytravel.com/otel-listesi/balayi-oteller \
  --max-hotels 25 --out-dir data/scraped

# Fast structured-only pass (no detail-page visits → overview+location chunks).
uv run python scripts/scrape_parafly_hotels.py --no-details
```

Output (default `data/scraped/`):
- `hotels.json`  — array of seed records (point `SEED_FILE` at it, or copy to `data/seed/hotels.json`)
- `hotels.jsonl` — same records, one per line
- `.scrape_checkpoint.json` — resume state (`--no-resume` to ignore)

## How it works

The listing pages are an Angular SPA backed by a token-gated third-party
engine (lidyateknoloji.com). The fully-formed hotel objects live in the page's
Angular scope (`hotelResults`), so the scraper drives headless Chromium and
reads the scope directly instead of replaying the fragile backend API. List
URLs are discovered from `sitemap.xml`. Hotels are deduplicated by site id; the
set of lists a hotel appears in drives its `activity_tags` / `beachfront` /
`adults_only`.

## Field mapping (source → seed)

| Seed field      | Source                                                            |
|-----------------|------------------------------------------------------------------|
| `hotel_id`      | `parafly_<site id>`                                              |
| `name`          | `name`                                                           |
| `aliases`       | light name variants (name minus Otel/Resort/Suites…)            |
| `chain`         | brand token detected in name (Rixos, Hilton, …) else ""        |
| `city`/`district`/`region` | parsed from `destination` ("Province, Region, District"); coastal provinces bucket to "Turkish Riviera" |
| `country`       | `country_code` (`tr` → "Turkey")                                |
| `price_tier`    | heuristic from star rating (5–6★ luxury, 4 premium, 3 mid, …)  |
| `coordinates`   | `location` `[lat, lng]`                                          |
| `activity_tags` | union of tags from the lists the hotel appears in               |
| `beachfront`    | true if the hotel appears in a coastal list                     |
| `adults_only`   | true for conservative/adults-segregated lists                   |
| `kids_age_min/max` | `min/max_free_child_age`                                      |
| `board_type`    | `meal_type` (often blank on the listing) → slug                 |
| `star_rating`   | `stars`                                                         |
| `chunks[]`      | Turkish text per category; `text_en` left empty (translate later)|

Chunk categories use the 12-value enum from `call_center.kb_config`. `overview`
and `location` come from structured listing data (always present);
`food_beverage`, `amenities`, `activities`, etc. come from the detail page when
available.

## Known limitations / future work

- `text_en` is intentionally empty — run a separate translation pass before or
  during ingestion if English chunks are needed.
- `price_tier` and `board_type` are heuristic (no live price/board without a
  dated availability search).
- Per-room-type descriptions live behind a tab/accordion on the detail page and
  are not yet extracted; `overview` still records the room count. Foreign
  hotels often have thin Turkish detail content, so they may yield only
  `overview` + `location`.
- Tune `--delay` up on slow connections; `_DETAIL_POLLS` controls how long each
  detail page is polled for progressively-rendered content.
