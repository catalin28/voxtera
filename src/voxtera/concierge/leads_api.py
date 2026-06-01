"""aiohttp Leads API — the single write path in front of the leads_calls table.

The bot (via its ``create_lead`` tool), the dial-in webhook handler, and a
future web form all go through this service rather than touching MySQL
directly, so the bot only ever holds an API token — never DB credentials.

Endpoints (all JSON):

- ``GET  /health``  — liveness probe, no auth.
- ``POST /calls``   — log an inbound call. Body: ``{caller_number, dialed_number}``.
                      Returns ``{"id": <int>}``. Called by the dial-in webhook.
- ``POST /leads``   — capture/booking. Body with ``id`` updates that row;
                      without ``id`` inserts a new standalone lead.
- ``GET  /leads``   — list recent rows. Query: ``limit`` (<=1000), ``status``.

Auth: every endpoint except ``/health`` requires ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
import json
from typing import Any

from aiohttp import web
from loguru import logger

from voxtera.concierge.db import LeadsStore

_STORE_KEY = web.AppKey("leads_store", LeadsStore)
_TOKEN_KEY = web.AppKey("api_token", str)


def _json(data: Any, *, status: int = 200) -> web.Response:
    """JSON response that tolerates datetime/Decimal via ``default=str``."""
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


@web.middleware
async def _auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Require a bearer token on everything except the health probe."""
    if request.path == "/health":
        return await handler(request)  # type: ignore[no-any-return]

    expected = request.app[_TOKEN_KEY]
    header = request.headers.get("Authorization", "")
    provided = header[7:] if header.startswith("Bearer ") else ""
    if not provided or not hmac.compare_digest(provided, expected):
        logger.warning("Rejected unauthenticated request to {}", request.path)
        return _json({"error": "unauthorized"}, status=401)
    return await handler(request)  # type: ignore[no-any-return]


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Parse a JSON object body, raising 400 on malformed input."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(text=json.dumps({"error": f"invalid JSON: {exc}"})) from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "body must be a JSON object"}))
    return body


async def _health(_request: web.Request) -> web.Response:
    return _json({"ok": True})


async def _post_call(request: web.Request) -> web.Response:
    body = await _read_json(request)
    store = request.app[_STORE_KEY]
    lead_id = await store.record_call(
        caller_number=body.get("caller_number"),
        dialed_number=body.get("dialed_number"),
    )
    return _json({"id": lead_id}, status=201)


async def _post_lead(request: web.Request) -> web.Response:
    body = await _read_json(request)
    store = request.app[_STORE_KEY]

    lead_id = body.pop("id", None)
    if lead_id is not None:
        matched = await store.update_lead(int(lead_id), **body)
        if not matched:
            return _json({"error": "lead not found"}, status=404)
        return _json({"id": int(lead_id), "updated": True})

    new_id = await store.create_lead(**body)
    return _json({"id": new_id, "created": True}, status=201)


async def _get_leads(request: web.Request) -> web.Response:
    store = request.app[_STORE_KEY]
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        raise web.HTTPBadRequest(text=json.dumps({"error": "limit must be an integer"})) from None
    status = request.query.get("status")
    rows = await store.list_leads(limit=limit, status=status)
    return _json({"leads": rows, "count": len(rows)})


def create_app(store: LeadsStore, *, token: str) -> web.Application:
    """Build the aiohttp application wired to ``store`` and the auth ``token``."""
    app = web.Application(middlewares=[_auth_middleware])
    app[_STORE_KEY] = store
    app[_TOKEN_KEY] = token
    app.add_routes(
        [
            web.get("/health", _health),
            web.post("/calls", _post_call),
            web.post("/leads", _post_lead),
            web.get("/leads", _get_leads),
        ]
    )
    return app
