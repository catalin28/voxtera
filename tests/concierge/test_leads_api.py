"""Tests for the aiohttp Leads API.

These exercise the HTTP layer (routing, auth, request/response shapes) against
an in-memory fake store, so no MySQL — or the ``aiomysql`` driver — is needed.
``asyncio_mode = "auto"`` (pyproject) lets the async tests/fixtures run directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from voxtera.concierge.db import LeadsStore
from voxtera.concierge.leads_api import create_app

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeStore:
    """In-memory :class:`LeadsStore` for HTTP-layer tests."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next_id = 0

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def record_call(self, *, caller_number: str | None, dialed_number: str | None) -> int:
        lead_id = self._new_id()
        self.rows[lead_id] = {
            "id": lead_id,
            "caller_number": caller_number,
            "dialed_number": dialed_number,
            "status": "ringing",
        }
        return lead_id

    async def update_lead(self, lead_id: int, **fields: Any) -> bool:
        if lead_id not in self.rows:
            return False
        self.rows[lead_id].update({k: v for k, v in fields.items() if v is not None})
        return True

    async def create_lead(self, **fields: Any) -> int:
        lead_id = self._new_id()
        self.rows[lead_id] = {"id": lead_id, "status": "captured", **fields}
        return lead_id

    async def list_leads(
        self, *, limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = list(self.rows.values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return rows[:limit]


@pytest.fixture
async def client():
    store = FakeStore()
    app = create_app(store, token=TOKEN)
    async with TestClient(TestServer(app)) as c:
        c.store = store  # type: ignore[attr-defined]  # handy for assertions
        yield c


def test_fake_store_satisfies_protocol():
    assert isinstance(FakeStore(), LeadsStore)


async def test_health_needs_no_auth(client):
    resp = await client.get("/health")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}


async def test_protected_route_rejects_missing_token(client):
    resp = await client.post("/calls", json={"caller_number": "+15550001111"})
    assert resp.status == 401


async def test_protected_route_rejects_wrong_token(client):
    resp = await client.post(
        "/calls", json={"caller_number": "+1"}, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status == 401


async def test_post_call_logs_row(client):
    resp = await client.post(
        "/calls",
        json={"caller_number": "+15550001111", "dialed_number": "+12363124419"},
        headers=AUTH,
    )
    assert resp.status == 201
    body = await resp.json()
    lead_id = body["id"]
    assert client.store.rows[lead_id]["caller_number"] == "+15550001111"
    assert client.store.rows[lead_id]["status"] == "ringing"


async def test_post_lead_updates_existing(client):
    created = await (await client.post("/calls", json={"caller_number": "+1"}, headers=AUTH)).json()
    lead_id = created["id"]

    resp = await client.post(
        "/leads",
        json={"id": lead_id, "name": "Dana", "email": "dana@example.com", "status": "booked"},
        headers=AUTH,
    )
    assert resp.status == 200
    assert (await resp.json())["updated"] is True
    assert client.store.rows[lead_id]["email"] == "dana@example.com"
    assert client.store.rows[lead_id]["status"] == "booked"


async def test_post_lead_update_missing_returns_404(client):
    resp = await client.post("/leads", json={"id": 9999, "name": "x"}, headers=AUTH)
    assert resp.status == 404


async def test_post_lead_creates_standalone(client):
    resp = await client.post(
        "/leads", json={"name": "Webform Lead", "email": "w@example.com"}, headers=AUTH
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["created"] is True
    assert client.store.rows[body["id"]]["name"] == "Webform Lead"


async def test_get_leads_lists_and_filters(client):
    await client.post("/calls", json={"caller_number": "+1"}, headers=AUTH)
    booked = await (await client.post("/calls", json={"caller_number": "+2"}, headers=AUTH)).json()
    await client.post("/leads", json={"id": booked["id"], "status": "booked"}, headers=AUTH)

    all_resp = await client.get("/leads", headers=AUTH)
    assert all_resp.status == 200
    assert (await all_resp.json())["count"] == 2

    filtered = await client.get("/leads?status=booked", headers=AUTH)
    data = await filtered.json()
    assert data["count"] == 1
    assert data["leads"][0]["status"] == "booked"


async def test_invalid_json_returns_400(client):
    resp = await client.post("/calls", data="not json", headers=AUTH)
    assert resp.status == 400
