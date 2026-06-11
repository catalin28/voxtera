"""Ingest Kempinski Çırağan Palace into Elasticsearch + Qdrant.

Reads data/seed/kempinski.json and upserts into the same indices used by the
main hotel fleet (ES index: "hotels", Qdrant collection: "hotel_kb").
Works alongside ingest_hotels.py without touching existing documents.

Usage:
    uv run python scripts/ingest_kempinski.py            # ES + Qdrant
    uv run python scripts/ingest_kempinski.py --es-only
    uv run python scripts/ingest_kempinski.py --qdrant-only

Environment variables (same as ingest_hotels.py):
    ELASTICSEARCH_URL        default: http://138.197.142.222:9200
    ELASTICSEARCH_USER       default: elastic
    ELASTICSEARCH_PASSWORD   required for remote
    QDRANT_URL               default: http://138.197.142.222:6333
    QDRANT_API_KEY           optional
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

SEED_FILE = Path(__file__).resolve().parent.parent / "data" / "seed" / "kempinski.json"

ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://138.197.142.222:9200")
ES_USER = os.environ.get("ELASTICSEARCH_USER", "elastic")
ES_PASS = os.environ.get("ELASTICSEARCH_PASSWORD", "")

QDRANT_URL = os.environ.get("QDRANT_URL", "http://138.197.142.222:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")


# ---------------------------------------------------------------------------
# Elasticsearch — upsert only, no delete/recreate of the whole index
# ---------------------------------------------------------------------------


async def ingest_es(hotels: list[dict]) -> None:
    """Upsert Kempinski hotel doc(s) into the existing hotels ES index.

    Does NOT recreate the index — safe to run alongside existing data.
    Creates the index if it doesn't exist yet (first run scenario).
    """
    auth = aiohttp.BasicAuth(ES_USER, ES_PASS)
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(auth=auth, timeout=timeout) as session:
        # Create index if missing (idempotent)
        async with session.head(f"{ES_URL}/{ES_INDEX}") as resp:
            if resp.status == 404:
                print(f"[ES] Index '{ES_INDEX}' not found, creating...")
                async with session.put(
                    f"{ES_URL}/{ES_INDEX}",
                    json=build_hotel_mapping(),
                    headers={"Content-Type": "application/json"},
                ) as create_resp:
                    body = await create_resp.json()
                    if not body.get("acknowledged"):
                        print(f"[ES] ERROR creating index: {body}")
                        return
                    print("[ES] Index created.")
            else:
                print(f"[ES] Index '{ES_INDEX}' exists — will upsert.")

        # Build bulk upsert body
        bulk_lines = []
        for hotel in hotels:
            # Use index action (upsert by _id)
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
                "coordinates": {
                    "lat": hotel["coordinates"][0],
                    "lon": hotel["coordinates"][1],
                },
                "activity_tags": hotel.get("activity_tags", []),
                "beachfront": hotel.get("beachfront", False),
                "adults_only": hotel.get("adults_only", False),
                "board_type": hotel.get("board_type", ""),
                "star_rating": hotel.get("star_rating", 0),
                "hotel_url": hotel.get("hotel_url", ""),
            }
            bulk_lines.append(json.dumps(action))
            bulk_lines.append(json.dumps(doc))

        bulk_body = "\n".join(bulk_lines) + "\n"
        async with session.post(
            f"{ES_URL}/_bulk",
            data=bulk_body.encode(),
            headers={"Content-Type": "application/x-ndjson"},
        ) as resp:
            result = await resp.json()
            errors = [
                item for item in result.get("items", [])
                if "error" in item.get("index", {})
            ]
            if errors:
                print(f"[ES] {len(errors)} error(s):")
                for err in errors:
                    print(f"  {err['index']['error']}")
            else:
                print(f"[ES] Done: {len(result.get('items', []))} doc(s) indexed/upserted")

        # Refresh so the new docs are immediately searchable
        async with session.post(f"{ES_URL}/{ES_INDEX}/_refresh"):
            pass


# ---------------------------------------------------------------------------
# Qdrant — upsert chunks, keyed by (hotel_id, chunk_index)
# ---------------------------------------------------------------------------


async def _ensure_collection(session: aiohttp.ClientSession) -> None:
    """Create the Qdrant collection if it doesn't exist."""
    async with session.get(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}") as resp:
        if resp.status == 200:
            print(f"[Qdrant] Collection '{QDRANT_COLLECTION}' exists.")
            return

    print(f"[Qdrant] Creating collection '{QDRANT_COLLECTION}'...")
    async with session.put(
        f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
        json={"vectors": {"size": EMBEDDING_DIM, "distance": "Cosine"}},
    ) as resp:
        result = await resp.json()
        print(f"[Qdrant] Create collection: {result.get('status', result)}")


async def ingest_qdrant(hotels: list[dict]) -> None:
    """Embed Kempinski chunks and upsert into Qdrant."""
    headers = {"Content-Type": "application/json"}
    if QDRANT_API_KEY:
        headers["api-key"] = QDRANT_API_KEY

    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        await _ensure_collection(session)

        # Flatten all chunks across all hotels in the seed file
        all_chunks: list[dict] = []
        for hotel in hotels:
            for i, chunk in enumerate(hotel.get("chunks", [])):
                chunk_id = f"{hotel['hotel_id']}_{i}"
                all_chunks.append(
                    {
                        "id": chunk_id,
                        "hotel_id": hotel["hotel_id"],
                        "hotel_name": hotel["name"],
                        "category": chunk.get("category", ""),
                        "text": chunk["text"],
                        "text_en": chunk.get("text_en", chunk["text"]),
                        "region": hotel.get("region", ""),
                        "price_tier": hotel.get("price_tier", ""),
                        "country": hotel.get("country", ""),
                        "district": hotel.get("district", ""),
                        "activity_tags": hotel.get("activity_tags", []),
                        "hotel_url": hotel.get("hotel_url", ""),
                    }
                )

        print(f"[Qdrant] Total chunks to embed: {len(all_chunks)}")

        batch_size = 32
        total_upserted = 0

        for batch_start in range(0, len(all_chunks), batch_size):
            batch = all_chunks[batch_start: batch_start + batch_size]
            # Embed with text_en for consistent language (model handles multilingual
            # but English embeds more reliably for cross-lingual retrieval).
            texts = [c["text_en"] for c in batch]

            t0 = time.perf_counter()
            embeddings = embed_texts(texts, prefix=PREFIX_PASSAGE)
            embed_ms = (time.perf_counter() - t0) * 1000

            points = []
            for j, chunk in enumerate(batch):
                # Stable integer ID: abs(hash) keeps it deterministic across runs
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
                            "hotel_url": chunk["hotel_url"],
                        },
                    }
                )

            async with session.put(
                f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
                json={"points": points},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                result = await resp.json()
                ok = (
                    result.get("status") == "ok"
                    or result.get("result", {}).get("status") == "completed"
                )
                if ok:
                    total_upserted += len(points)
                else:
                    print(f"[Qdrant] Batch {batch_start} error: {result}")

            print(
                f"[Qdrant] {batch_start + len(batch)}/{len(all_chunks)} chunks "
                f"({embed_ms:.0f}ms embed)"
            )

        print(f"[Qdrant] Done: {total_upserted} point(s) upserted")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    if not SEED_FILE.exists():
        print(f"ERROR: Seed file not found: {SEED_FILE}")
        sys.exit(1)

    hotels = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(hotels)} hotel(s) from {SEED_FILE.name}")
    for h in hotels:
        n_chunks = len(h.get("chunks", []))
        print(f"  {h['hotel_id']}  ({n_chunks} chunks)")

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
