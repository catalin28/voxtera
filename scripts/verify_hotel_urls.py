"""Verify hotel URLs: check reachability, SSL, and name match.

Fetches each hotel_url, checks HTTP status, SSL validity, and whether the
page title/content relates to the hotel name. Outputs a report and an
updated hotels JSON with a `url_status` field.

Usage:
    uv run python scripts/verify_hotel_urls.py
    uv run python scripts/verify_hotel_urls.py --input data/scraped/hotels_en.json
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONCURRENT = 20
TIMEOUT = 15  # seconds per request
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Words that indicate a parked/deceptive/expired domain
PARKED_MARKERS = [
    "domain for sale",
    "buy this domain",
    "parked domain",
    "this domain",
    "domain expired",
    "suspended",
    "account suspended",
    "coming soon",
    "under construction",
    "godaddy",
    "namecheap",
    "sedo",
    "afternic",
    "hugedomains",
    "dan.com",
    "register.com",
    "parking",
    "域名",
    "satılık",
    "satılıktır",
    "bu alan adı",
]

# Status categories
STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
STATUS_PARKED = "parked"
STATUS_SSL_ERROR = "ssl_error"
STATUS_TIMEOUT = "timeout"
STATUS_UNREACHABLE = "unreachable"
STATUS_REDIRECT_SUSPICIOUS = "redirect_suspicious"
STATUS_EMPTY = "no_url"
STATUS_INVALID = "invalid_url"


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def _is_valid_url(url: str) -> bool:
    """Basic URL format check."""
    if not url or not url.strip():
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc or "." not in parsed.netloc:
        return False
    # Catch malformed URLs like "https://letoonia@letoonia.com"
    return "@" not in parsed.netloc


def _normalize_name(name: str) -> str:
    """Normalize hotel name for fuzzy comparison."""
    # Remove common suffixes
    name = re.sub(
        r"\b(hotel|otel|resort|spa|suites?|residence|beach|club|casino|"
        r"convention|collection|village|chalet|center|plaza|palace)\b",
        "",
        name,
        flags=re.I,
    )
    return re.sub(r"[^a-z0-9]", "", name.lower()).strip()


def _title_matches_hotel(title: str, hotel_name: str) -> bool:
    """Check if page title is related to the hotel."""
    if not title:
        return False

    title_lower = title.lower()
    name_lower = hotel_name.lower()

    # Direct substring match
    # Check if significant parts of the hotel name appear in the title
    name_words = [w for w in re.split(r"\W+", name_lower) if len(w) > 3]
    if not name_words:
        return True  # Can't check, assume ok

    matches = sum(1 for w in name_words if w in title_lower)
    if matches >= len(name_words) * 0.4:  # At least 40% of name words match
        return True

    # Fuzzy match on normalized versions
    norm_title = _normalize_name(title)
    norm_name = _normalize_name(hotel_name)
    if not norm_name:
        return True
    ratio = SequenceMatcher(None, norm_title[:60], norm_name).ratio()
    return ratio > 0.35


def _is_parked(html: str, title: str) -> bool:
    """Detect parked/expired/deceptive domains."""
    combined = (title + " " + html[:3000]).lower()
    return any(marker in combined for marker in PARKED_MARKERS)


# ---------------------------------------------------------------------------
# Async verification
# ---------------------------------------------------------------------------


async def verify_url(
    session: aiohttp.ClientSession,
    hotel: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """Verify a single hotel URL. Returns status dict."""
    url = hotel.get("hotel_url", "").strip()
    name = hotel.get("name", "")
    hotel_id = hotel.get("hotel_id", "")

    if not url:
        return {"hotel_id": hotel_id, "name": name, "url": "", "status": STATUS_EMPTY}

    if not _is_valid_url(url):
        return {
            "hotel_id": hotel_id,
            "name": name,
            "url": url,
            "status": STATUS_INVALID,
            "reason": "Malformed URL",
        }

    async with semaphore:
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                allow_redirects=True,
                ssl=False,  # Don't fail on SSL issues, we'll check separately
            ) as resp:
                status_code = resp.status
                final_url = str(resp.url)

                # Check for suspicious redirects (different domain entirely)
                orig_domain = urlparse(url).netloc.lower().replace("www.", "")
                final_domain = urlparse(final_url).netloc.lower().replace("www.", "")

                # Read limited body for title extraction
                try:
                    body = await resp.text(encoding="utf-8", errors="replace")
                    body = body[:5000]
                except Exception:
                    body = ""

                # Extract title
                title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
                title = title_match.group(1).strip()[:200] if title_match else ""

                # Check for parked domain
                if _is_parked(body, title):
                    return {
                        "hotel_id": hotel_id,
                        "name": name,
                        "url": url,
                        "status": STATUS_PARKED,
                        "title": title,
                        "status_code": status_code,
                    }

                # Check redirect to unrelated domain
                if orig_domain != final_domain:
                    # Check if final domain is still related
                    if not _title_matches_hotel(title, name):
                        return {
                            "hotel_id": hotel_id,
                            "name": name,
                            "url": url,
                            "status": STATUS_REDIRECT_SUSPICIOUS,
                            "final_url": final_url,
                            "title": title,
                            "status_code": status_code,
                        }

                # Check title match
                if status_code == 200 and title and not _title_matches_hotel(title, name):
                    return {
                        "hotel_id": hotel_id,
                        "name": name,
                        "url": url,
                        "status": STATUS_MISMATCH,
                        "title": title,
                        "status_code": status_code,
                    }

                # Check HTTP errors
                if status_code >= 400:
                    return {
                        "hotel_id": hotel_id,
                        "name": name,
                        "url": url,
                        "status": STATUS_UNREACHABLE,
                        "reason": f"HTTP {status_code}",
                        "status_code": status_code,
                    }

                return {
                    "hotel_id": hotel_id,
                    "name": name,
                    "url": url,
                    "status": STATUS_OK,
                    "title": title,
                    "status_code": status_code,
                }

        except aiohttp.ClientSSLError as e:
            return {
                "hotel_id": hotel_id,
                "name": name,
                "url": url,
                "status": STATUS_SSL_ERROR,
                "reason": str(e)[:100],
            }
        except TimeoutError:
            return {"hotel_id": hotel_id, "name": name, "url": url, "status": STATUS_TIMEOUT}
        except (aiohttp.ClientError, OSError) as e:
            return {
                "hotel_id": hotel_id,
                "name": name,
                "url": url,
                "status": STATUS_UNREACHABLE,
                "reason": str(e)[:100],
            }


# ---------------------------------------------------------------------------
# SSL-specific check (separate pass for urls that passed HTTP but may have bad certs)
# ---------------------------------------------------------------------------


async def check_ssl(url: str) -> bool:
    """Return True if SSL is valid, False otherwise."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return True  # Not HTTPS, skip
    try:
        ctx = ssl.create_default_context()
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, 443, ssl=ctx),
            timeout=10,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(input_path: Path, output_dir: Path) -> None:
    hotels: list[dict[str, Any]] = json.loads(input_path.read_text(encoding="utf-8"))
    logger.info("Verifying URLs for {} hotels", len(hotels))

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT, ssl=False)
    async with aiohttp.ClientSession(
        connector=connector,
        headers={"User-Agent": USER_AGENT},
    ) as session:
        tasks = [verify_url(session, h, semaphore) for h in hotels]
        results = await asyncio.gather(*tasks)

    # SSL check for OK urls that are HTTPS
    ssl_tasks = []
    ssl_indices = []
    for i, r in enumerate(results):
        if r["status"] == STATUS_OK and r.get("url", "").startswith("https://"):
            ssl_tasks.append(check_ssl(r["url"]))
            ssl_indices.append(i)

    if ssl_tasks:
        logger.info("Checking SSL for {} HTTPS URLs...", len(ssl_tasks))
        ssl_results = await asyncio.gather(*ssl_tasks)
        for idx, ssl_ok in zip(ssl_indices, ssl_results, strict=False):
            if not ssl_ok:
                results[idx]["status"] = STATUS_SSL_ERROR
                results[idx]["reason"] = "Invalid/expired SSL certificate"

    # Build report
    by_status: dict[str, list] = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    # Summary
    logger.info("=== URL Verification Report ===")
    for status, items in sorted(by_status.items(), key=lambda x: -len(x[1])):
        logger.info("  {:20s} {:>5d} hotels", status, len(items))

    # Write detailed report
    report_path = output_dir / "url_verification_report.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Full report -> {}", report_path)

    # Write flagged URLs (non-OK) as a simple list for review
    flagged = [r for r in results if r["status"] != STATUS_OK and r["status"] != STATUS_EMPTY]
    flagged_path = output_dir / "url_flagged.json"
    flagged_path.write_text(json.dumps(flagged, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Flagged URLs ({}) -> {}", len(flagged), flagged_path)

    # Update hotels with url_status field
    status_map = {r["hotel_id"]: r["status"] for r in results}
    for hotel in hotels:
        hotel["url_status"] = status_map.get(hotel.get("hotel_id", ""), "unknown")

    # Clear bad URLs (ssl_error excluded: browsers handle these fine)
    bad_statuses = {STATUS_PARKED, STATUS_UNREACHABLE, STATUS_INVALID}
    cleared = 0
    for hotel in hotels:
        if hotel.get("url_status") in bad_statuses:
            hotel["hotel_url"] = ""
            cleared += 1

    output_path = output_dir / "hotels_verified.json"
    output_path.write_text(json.dumps(hotels, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.success(
        "Done. {} OK, {} flagged, {} URLs cleared -> {}",
        len(by_status.get(STATUS_OK, [])),
        len(flagged),
        cleared,
        output_path,
    )


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Verify hotel URLs for validity.")
    p.add_argument("--input", default="data/scraped/hotels_en.json", help="Input hotels JSON.")
    p.add_argument("--output-dir", default="data/scraped", help="Output directory.")
    args = p.parse_args()
    asyncio.run(run(Path(args.input), Path(args.output_dir)))


if __name__ == "__main__":
    main()
