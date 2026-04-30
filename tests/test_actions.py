"""Unit tests for the action layer (Phase 1–3 modules).

These tests cover:
- Ticket / Category construction and defaults.
- Hotel config loading: happy path, missing fields, unknown categories.
- TelegramSink message formatting and behaviour on bad input.
- create_ticket tool schema correctness.
- Handler argument parsing and outcome reporting.
- wire_actions integration with LLMContext and an LLM service.

No live network calls. Sinks are replaced with FakeSink so we can assert on
delivered tickets. No live Pipecat pipeline — we exercise only the public
API of the LLM service and LLMContext.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from voxtera.actions.handler import make_create_ticket_handler
from voxtera.actions.hotel_config import HotelConfig, load_hotel_config
from voxtera.actions.integration import wire_actions
from voxtera.actions.prompt import (
    build_actions_prompt_fragment,
    compose_system_prompt,
)
from voxtera.actions.sink import TicketSink
from voxtera.actions.telegram_sink import TelegramSink
from voxtera.actions.ticket import Category, Ticket
from voxtera.actions.tool import (
    CREATE_TICKET_FUNCTION_NAME,
    build_create_ticket_tool,
)

# ---- helpers -----------------------------------------------------------


@dataclass
class FakeSink(TicketSink):
    """In-memory sink used by handler / integration tests."""

    delivered: list[Ticket] = field(default_factory=list)
    succeed: bool = True

    async def send(self, ticket: Ticket) -> bool:
        if self.succeed:
            self.delivered.append(ticket)
            return True
        return False


def _demo_hotel_config(
    *,
    allowed: tuple[Category, ...] | None = None,
    addendum: str | None = "Be friendly and concise.",
) -> HotelConfig:
    return HotelConfig(
        hotel_id="demo",
        hotel_name="Test Hotel",
        official_language="en",
        telegram_channel_id="-1009999999999",
        allowed_categories=allowed or (Category.MAINTENANCE, Category.RESERVATION, Category.OTHER),
        system_prompt_addendum=addendum,
    )


class FakeResultCallback:
    """Captures the result Pipecat would otherwise feed back to the LLM."""

    def __init__(self) -> None:
        self.results: list[Any] = []

    async def __call__(self, result: Any, *, properties: Any = None) -> None:
        self.results.append(result)


@dataclass
class FakeFunctionCallParams:
    """Minimal stand-in for Pipecat's FunctionCallParams.

    The handler only reads ``arguments`` and ``result_callback`` from this
    object, so the other Pipecat-specific fields are omitted intentionally.
    """

    arguments: dict[str, Any]
    result_callback: FakeResultCallback


# ---- Ticket / Category -------------------------------------------------


def test_ticket_defaults_session_id_and_timestamp():
    t = Ticket(
        category=Category.MAINTENANCE,
        summary="AC broken",
        room_number="412",
        original_quote="Le climatiseur est cassé.",
        language_detected="French",
    )
    assert t.session_id.startswith("vox-")
    assert "-" in t.session_id
    assert t.timestamp is not None


def test_category_round_trip_via_value():
    assert Category("Maintenance") is Category.MAINTENANCE
    assert Category("Lost & Found") is Category.LOST_AND_FOUND


# ---- HotelConfig loader -----------------------------------------------


def _write_yaml(path: Path, body: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def test_load_hotel_config_happy_path(tmp_path: Path):
    cfg_dir = tmp_path / "hotels"
    cfg_dir.mkdir()
    _write_yaml(
        cfg_dir / "demo.yaml",
        {
            "hotel_name": "Test Hotel",
            "official_language": "en",
            "telegram_channel_id": "-100123",
            "allowed_categories": ["Maintenance", "Other"],
            "system_prompt_addendum": "Be brief.",
        },
    )
    cfg = load_hotel_config("demo", config_dir=cfg_dir)
    assert cfg.hotel_id == "demo"
    assert cfg.hotel_name == "Test Hotel"
    assert cfg.official_language == "en"
    assert cfg.telegram_channel_id == "-100123"
    assert cfg.allowed_categories == (Category.MAINTENANCE, Category.OTHER)
    assert cfg.system_prompt_addendum == "Be brief."


def test_load_hotel_config_missing_field(tmp_path: Path):
    cfg_dir = tmp_path / "hotels"
    cfg_dir.mkdir()
    _write_yaml(
        cfg_dir / "broken.yaml",
        {
            "hotel_name": "X",
            "official_language": "en",
            "telegram_channel_id": "-100",
            # `allowed_categories` missing on purpose
        },
    )
    with pytest.raises(ValueError, match="missing required fields"):
        load_hotel_config("broken", config_dir=cfg_dir)


def test_load_hotel_config_unknown_category(tmp_path: Path):
    cfg_dir = tmp_path / "hotels"
    cfg_dir.mkdir()
    _write_yaml(
        cfg_dir / "weird.yaml",
        {
            "hotel_name": "X",
            "official_language": "en",
            "telegram_channel_id": "-100",
            "allowed_categories": ["Maintenance", "FooBar"],
        },
    )
    with pytest.raises(ValueError, match="unknown category"):
        load_hotel_config("weird", config_dir=cfg_dir)


def test_load_hotel_config_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_hotel_config("ghost", config_dir=tmp_path)


# ---- TelegramSink ------------------------------------------------------


def test_telegram_sink_rejects_empty_token():
    with pytest.raises(ValueError):
        TelegramSink(bot_token="", channel_id="-100")


def test_telegram_sink_rejects_empty_channel():
    with pytest.raises(ValueError):
        TelegramSink(bot_token="abc", channel_id="")


def test_telegram_sink_format_message_contains_required_lines():
    sink = TelegramSink(bot_token="t", channel_id="-100")
    t = Ticket(
        category=Category.MAINTENANCE,
        summary="AC not cooling.",
        room_number="412",
        original_quote="Le climatiseur ne refroidit pas.",
        language_detected="French",
    )
    body = sink._format_message(t)  # noqa: SLF001 — testing the formatter
    assert "[Maintenance]" in body
    assert "Room 412" in body
    assert "AC not cooling." in body
    assert "French" in body
    assert "Le climatiseur ne refroidit pas." in body
    assert "Session: " in body
    assert t.session_id in body


# ---- Tool schema -------------------------------------------------------


def test_create_ticket_tool_categories_match_hotel():
    cfg = _demo_hotel_config(
        allowed=(Category.MAINTENANCE, Category.RESERVATION),
    )
    schema = build_create_ticket_tool(cfg)
    assert schema.name == CREATE_TICKET_FUNCTION_NAME
    assert schema.required == [
        "category",
        "summary",
        "room_number",
        "original_quote",
        "language_detected",
    ]
    enum_vals = schema.properties["category"]["enum"]
    assert enum_vals == ["Maintenance", "Reservation"]


def test_create_ticket_tool_default_dict_shape():
    cfg = _demo_hotel_config()
    schema = build_create_ticket_tool(cfg)
    d = schema.to_default_dict()
    assert d["name"] == CREATE_TICKET_FUNCTION_NAME
    assert d["parameters"]["type"] == "object"
    assert set(d["parameters"]["required"]) == {
        "category",
        "summary",
        "room_number",
        "original_quote",
        "language_detected",
    }


# ---- Handler -----------------------------------------------------------


def _good_args() -> dict[str, Any]:
    return {
        "category": "Maintenance",
        "summary": "AC not cooling in room 412.",
        "room_number": "412",
        "original_quote": "Le climatiseur ne refroidit pas.",
        "language_detected": "French",
    }


def test_handler_success_calls_sink_and_reports_filed():
    cfg = _demo_hotel_config()
    sink = FakeSink(succeed=True)
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    params = FakeFunctionCallParams(arguments=_good_args(), result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert len(sink.delivered) == 1
    assert sink.delivered[0].room_number == "412"
    assert sink.delivered[0].category is Category.MAINTENANCE
    assert len(cb.results) == 1
    assert cb.results[0]["status"] == "filed"
    assert cb.results[0]["category"] == "Maintenance"
    assert "session_id" in cb.results[0]


def test_handler_sink_failure_reports_failed():
    cfg = _demo_hotel_config()
    sink = FakeSink(succeed=False)
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    params = FakeFunctionCallParams(arguments=_good_args(), result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert len(sink.delivered) == 0
    assert cb.results[0]["status"] == "failed"


def test_handler_rejects_disallowed_category():
    # Hotel has only MAINTENANCE; we ask for RESERVATION (a valid Category
    # globally, but not enabled for this hotel).
    cfg = _demo_hotel_config(allowed=(Category.MAINTENANCE,))
    sink = FakeSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    args = _good_args() | {"category": "Reservation"}
    params = FakeFunctionCallParams(arguments=args, result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert sink.delivered == []
    assert cb.results[0]["status"] == "rejected"


def test_handler_rejects_missing_field():
    cfg = _demo_hotel_config()
    sink = FakeSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    args = _good_args()
    del args["room_number"]
    params = FakeFunctionCallParams(arguments=args, result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert sink.delivered == []
    assert cb.results[0]["status"] == "rejected"


def test_handler_rejects_empty_string():
    cfg = _demo_hotel_config()
    sink = FakeSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    args = _good_args() | {"room_number": "  "}
    params = FakeFunctionCallParams(arguments=args, result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert sink.delivered == []
    assert cb.results[0]["status"] == "rejected"


def test_handler_rejects_too_long_string():
    cfg = _demo_hotel_config()
    sink = FakeSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    # original_quote max is 1000; supply 5000 characters
    args = _good_args() | {"original_quote": "x" * 5000}
    params = FakeFunctionCallParams(arguments=args, result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert sink.delivered == []
    assert cb.results[0]["status"] == "rejected"
    assert "exceeds max length" in cb.results[0]["reason"]


def test_handler_rejects_null_byte():
    cfg = _demo_hotel_config()
    sink = FakeSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    args = _good_args() | {"summary": "AC broken\x00"}
    params = FakeFunctionCallParams(arguments=args, result_callback=cb)

    asyncio.run(handler(params))  # type: ignore[arg-type]

    assert sink.delivered == []
    assert cb.results[0]["status"] == "rejected"


def test_handler_survives_sink_raising_unexpectedly():
    """If a future sink violates the contract and raises, voice loop must continue."""

    class ExplodingSink(TicketSink):
        async def send(self, ticket: Ticket) -> bool:
            raise RuntimeError("kaboom")

    cfg = _demo_hotel_config()
    sink = ExplodingSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = FakeResultCallback()
    params = FakeFunctionCallParams(arguments=_good_args(), result_callback=cb)

    # Must not raise.
    asyncio.run(handler(params))  # type: ignore[arg-type]
    assert cb.results[0]["status"] == "failed"


def test_handler_survives_callback_raising():
    """If Pipecat's callback raises, the handler must not propagate."""

    class ExplodingCallback:
        async def __call__(self, result: Any, *, properties: Any = None) -> None:
            raise RuntimeError("queue closed")

    cfg = _demo_hotel_config()
    sink = FakeSink()
    handler = make_create_ticket_handler(sink=sink, hotel_config=cfg)
    cb = ExplodingCallback()
    params = FakeFunctionCallParams(arguments=_good_args(), result_callback=cb)  # type: ignore[arg-type]

    # Must not raise.
    asyncio.run(handler(params))  # type: ignore[arg-type]
    # Sink still got the ticket — callback failure is independent.
    assert len(sink.delivered) == 1


