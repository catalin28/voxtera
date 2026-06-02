"""Elasticsearch and Qdrant client helpers for the call center RAG."""

from __future__ import annotations

import os

import aiohttp


def _es_url() -> str:
    return os.environ.get("ELASTICSEARCH_URL", "http://138.197.142.222:9200")


def _es_auth() -> aiohttp.BasicAuth:
    user = os.environ.get("ELASTICSEARCH_USER", "elastic")
    pw = os.environ.get("ELASTICSEARCH_PASSWORD", "")
    return aiohttp.BasicAuth(user, pw)


def _qdrant_url() -> str:
    return os.environ.get("QDRANT_URL", "http://138.197.142.222:6333")


def _qdrant_headers() -> dict[str, str]:
    key = os.environ.get("QDRANT_API_KEY", "")
    if key:
        return {"api-key": key}
    return {}


async def es_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    json: dict | list | None = None,
) -> dict:
    """Make an authenticated request to Elasticsearch."""
    url = f"{_es_url()}{path}"
    async with session.request(
        method, url, json=json, auth=_es_auth(), timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        return await resp.json()


async def qdrant_request(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    json: dict | list | None = None,
) -> dict:
    """Make an authenticated request to Qdrant."""
    url = f"{_qdrant_url()}{path}"
    async with session.request(
        method, url, json=json, headers=_qdrant_headers(), timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        return await resp.json()
