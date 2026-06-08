"""Tests for Phase 4a external sources — feature flags and wiring.

We deliberately do NOT exercise live YouTube / Places HTTP calls in unit
tests: those need real keys, real network, and would be flaky in CI.
Instead, the tests cover the layer the bot actually depends on:

- :mod:`voxtera.external_flags` — the env-driven gating used by every
  ``wire_*`` function.
- :func:`voxtera.actions.integration.wire_find_videos` /
  :func:`voxtera.actions.integration.wire_find_reviews` — verify they
  register on the LLM context when enabled and skip cleanly when not.
- The handlers — verify the empty-input and graceful-failure paths
  produce the expected ``{status, guidance}`` shape, since those are the
  only paths the LLM can reach without making a network call.

Live integration tests are out of scope; if you want to sanity-check
the clients against real APIs, run the scripts in ``scripts/`` with
keys set in your local ``.env``.
"""

from __future__ import annotations

from typing import Any

import pytest

from voxtera import external_flags
from voxtera.actions.find_reviews_handler import make_find_reviews_handler
from voxtera.actions.find_videos_handler import make_find_videos_handler
from voxtera.actions.integration import wire_find_reviews, wire_find_videos, wire_web_search
from voxtera.actions.tool import (
    FIND_REVIEWS_FUNCTION_NAME,
    FIND_VIDEOS_FUNCTION_NAME,
    WEB_SEARCH_FUNCTION_NAME,
)


# ---------- shared fakes (mirror tests/test_actions.py) ----------
class FakeLLMContext:
    def __init__(self, initial_tools: Any = None) -> None:
        self._tools = initial_tools

    @property
    def tools(self) -> Any:
        return self._tools

    def set_tools(self, tools: Any) -> None:
        self._tools = tools


class FakeLLMService:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_function(self, function_name: str, handler: Any, *_, **__) -> None:
        self.registered[function_name] = handler


class FakeCallParams:
    """Minimal stand-in for pipecat.services.llm_service.FunctionCallParams."""

    def __init__(self, arguments: dict) -> None:
        self.arguments = arguments
        self.results: list[dict] = []

    async def result_callback(self, result: dict) -> None:
        self.results.append(result)


# ============ external_flags ============

def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        external_flags.ENV_YOUTUBE_SEARCH,
        external_flags.ENV_PLACES_SEARCH,
        external_flags.ENV_WEB_SEARCH,
        external_flags.ENV_YOUTUBE_API_KEY,
        external_flags.ENV_GOOGLE_PLACES_API_KEY,
        external_flags.ENV_TAVILY_API_KEY,
    ):
        monkeypatch.delenv(name, raising=False)


class TestFlags:
    def test_youtube_disabled_when_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        assert not external_flags.is_youtube_search_enabled()

    def test_youtube_enabled_when_key_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_YOUTUBE_API_KEY, "fake-key")
        assert external_flags.is_youtube_search_enabled()

    def test_youtube_disabled_when_flag_false_even_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_YOUTUBE_API_KEY, "fake-key")
        monkeypatch.setenv(external_flags.ENV_YOUTUBE_SEARCH, "false")
        assert not external_flags.is_youtube_search_enabled()

    @pytest.mark.parametrize("falsey", ["false", "0", "no", "off", "FALSE", "Off"])
    def test_flag_falsey_values(self, monkeypatch: pytest.MonkeyPatch, falsey: str) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_TAVILY_API_KEY, "fake-key")
        monkeypatch.setenv(external_flags.ENV_WEB_SEARCH, falsey)
        assert not external_flags.is_web_search_enabled()

    @pytest.mark.parametrize("truthy", ["true", "1", "yes", "on", "TRUE", "anything-else"])
    def test_flag_truthy_values(self, monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_TAVILY_API_KEY, "fake-key")
        monkeypatch.setenv(external_flags.ENV_WEB_SEARCH, truthy)
        assert external_flags.is_web_search_enabled()

    def test_places_independent_from_youtube(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_GOOGLE_PLACES_API_KEY, "fake-key")
        assert external_flags.is_places_search_enabled()
        assert not external_flags.is_youtube_search_enabled()
        assert not external_flags.is_web_search_enabled()

    def test_disabled_reason_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        # flag explicitly false → reason mentions flag
        monkeypatch.setenv(external_flags.ENV_WEB_SEARCH, "false")
        monkeypatch.setenv(external_flags.ENV_TAVILY_API_KEY, "fake-key")
        reason = external_flags.disabled_reason(
            external_flags.ENV_WEB_SEARCH, external_flags.ENV_TAVILY_API_KEY,
        )
        assert "WEB_SEARCH_ENABLED" in reason and "false" in reason

        # flag default-true but key missing → reason mentions key
        monkeypatch.delenv(external_flags.ENV_WEB_SEARCH, raising=False)
        monkeypatch.delenv(external_flags.ENV_TAVILY_API_KEY, raising=False)
        reason = external_flags.disabled_reason(
            external_flags.ENV_WEB_SEARCH, external_flags.ENV_TAVILY_API_KEY,
        )
        assert "TAVILY_API_KEY" in reason


