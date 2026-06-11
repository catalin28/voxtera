"""Tests for the standalone concierge service (P0.2 Phase A).

Exercises the aiohttp app with a stubbed pipeline + deps, so no Redis,
Anthropic, ES/Qdrant, or embedding model is needed. The WhatsApp mount
(``with_whatsapp=True``) needs pipecat's WebRTC stack and is validated on the
droplet; here we run the app with ``with_whatsapp=False``.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from voxtera import concierge_service


class _FakeStore:
    async def load(self, _sid: str) -> dict:
        return {}


class _FakePipeline:
    """Stands in for ConciergePipeline.run — echoes its inputs back."""

    def __init__(self, *, render_fn=None, fail: bool = False):
        self._render_fn = render_fn
        self._fail = fail

    async def run(
        self,
        *,
        utterance: str,
        session_id: str | None,
        region: str | None,
        brief: bool = False,
        hotel_id: str | None = None,
    ) -> dict[str, Any]:
        if self._fail:
            raise RuntimeError("boom")
        answer = "fake answer"
        if self._render_fn is not None:
            answer = await self._render_fn({"utterance": utterance})
        return {
            "session_id": session_id or "new-session",
            "answer": answer,
            "region": region,
            "brief": brief,
            "hotel_id": hotel_id,
            "path": "fake",
        }


@pytest.fixture
def stub_concierge(monkeypatch):
    """Stub the heavy deps + pipeline wiring the service imports lazily."""
    from voxtera.call_center import deps as deps_mod

    fake_deps = {"store": _FakeStore(), "http": None}

    async def fake_build_deps(http=None):  # noqa: ANN001
        return fake_deps

    state: dict[str, Any] = {"fail": False}

    def fake_build_pipeline(deps, *, render_fn=None, decomposer=None, render_delta=None):  # noqa: ANN001
        assert deps is fake_deps  # the request used the app's warm deps
        state["decomposer"] = decomposer
        return _FakePipeline(render_fn=render_fn, fail=state["fail"])

    monkeypatch.setattr(deps_mod, "build_concierge_deps", fake_build_deps)
    monkeypatch.setattr(deps_mod, "build_pipeline", fake_build_pipeline)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # The stream endpoint builds a real Anthropic streaming closure; replace
    # it with a two-chunk fake so the NDJSON protocol is exercised.
    from voxtera.call_center import concierge as concierge_mod

    def fake_render_stream(_model: str):
        async def stream(_payload: dict):
            yield "Hello "
            yield "world"

        return stream

    monkeypatch.setattr(concierge_mod, "_build_anthropic_render_stream", fake_render_stream)
    return state


@pytest.fixture
async def client(stub_concierge):
    app = concierge_service.create_app(with_whatsapp=False, warmup=False)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def test_health(client) -> None:
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["service"] == "concierge"


async def test_concierge_happy_path(client) -> None:
    resp = await client.post(
        "/api/concierge",
        json={"utterance": "best hotel in Bodrum?", "region": "Bodrum", "session_id": "s1"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["session_id"] == "s1"
    assert body["region"] == "Bodrum"
    assert body["answer"] == "fake answer"
    assert body["hotel_id"] is None  # no property scope unless requested


async def test_concierge_forwards_hotel_scope(client) -> None:
    resp = await client.post(
        "/api/concierge",
        json={"utterance": "what time is breakfast?", "hotel_id": "demo"},
    )
    assert resp.status == 200
    assert (await resp.json())["hotel_id"] == "demo"


async def test_concierge_requires_utterance(client) -> None:
    resp = await client.post("/api/concierge", json={"utterance": "  "})
    assert resp.status == 400
    assert (await resp.json())["error"] == "utterance_required"


async def test_concierge_pipeline_error_is_500(client, stub_concierge) -> None:
    stub_concierge["fail"] = True
    resp = await client.post("/api/concierge", json={"utterance": "hi"})
    assert resp.status == 500
    assert "boom" in (await resp.json())["error"]


async def test_stream_emits_text_then_done(client) -> None:
    import json as _json

    resp = await client.post(
        "/api/concierge/stream", json={"utterance": "hi", "region": "", "brief": True}
    )
    assert resp.status == 200
    lines = [_json.loads(line) for line in (await resp.text()).splitlines() if line]
    kinds = [entry["type"] for entry in lines]
    assert kinds == ["text", "text", "done"]
    assert lines[0]["chunk"] == "Hello "
    # The teeing render returned the joined chunks as the answer.
    assert lines[-1]["result"]["answer"] == "Hello world"
    assert lines[-1]["result"]["brief"] is True


async def test_stream_requires_utterance(client) -> None:
    import json as _json

    resp = await client.post("/api/concierge/stream", json={})
    lines = [_json.loads(line) for line in (await resp.text()).splitlines() if line]
    assert lines == [{"type": "error", "error": "utterance_required"}]


async def test_replay_uses_fixed_decomposer(client, stub_concierge) -> None:
    resp = await client.post(
        "/api/concierge/replay",
        json={"utterance": "hi", "decomposition": {"hotel_mention": "Casa Dell Arte"}},
    )
    assert resp.status == 200
    decomposer = stub_concierge["decomposer"]
    assert decomposer is not None
    assert await decomposer.decompose("x", {}) == {"hotel_mention": "Casa Dell Arte"}


async def test_replay_requires_decomposition(client) -> None:
    resp = await client.post("/api/concierge/replay", json={"utterance": "hi"})
    assert resp.status == 400


async def test_feedback_validation_and_store(client, monkeypatch) -> None:
    from voxtera.call_center import pipeline as pipeline_mod

    stored: list[dict] = []
    monkeypatch.setattr(pipeline_mod, "append_feedback_record", stored.append)

    resp = await client.post("/api/concierge/feedback", json={"rating": "sideways"})
    assert resp.status == 400

    resp = await client.post(
        "/api/concierge/feedback",
        json={"rating": "up", "utterance": "q", "answer": "a", "session_id": "s1"},
    )
    assert resp.status == 200
    assert stored == [
        {"session_id": "s1", "utterance": "q", "answer": "a", "rating": "up", "comment": ""}
    ]


def test_whatsapp_routes_share_deps_key() -> None:
    """register_whatsapp_routes records the host app's deps key for reuse."""
    from aiohttp import web

    from voxtera.whatsapp import webhook as wh
    from voxtera.whatsapp.config import WhatsAppSettings

    app = web.Application()
    settings = WhatsAppSettings(
        access_token="t",
        phone_number_id="p",
        verify_token="v",
        app_secret="s",
        default_region="",
        business_account_id=None,
    )
    wh.register_whatsapp_routes(app, settings=settings, shared_deps_key="host_deps")
    assert app[wh.KEY_SHARED_DEPS] == "host_deps"
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/whatsapp/webhook" in paths
