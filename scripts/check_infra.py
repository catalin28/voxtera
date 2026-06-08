"""One-shot health probe for live RAG infra (Qdrant + Elasticsearch + Redis).

Reads connection details from .env and prints what is actually populated.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv


def _get(url: str, headers: dict[str, str] | None = None, timeout: int = 10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def check_qdrant() -> None:
    url = os.environ["QDRANT_URL"].rstrip("/")
    key = os.environ.get("QDRANT_API_KEY", "")
    h = {"api-key": key} if key else {}
    try:
        _, body = _get(f"{url}/collections", h)
        cols = [c["name"] for c in body["result"]["collections"]]
        print(f"Qdrant @ {url}")
        print(f"  collections: {cols}")
        for name in cols:
            _, info = _get(f"{url}/collections/{name}", h)
            r = info["result"]
            params = r["config"]["params"]["vectors"]
            print(
                f"  - {name}: points={r['points_count']} "
                f"vectors={r.get('vectors_count')} "
                f"dim={params.get('size') if isinstance(params, dict) else params} "
                f"distance={params.get('distance') if isinstance(params, dict) else '?'}"
            )
    except Exception as e:  # noqa: BLE001
        print(f"Qdrant ERROR: {e}")


def check_es() -> None:
    url = os.environ["ELASTICSEARCH_URL"].rstrip("/")
    user = os.environ["ELASTICSEARCH_USER"]
    pw = os.environ["ELASTICSEARCH_PASSWORD"]
    auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    h = {"Authorization": auth}
    try:
        _, idxs = _get(f"{url}/_cat/indices?format=json", h)
        print(f"\nElasticsearch @ {url}")
        if not idxs:
            print("  (no indices)")
            return
        for idx in idxs:
            print(
                f"  - {idx['index']}: docs={idx['docs.count']} "
                f"size={idx['store.size']} health={idx['health']}"
            )
    except Exception as e:  # noqa: BLE001
        print(f"ES ERROR: {e}")


def check_redis() -> None:
    url = os.environ.get("REDIS_URL", "")
    print(f"\nRedis @ {url}")
    try:
        import redis  # type: ignore

        r = redis.Redis.from_url(url, socket_connect_timeout=5)
        pong = r.ping()
        info = r.info(section="server")
        keys = r.dbsize()
        print(f"  ping={pong} version={info.get('redis_version')} keys={keys}")
    except ModuleNotFoundError:
        print("  (redis package not installed — pip install redis)")
    except Exception as e:  # noqa: BLE001
        print(f"  Redis ERROR: {e}")


if __name__ == "__main__":
    from pathlib import Path
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path, override=False)
    if "QDRANT_URL" not in os.environ:
        print(f"WARN: QDRANT_URL not loaded from {env_path}")
    check_qdrant()
    check_es()
    check_redis()
    sys.exit(0)