# ---- Prompt fragment ---------------------------------------------------


def test_prompt_fragment_lists_allowed_categories():
    cfg = _demo_hotel_config(
        allowed=(Category.MAINTENANCE, Category.EMERGENCY),
        addendum=None,
    )
    text = build_actions_prompt_fragment(cfg)
    assert "Maintenance" in text
    assert "Emergency" in text
    # Categories not enabled must not appear
    assert "Reservation" not in text
    # Confirmation rule is required content
    assert "confirm" in text.lower()
    # Language split is required content
    assert "summary" in text.lower()
    assert cfg.official_language in text


def test_prompt_fragment_includes_addendum_when_present():
    cfg = _demo_hotel_config(addendum="Custom hotel facts.")
    text = build_actions_prompt_fragment(cfg)
    assert "Custom hotel facts." in text


def test_prompt_fragment_omits_addendum_block_when_none():
    cfg = _demo_hotel_config(addendum=None)
    text = build_actions_prompt_fragment(cfg)
    assert "Hotel-specific facts:" not in text


def test_compose_system_prompt_appends_fragment():
    cfg = _demo_hotel_config()
    base = "BASE PROMPT"
    composed = compose_system_prompt(base, cfg)
    assert composed.startswith("BASE PROMPT")
    assert "ACTION TOOL" in composed


