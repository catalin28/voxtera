"""Translate hotel chunks from Turkish to English and add hotel_url field.

Reads ``data/scraped/hotels.json``, fills empty ``text_en`` fields using
OpenAI gpt-4.1-nano batch translation, and merges ``hotel_url`` from
``data/scraped/hotel_urls.jsonl``. Writes to ``data/scraped/hotels_en.json``.

Resumable: keeps a checkpoint so interrupted runs can continue.

Requirements:
    - OPENAI_API_KEY environment variable set
    - openai package installed (already in project deps)

Usage:
    uv run python scripts/translate_hotels.py
    uv run python scripts/translate_hotels.py --input data/scraped/hotels.json --batch-size 20
    uv run python scripts/translate_hotels.py --resume  # continue from checkpoint
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

try:
    from openai import AsyncOpenAI
except ImportError:
    logger.error("openai package required. Install with: pip install openai")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Translation via OpenAI
# ---------------------------------------------------------------------------

MODEL = "gpt-4.1-nano"
SYSTEM_PROMPT = (
    "You are a professional translator. Translate the following Turkish hotel "
    "description text to natural, fluent English. Preserve factual details "
    "(hotel names, place names, numbers, times). Output ONLY the translated text, "
    "nothing else."
)

# Rate limiting
MAX_CONCURRENT = 10
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0


async def translate_text(client: AsyncOpenAI, text: str, semaphore: asyncio.Semaphore) -> str:
    """Translate a single Turkish text to English using OpenAI."""
    if not text or not text.strip():
        return ""

    async with semaphore:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.2,
                    max_tokens=2000,
                )
                return (response.choices[0].message.content or "").strip()
            except Exception as e:
                if attempt < RETRY_ATTEMPTS - 1:
                    logger.warning("Translation retry {}/{}: {}", attempt + 1, RETRY_ATTEMPTS, e)
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                else:
                    logger.error("Translation failed after {} attempts: {}", RETRY_ATTEMPTS, e)
                    return ""


async def translate_hotel_chunks(
    client: AsyncOpenAI, hotel: dict[str, Any], semaphore: asyncio.Semaphore
) -> dict[str, Any]:
    """Translate all chunks for a single hotel concurrently."""
    chunks = hotel.get("chunks", [])
    tasks = []
    for chunk in chunks:
        if chunk.get("text_en"):
            # Already translated
            tasks.append(asyncio.ensure_future(asyncio.coroutine(lambda t=chunk["text_en"]: t)()))
        else:
            tasks.append(translate_text(client, chunk.get("text", ""), semaphore))

    translations = await asyncio.gather(*tasks)
    for chunk, text_en in zip(chunks, translations, strict=False):
        chunk["text_en"] = text_en

    return hotel


# ---------------------------------------------------------------------------
# URL merging
# ---------------------------------------------------------------------------


def load_hotel_urls(urls_path: Path) -> dict[str, str]:
    """Load hotel_id -> website mapping from hotel_urls.jsonl."""
    url_map: dict[str, str] = {}
    if not urls_path.exists():
        logger.warning("URL file not found: {}", urls_path)
        return url_map

    with urls_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                hid = record.get("hotel_id", "")
                website = record.get("website", "")
                if hid and website:
                    # hotel_urls.jsonl uses raw IDs, hotels.json uses "parafly_<id>"
                    url_map[f"parafly_{hid}"] = website
            except json.JSONDecodeError:
                continue

    logger.info("Loaded {} hotel URLs from {}", len(url_map), urls_path.name)
    return url_map


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    urls_path = Path(args.urls)
    output_path = Path(args.output)
    ckpt_path = output_path.parent / ".translate_checkpoint.json"

    if not input_path.exists():
        logger.error("Input file not found: {}", input_path)
        sys.exit(1)

    # Load hotels
    hotels: list[dict[str, Any]] = json.loads(input_path.read_text(encoding="utf-8"))
    logger.info("Loaded {} hotels from {}", len(hotels), input_path.name)

    # Load URLs
    url_map = load_hotel_urls(urls_path)

    # Add hotel_url field
    matched_urls = 0
    for hotel in hotels:
        hid = hotel.get("hotel_id", "")
        url = url_map.get(hid, "")
        hotel["hotel_url"] = url
        if url:
            matched_urls += 1
    logger.info("Matched {} / {} hotels with URLs", matched_urls, len(hotels))

    # Resume state
    done_ids: set[str] = set()
    if args.resume and ckpt_path.exists():
        try:
            done_ids = set(json.loads(ckpt_path.read_text(encoding="utf-8")))
            logger.info("Resuming: {} hotels already translated", len(done_ids))
        except json.JSONDecodeError:
            pass

    # If resuming, load existing output to preserve translations
    if args.resume and output_path.exists() and done_ids:
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            existing_map = {h["hotel_id"]: h for h in existing}
            # Merge existing translations into current hotels
            for hotel in hotels:
                hid = hotel.get("hotel_id", "")
                if hid in existing_map:
                    for i, chunk in enumerate(hotel.get("chunks", [])):
                        if i < len(existing_map[hid].get("chunks", [])):
                            existing_en = existing_map[hid]["chunks"][i].get("text_en", "")
                            if existing_en:
                                chunk["text_en"] = existing_en
        except (json.JSONDecodeError, KeyError):
            pass

    # Translate
    client = AsyncOpenAI()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    translated = 0
    total_chunks = 0

    for i, hotel in enumerate(hotels):
        hid = hotel.get("hotel_id", "")
        if hid in done_ids:
            continue

        # Skip hotels with no chunks needing translation
        needs_translation = any(
            not chunk.get("text_en") and chunk.get("text") for chunk in hotel.get("chunks", [])
        )
        if not needs_translation:
            done_ids.add(hid)
            continue

        # Translate chunks for this hotel
        chunks_to_translate = [
            c for c in hotel.get("chunks", []) if not c.get("text_en") and c.get("text")
        ]
        tasks = [translate_text(client, c["text"], semaphore) for c in chunks_to_translate]
        results = await asyncio.gather(*tasks)

        # Apply translations
        result_idx = 0
        for chunk in hotel.get("chunks", []):
            if not chunk.get("text_en") and chunk.get("text"):
                chunk["text_en"] = results[result_idx]
                result_idx += 1

        done_ids.add(hid)
        translated += 1
        total_chunks += len(chunks_to_translate)

        # Checkpoint every batch_size hotels
        if translated % args.batch_size == 0:
            _write_output(output_path, hotels)
            ckpt_path.write_text(json.dumps(sorted(done_ids)), encoding="utf-8")
            logger.info(
                "Checkpoint: {}/{} hotels translated ({} chunks so far)",
                translated,
                len(hotels) - len(done_ids) + translated,
                total_chunks,
            )

    # Final write
    _write_output(output_path, hotels)
    ckpt_path.write_text(json.dumps(sorted(done_ids)), encoding="utf-8")

    # Also write jsonl
    jsonl_path = output_path.with_suffix(".jsonl")
    with jsonl_path.open("w", encoding="utf-8") as f:
        for h in hotels:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")

    logger.success(
        "Done. {} hotels translated ({} chunks), {} with URLs -> {}",
        translated,
        total_chunks,
        matched_urls,
        output_path,
    )


def _write_output(path: Path, hotels: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hotels, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Translate hotel chunks to English and add hotel_url.")
    p.add_argument("--input", default="data/scraped/hotels.json", help="Input hotels JSON.")
    p.add_argument("--urls", default="data/scraped/hotel_urls.jsonl", help="Hotel URLs JSONL.")
    p.add_argument("--output", default="data/scraped/hotels_en.json", help="Output file.")
    p.add_argument("--batch-size", type=int, default=20, help="Checkpoint every N hotels.")
    p.add_argument("--resume", action="store_true", default=True, help="Resume from checkpoint.")
    p.add_argument("--no-resume", dest="resume", action="store_false", help="Start fresh.")
    return p.parse_args(argv)


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
