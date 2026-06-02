"""Call Center RAG admin server — aiohttp routes and UI.

Endpoints (all under /call_center/):

  GET  /call_center/                — Admin UI (HTML)
  GET  /call_center/api/status      — Health check (ES + Qdrant connectivity)

  # Elasticsearch
  POST /call_center/api/es/load     — Load seed hotels.json into ES
  GET  /call_center/api/es/hotels   — List all hotels in ES index
  GET  /call_center/api/es/search?q=...  — Search ES hotel resolver

  # Qdrant
  POST /call_center/api/qdrant/load — Embed chunks & upsert into Qdrant
  GET  /call_center/api/qdrant/collections — List Qdrant collections
  GET  /call_center/api/qdrant/points?collection=...&hotel_id=... — Browse points
  POST /call_center/api/qdrant/search — Semantic search (embed query → search)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiohttp as aiohttp_lib
from aiohttp import web
from loguru import logger

from voxtera.call_center.clients import es_request, qdrant_request

SEED_FILE = Path(__file__).resolve().parents[3] / "data" / "seed" / "hotels.json"

# ES index for hotel resolution
ES_INDEX = "hotels"

# Qdrant collection for knowledge chunks
QDRANT_COLLECTION = "hotel_kb"

# Embedding config
EMBEDDING_MODEL = "intfloat/multilingual-e5-large"
EMBEDDING_DIM = 1024
PREFIX_PASSAGE = "passage: "
PREFIX_QUERY = "query: "

# Lazy-loaded model
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: {}", EMBEDDING_MODEL)
        t0 = time.perf_counter()
        _model = SentenceTransformer(EMBEDDING_MODEL)
        logger.info("Model loaded in {:.1f}s", time.perf_counter() - t0)
    return _model


def _embed_texts(texts: list[str], prefix: str = PREFIX_QUERY) -> list[list[float]]:
    """Embed texts using e5-large with the given prefix."""
    model = _get_model()
    prefixed = [f"{prefix}{t}" for t in texts]
    embeddings = model.encode(prefixed, normalize_embeddings=True)
    return embeddings.tolist()


# --- ES Index Mapping ---

ES_HOTEL_MAPPING = {
    "settings": {
        "analysis": {
            "analyzer": {
                "turkish_custom": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "turkish_stop", "turkish_stemmer"],
                },
            },
            "filter": {
                "turkish_stop": {"type": "stop", "stopwords": "_turkish_"},
                "turkish_stemmer": {"type": "stemmer", "language": "turkish"},
            },
        }
    },
    "mappings": {
        "properties": {
            "hotel_id": {"type": "keyword"},
            "name": {
                "type": "text",
                "analyzer": "turkish_custom",
                "fields": {"raw": {"type": "keyword"}},
            },
            "aliases": {"type": "text", "analyzer": "turkish_custom"},
            "chain": {"type": "keyword"},
            "city": {"type": "keyword"},
            "district": {"type": "keyword"},
            "region": {"type": "keyword"},
            "country": {"type": "keyword"},
            "price_tier": {"type": "keyword"},
            "coordinates": {"type": "geo_point"},
            "activity_tags": {"type": "keyword"},
            "beachfront": {"type": "boolean"},
            "adults_only": {"type": "boolean"},
            "board_type": {"type": "keyword"},
            "star_rating": {"type": "integer"},
        }
    },
}


# --- Route Handlers ---


async def handle_status(request: web.Request) -> web.Response:
    """Check connectivity to ES and Qdrant."""
    session: aiohttp_lib.ClientSession = request.app["http_session"]
    status = {"es": "error", "qdrant": "error"}
    try:
        es = await es_request(session, "GET", "/")
        status["es"] = "ok"
        status["es_version"] = es.get("version", {}).get("number", "?")
    except Exception as e:
        status["es_error"] = str(e)
    try:
        qd = await qdrant_request(session, "GET", "/collections")
        status["qdrant"] = "ok"
        status["qdrant_collections"] = len(qd.get("result", {}).get("collections", []))
    except Exception as e:
        status["qdrant_error"] = str(e)
    return web.json_response(status)


async def handle_es_load(request: web.Request) -> web.Response:
    """Load seed hotels into Elasticsearch."""
    session: aiohttp_lib.ClientSession = request.app["http_session"]

    # Read seed file
    if not SEED_FILE.exists():
        return web.json_response({"error": f"Seed file not found: {SEED_FILE}"}, status=404)

    hotels = json.loads(SEED_FILE.read_text())

    # Delete index if exists, then create with mapping
    await es_request(session, "DELETE", f"/{ES_INDEX}")
    create_resp = await es_request(session, "PUT", f"/{ES_INDEX}", json=ES_HOTEL_MAPPING)
    if create_resp.get("error"):
        return web.json_response(
            {"error": "Failed to create index", "detail": create_resp}, status=500
        )

    # Index each hotel
    indexed = 0
    errors = []
    for hotel in hotels:
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
        }
        resp = await es_request(session, "PUT", f"/{ES_INDEX}/_doc/{hotel['hotel_id']}", json=doc)
        if resp.get("error"):
            errors.append({"hotel_id": hotel["hotel_id"], "error": resp["error"]})
        else:
            indexed += 1

    # Refresh index
    await es_request(session, "POST", f"/{ES_INDEX}/_refresh")

    return web.json_response({"indexed": indexed, "total": len(hotels), "errors": errors})


async def handle_es_hotels(request: web.Request) -> web.Response:
    """List all hotels in ES."""
    session: aiohttp_lib.ClientSession = request.app["http_session"]
    resp = await es_request(
        session,
        "POST",
        f"/{ES_INDEX}/_search",
        json={
            "size": 100,
            "query": {"match_all": {}},
            "_source": [
                "hotel_id",
                "name",
                "chain",
                "district",
                "price_tier",
                "board_type",
                "star_rating",
            ],
        },
    )
    hits = resp.get("hits", {}).get("hits", [])
    hotels = [h["_source"] for h in hits]
    return web.json_response({"count": len(hotels), "hotels": hotels})


async def handle_es_search(request: web.Request) -> web.Response:
    """Search ES hotel resolver."""
    q = request.query.get("q", "")
    if not q:
        return web.json_response({"error": "Missing ?q= parameter"}, status=400)

    session: aiohttp_lib.ClientSession = request.app["http_session"]
    resp = await es_request(
        session,
        "POST",
        f"/{ES_INDEX}/_search",
        json={
            "size": 10,
            "query": {
                "multi_match": {
                    "query": q,
                    "fields": ["name^3", "aliases^2", "chain", "district", "city"],
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                }
            },
        },
    )
    hits = resp.get("hits", {}).get("hits", [])
    results = [
        {"hotel_id": h["_source"]["hotel_id"], "name": h["_source"]["name"], "score": h["_score"]}
        for h in hits
    ]
    return web.json_response({"query": q, "count": len(results), "results": results})


async def handle_qdrant_load(request: web.Request) -> web.Response:
    """Embed all chunks and upsert into Qdrant."""
    session: aiohttp_lib.ClientSession = request.app["http_session"]

    if not SEED_FILE.exists():
        return web.json_response({"error": f"Seed file not found: {SEED_FILE}"}, status=404)

    hotels = json.loads(SEED_FILE.read_text())

    # Ensure collection exists
    collections_resp = await qdrant_request(session, "GET", "/collections")
    existing = [c["name"] for c in collections_resp.get("result", {}).get("collections", [])]

    if QDRANT_COLLECTION not in existing:
        await qdrant_request(
            session,
            "PUT",
            f"/collections/{QDRANT_COLLECTION}",
            json={
                "vectors": {"size": EMBEDDING_DIM, "distance": "Cosine"},
            },
        )

    # Collect all chunks with metadata
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
                }
            )

    # Embed in batches
    batch_size = 16
    total_upserted = 0
    errors = []

    for batch_start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[batch_start : batch_start + batch_size]
        texts = [c["text"] for c in batch]

        t0 = time.perf_counter()
        embeddings = _embed_texts(texts, prefix=PREFIX_PASSAGE)
        embed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "Embedded batch {}-{} ({} chunks) in {:.0f}ms",
            batch_start,
            batch_start + len(batch),
            len(batch),
            embed_ms,
        )

        # Build Qdrant points
        points = []
        for j, chunk in enumerate(batch):
            # Use a deterministic numeric ID from a hash
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
                    },
                }
            )

        resp = await qdrant_request(
            session,
            "PUT",
            f"/collections/{QDRANT_COLLECTION}/points",
            json={
                "points": points,
            },
        )
        if resp.get("status") == "ok" or resp.get("result", {}).get("status") == "completed":
            total_upserted += len(points)
        else:
            errors.append({"batch_start": batch_start, "response": resp})

    return web.json_response(
        {
            "total_chunks": len(all_chunks),
            "upserted": total_upserted,
            "errors": errors,
        }
    )


async def handle_qdrant_collections(request: web.Request) -> web.Response:
    """List Qdrant collections."""
    session: aiohttp_lib.ClientSession = request.app["http_session"]
    resp = await qdrant_request(session, "GET", "/collections")
    collections = resp.get("result", {}).get("collections", [])
    return web.json_response({"collections": collections})


async def handle_qdrant_points(request: web.Request) -> web.Response:
    """Browse points in a Qdrant collection, optionally filtered by hotel_id."""
    session: aiohttp_lib.ClientSession = request.app["http_session"]
    collection = request.query.get("collection", QDRANT_COLLECTION)
    hotel_id = request.query.get("hotel_id", "")
    limit = int(request.query.get("limit", "20"))

    body: dict = {"limit": limit, "with_payload": True, "with_vector": False}
    if hotel_id:
        body["filter"] = {"must": [{"key": "hotel_id", "match": {"value": hotel_id}}]}

    resp = await qdrant_request(
        session, "POST", f"/collections/{collection}/points/scroll", json=body
    )
    points = resp.get("result", {}).get("points", [])
    return web.json_response({"collection": collection, "count": len(points), "points": points})


async def handle_qdrant_search(request: web.Request) -> web.Response:
    """Semantic search: embed query and search Qdrant."""
    body = await request.json()
    query_text = body.get("query", "")
    hotel_id = body.get("hotel_id", "")
    limit = body.get("limit", 5)

    if not query_text:
        return web.json_response({"error": "Missing 'query' in body"}, status=400)

    session: aiohttp_lib.ClientSession = request.app["http_session"]

    # Embed query
    t0 = time.perf_counter()
    embeddings = _embed_texts([query_text], prefix=PREFIX_QUERY)
    embed_ms = (time.perf_counter() - t0) * 1000

    # Search Qdrant
    search_body: dict = {
        "vector": embeddings[0],
        "limit": limit,
        "with_payload": True,
    }
    if hotel_id:
        search_body["filter"] = {"must": [{"key": "hotel_id", "match": {"value": hotel_id}}]}

    resp = await qdrant_request(
        session, "POST", f"/collections/{QDRANT_COLLECTION}/points/search", json=search_body
    )
    results = resp.get("result", [])

    return web.json_response(
        {
            "query": query_text,
            "embed_ms": round(embed_ms, 1),
            "count": len(results),
            "results": [
                {
                    "score": r["score"],
                    "hotel_id": r["payload"].get("hotel_id"),
                    "hotel_name": r["payload"].get("hotel_name"),
                    "category": r["payload"].get("category"),
                    "text": r["payload"].get("text"),
                    "text_en": r["payload"].get("text_en"),
                }
                for r in results
            ],
        }
    )


async def handle_ui(request: web.Request) -> web.Response:
    """Serve the admin UI."""
    html_path = Path(__file__).parent / "ui.html"
    return web.Response(text=html_path.read_text(), content_type="text/html")


# --- App Factory ---


async def _on_startup(app: web.Application) -> None:
    app["http_session"] = aiohttp_lib.ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    await app["http_session"].close()


def create_app() -> web.Application:
    """Create the call_center admin aiohttp app."""
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    # UI
    app.router.add_get("/call_center/", handle_ui)
    app.router.add_get("/call_center", handle_ui)

    # API
    app.router.add_get("/call_center/api/status", handle_status)

    # ES endpoints
    app.router.add_post("/call_center/api/es/load", handle_es_load)
    app.router.add_get("/call_center/api/es/hotels", handle_es_hotels)
    app.router.add_get("/call_center/api/es/search", handle_es_search)

    # Qdrant endpoints
    app.router.add_post("/call_center/api/qdrant/load", handle_qdrant_load)
    app.router.add_get("/call_center/api/qdrant/collections", handle_qdrant_collections)
    app.router.add_get("/call_center/api/qdrant/points", handle_qdrant_points)
    app.router.add_post("/call_center/api/qdrant/search", handle_qdrant_search)

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    port = int(os.environ.get("CALL_CENTER_PORT", "8100"))
    logger.info("Starting Call Center admin server on port {}", port)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