# ---- wire_actions integration -----------------------------------------


class FakeLLMContext:
    """Stand-in for pipecat.processors.aggregators.llm_context.LLMContext.

    Implements only ``tools`` (read) and ``set_tools`` (write), the surface
    wire_actions touches.
    """

    def __init__(self, initial_tools: Any = None) -> None:
        self._tools = initial_tools

    @property
    def tools(self) -> Any:
        return self._tools

    def set_tools(self, tools: Any) -> None:
        self._tools = tools


class FakeLLMService:
    """Stand-in for pipecat.services.llm_service.LLMService.

    Captures register_function calls for assertions.
    """

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_function(self, function_name: str, handler: Any, *_, **__) -> None:
        self.registered[function_name] = handler


def test_wire_actions_registers_function_and_sets_tools():
    cfg = _demo_hotel_config()
    sink = FakeSink()
    llm = FakeLLMService()
    ctx = FakeLLMContext()

    wire_actions(llm=llm, context=ctx, hotel_config=cfg, sink=sink)  # type: ignore[arg-type]

    assert CREATE_TICKET_FUNCTION_NAME in llm.registered
    # The context now carries a ToolsSchema with our function.
    from pipecat.adapters.schemas.tools_schema import ToolsSchema

    assert isinstance(ctx.tools, ToolsSchema)
    names = [t.name for t in ctx.tools.standard_tools]
    assert CREATE_TICKET_FUNCTION_NAME in names


def test_wire_actions_replaces_prior_create_ticket_schema():
    """Calling wire_actions twice should not duplicate the create_ticket schema."""
    cfg = _demo_hotel_config()
    sink = FakeSink()
    llm = FakeLLMService()
    ctx = FakeLLMContext()

    wire_actions(llm=llm, context=ctx, hotel_config=cfg, sink=sink)  # type: ignore[arg-type]
    wire_actions(llm=llm, context=ctx, hotel_config=cfg, sink=sink)  # type: ignore[arg-type]

    matching = [t for t in ctx.tools.standard_tools if t.name == CREATE_TICKET_FUNCTION_NAME]
    assert len(matching) == 1
