"""Ingest hotels into Elasticsearch (bulk) and Qdrant (batched embeddings).

Usage:
    uv run python scripts/ingest_hotels.py
    # ES only:
    uv run python scripts/ingest_hotels.py --es-only
    # Qdrant only:
    uv run python scripts/ingest_hotels.py --qdrant-only
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import aiohttp  # noqa: E402

from voxtera.call_center.embeddings import PREFIX_PASSAGE, embed_texts  # noqa: E402
from voxtera.call_center.index_config import ES_INDEX, build_hotel_mapping  # noqa: E402
from voxtera.call_center.kb_config import EMBEDDING_DIM, QDRANT_COLLECTION  # noqa: E402

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "seed" / "hotels.json"

ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://138.197.142.222:9200")
ES_USER = os.environ.get("ELASTICSEARCH_USER", "elastic")
ES_PASS = os.environ.get("ELASTICSEARCH_PASSWORD", "")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://138.197.142.222:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")


async def ingest_es(hotels: list[dict]) -> None:
    """Bulk-index hotels into Elasticsearch."""
    auth = aiohttp.BasicAuth(ES_USER, ES_PASS)
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        # Delete and recreate index
        print(f"[ES] Deleting index '{ES_INDEX}'...")
        async with session.delete(f"{ES_URL}/{ES_INDEX}") as resp:
            print(f"[ES] Delete: {resp.status}")

        print("[ES] Creating index with mapping...")
        async with session.put(
            f"{ES_URL}/{ES_INDEX}",
            json=build_hotel_mapping(),
            headers={"Content-Type": "application/json"},
        ) as resp:
            body = await resp.json()
            if not body.get("acknowledged"):
                print(f"[ES] ERROR creating index: {body}")
                return
            print("[ES] Index created.")

        # Build bulk body (ndjson)
        bulk_lines = []
        for hotel in hotels:
            action = {"index": {"_index": ES_INDEX, "_id": hotel["hotel_id"]}}
            doc = {
                "hotel_id": hotel["hotel_id"],
                "name": hotel["name"],
                "aliases": hotel.get("aliases", []),
                "chain": hotel.get("chain", ""),
                "city": hotel.get("city", ""),
                "district": hotel.get("district", ""),
                "region": hotel.get("region", ""),
                "country": hotel.get("country", ""),
                "price_tier": hotel.get("price_tier", ""),
                "coordinates": {"lat": hotel["coordinates"][0], "lon": hotel["coordinates"][1]},
                "activity_tags": hotel.get("activity_tags", []),
                "beachfront": hotel.get("beachfront", False),
                "adults_only": hotel.get("adults_only", False),
                "board_type": hotel.get("board_type", ""),
                "star_rating": hotel.get("star_rating", 0),
                "hotel_url": hotel.get("hotel_url", ""),
            }
            bulk_lines.append(json.dumps(action))
            bulk_lines.append(json.dumps(doc))

        # Send bulk request in chunks of 200
        chunk_size = 200
        total_indexed = 0
        total_errors = 0

        for i in range(0, len(hotels), chunk_size):
            chunk_lines = bulk_lines[i * 2 : (i + chunk_size) * 2]
            chunk_body = "\n".join(chunk_lines) + "\n"

            async with session.post(
                f"{ES_URL}/_bulk",
                data=chunk_body.encode(),
                headers={"Content-Type": "application/x-ndjson"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                result = await resp.json()
                if result.get("errors"):
                    errs = [item for item in result["items"] if "error" in item.get("index", {})]
                    total_errors += len(errs)
                    if errs:
                        print(f"[ES] Batch {i}-{i+chunk_size}: {len(errs)} errors")
                        print(f"[ES]   First error: {errs[0]['index']['error']}")
                else:
                    batch_count = len(result["items"])
                    total_indexed += batch_count
                    print(f"[ES] Batch {i}-{i+chunk_size}: {batch_count} indexed OK")

        # Refresh
        async with session.post(f"{ES_URL}/{ES_INDEX}/_refresh") as resp:
            pass

        print(f"[ES] Done: {total_indexed} indexed, {total_errors} errors")


async def ingest_qdrant(hotels: list[dict]) -> None:
    """Embed and upsert hotel chunks into Qdrant."""
    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # Ensure collection exists
        async with session.get(f"{QDRANT_URL}/collections") as resp:
            data = await resp.json()
            existing = [c["name"] for c in data.get("result", {}).get("collections", [])]

        if QDRANT_COLLECTION not in existing:
            print(f"[Qdrant] Creating collection '{QDRANT_COLLECTION}'...")
            async with session.put(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
                json={"vectors": {"size": EMBEDDING_DIM, "distance": "Cosine"}},
            ) as resp:
                result = await resp.json()
                print(f"[Qdrant] Create collection: {result.get('status', result)}")
        else:
            # Delete and recreate for clean state
            print(f"[Qdrant] Deleting collection '{QDRANT_COLLECTION}'...")
            async with session.delete(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}") as resp:
                pass
            print(f"[Qdrant] Creating collection '{QDRANT_COLLECTION}'...")
            async with session.put(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
                json={"vectors": {"size": EMBEDDING_DIM, "distance": "Cosine"}},
            ) as resp:
                result = await resp.json()
                print(f"[Qdrant] Create collection: {result.get('status', result)}")

        # Collect all chunks
        all_chunks = []
        for hotel in hotels:
            for i, chunk in enumerate(hotel.get("chunks", [])):
                all_chunks.append(
                    {
                        "id": f"{hotel['hotel_id']}_{i}",
                        "hotel_id": hotel["hotel_id"],
                        "hotel_name": hotel["name"],
                        "category": chunk.get("category", ""),
                        "text": chunk["text"],
                        "text_en": chunk.get("text_en", ""),
                        "region": hotel.get("region", ""),
                        "price_tier": hotel.get("price_tier", ""),
                        "country": hotel.get("country", ""),
                        "district": hotel.get("district", ""),
                        "activity_tags": hotel.get("activity_tags", []),
                        "hotel_url": hotel.get("hotel_url", ""),
                    }
                )

        print(f"[Qdrant] Total chunks to embed: {len(all_chunks)}")

        # Embed and upsert in batches
        batch_size = 32
        total_upserted = 0

        for batch_start in range(0, len(all_chunks), batch_size):
            batch = all_chunks[batch_start : batch_start + batch_size]
            texts = [c["text"] for c in batch]

            t0 = time.perf_counter()
            embeddings = embed_texts(texts, prefix=PREFIX_PASSAGE)
            embed_ms = (time.perf_counter() - t0) * 1000

            # Build points
            points = []
            for j, chunk in enumerate(batch):
                point_id = abs(hash(chunk["id"])) % (2**63)
                points.append(
                    {
                        "id": point_id,
                        "vector": embeddings[j],
                        "payload": {
                            "chunk_id": chunk["id"],
                            "hotel_id": chunk["hotel_id"],
                            "hotel_name": chunk["hotel_name"],
                            "category": chunk["category"],
                            "text": chunk["text"],
                            "text_en": chunk["text_en"],
                            "region": chunk["region"],
                            "price_tier": chunk["price_tier"],
                            "country": chunk["country"],
                            "district": chunk["district"],
                            "activity_tags": chunk["activity_tags"],
                            "hotel_url": chunk.get("hotel_url", ""),
                        },
                    }
                )

            async with session.put(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
                json={"points": points},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                result = await resp.json()
                if (
                    result.get("status") == "ok"
                    or result.get("result", {}).get("status") == "completed"
                ):
                    total_upserted += len(points)
                else:
                    print(f"[Qdrant] Batch {batch_start} error: {result}")

            if (batch_start // batch_size) % 10 == 0:
                print(
                    f"[Qdrant] Progress: {batch_start + len(batch)}/{len(all_chunks)} "
                    f"chunks ({embed_ms:.0f}ms embed)"
                )

        print(f"[Qdrant] Done: {total_upserted} points upserted")


async def main() -> None:
    if not SEED_FILE.exists():
        print(f"ERROR: Seed file not found: {SEED_FILE}")
        sys.exit(1)

    hotels = json.loads(SEED_FILE.read_text())
    print(f"Loaded {len(hotels)} hotels from {SEED_FILE.name}")

    es_only = "--es-only" in sys.argv
    qdrant_only = "--qdrant-only" in sys.argv

    if not qdrant_only:
        t0 = time.perf_counter()
        await ingest_es(hotels)
        print(f"[ES] Total time: {time.perf_counter() - t0:.1f}s\n")

    if not es_only:
        t0 = time.perf_counter()
        await ingest_qdrant(hotels)
        print(f"[Qdrant] Total time: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