# ============ wire_* gating ============

class TestWireGating:
    def test_wire_find_videos_skips_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        llm = FakeLLMService()
        ctx = FakeLLMContext()
        wire_find_videos(llm=llm, context=ctx)  # type: ignore[arg-type]
        assert FIND_VIDEOS_FUNCTION_NAME not in llm.registered
        assert ctx.tools is None

    def test_wire_find_videos_wires_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_YOUTUBE_API_KEY, "fake-key")
        llm = FakeLLMService()
        ctx = FakeLLMContext()
        wire_find_videos(llm=llm, context=ctx)  # type: ignore[arg-type]
        assert FIND_VIDEOS_FUNCTION_NAME in llm.registered
        names = [t.name for t in ctx.tools.standard_tools]
        assert FIND_VIDEOS_FUNCTION_NAME in names

    def test_wire_find_reviews_skips_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        llm = FakeLLMService()
        ctx = FakeLLMContext()
        wire_find_reviews(llm=llm, context=ctx)  # type: ignore[arg-type]
        assert FIND_REVIEWS_FUNCTION_NAME not in llm.registered

    def test_wire_find_reviews_wires_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_GOOGLE_PLACES_API_KEY, "fake-key")
        llm = FakeLLMService()
        ctx = FakeLLMContext()
        wire_find_reviews(llm=llm, context=ctx)  # type: ignore[arg-type]
        assert FIND_REVIEWS_FUNCTION_NAME in llm.registered

    def test_wire_web_search_now_gated_by_master_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even with TAVILY_API_KEY set, WEB_SEARCH_ENABLED=false must skip."""
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_TAVILY_API_KEY, "fake-key")
        monkeypatch.setenv(external_flags.ENV_WEB_SEARCH, "false")
        llm = FakeLLMService()
        ctx = FakeLLMContext()
        wire_web_search(llm=llm, context=ctx)  # type: ignore[arg-type]
        assert WEB_SEARCH_FUNCTION_NAME not in llm.registered

    def test_wire_calls_are_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(external_flags.ENV_YOUTUBE_API_KEY, "fake-key")
        llm = FakeLLMService()
        ctx = FakeLLMContext()
        wire_find_videos(llm=llm, context=ctx)  # type: ignore[arg-type]
        wire_find_videos(llm=llm, context=ctx)  # type: ignore[arg-type]
        matching = [t for t in ctx.tools.standard_tools if t.name == FIND_VIDEOS_FUNCTION_NAME]
        assert len(matching) == 1


# ============ handler edge cases ============

class TestHandlerErrorPaths:
    @pytest.mark.asyncio
    async def test_find_videos_empty_hotel_name(self) -> None:
        handler = make_find_videos_handler()
        params = FakeCallParams({})
        await handler(params)  # type: ignore[arg-type]
        assert len(params.results) == 1
        assert params.results[0]["status"] == "error"
        assert "missing" in params.results[0]["guidance"].lower() or "clarify" in params.results[0]["guidance"].lower()

    @pytest.mark.asyncio
    async def test_find_reviews_empty_hotel_name(self) -> None:
        handler = make_find_reviews_handler()
        params = FakeCallParams({})
        await handler(params)  # type: ignore[arg-type]
        assert len(params.results) == 1
        assert params.results[0]["status"] == "error"

    @pytest.mark.asyncio
    async def test_find_videos_handler_returns_unavailable_when_key_missing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No YOUTUBE_API_KEY → underlying client raises YouTubeSearchError →
        # handler returns the graceful unavailable payload.
        _clear_env(monkeypatch)
        handler = make_find_videos_handler()
        params = FakeCallParams({"hotel_name": "Test Hotel"})
        await handler(params)  # type: ignore[arg-type]
        assert len(params.results) == 1
        assert params.results[0]["status"] == "unavailable"
        assert "make up" in params.results[0]["guidance"].lower() or "invent" in params.results[0]["guidance"].lower()

    @pytest.mark.asyncio
    async def test_find_reviews_handler_returns_unavailable_when_key_missing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch)
        handler = make_find_reviews_handler()
        params = FakeCallParams({"hotel_name": "Test Hotel", "region_hint": "Madrid"})
        await handler(params)  # type: ignore[arg-type]
        assert len(params.results) == 1
        assert params.results[0]["status"] == "unavailable"


# ============ schema integrity ============

class TestToolSchemas:
    def test_find_videos_schema_loads(self) -> None:
        from voxtera.actions.tool import build_find_videos_tool
        schema = build_find_videos_tool()
        assert schema.name == FIND_VIDEOS_FUNCTION_NAME
        assert "hotel_name" in schema.required
        assert "intent" in schema.properties

    def test_find_reviews_schema_loads(self) -> None:
        from voxtera.actions.tool import build_find_reviews_tool
        schema = build_find_reviews_tool()
        assert schema.name == FIND_REVIEWS_FUNCTION_NAME
        assert "hotel_name" in schema.required
