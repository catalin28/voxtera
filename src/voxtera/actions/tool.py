"""Shared tool schema builders for Pipecat and OpenAI function calling.

``create_ticket`` is defined once here and can be rendered to:

- Pipecat ``FunctionSchema`` (voice pipeline path)
- OpenAI ``tools`` JSON (HTTP demo path)

This module also supports no-code JSON extensibility for OpenAI tools via
``config/tools/*.json`` with placeholder interpolation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pipecat.adapters.schemas.function_schema import FunctionSchema

from voxtera.actions.hotel_config import HotelConfig

# The function names Claude will call. Keep these stable — changing them
# would require coordinating prompt updates and any logs/dashboards keyed
# on them.
CREATE_TICKET_FUNCTION_NAME: Final[str] = "create_ticket"
WEB_SEARCH_FUNCTION_NAME: Final[str] = "web_search"
FIND_VIDEOS_FUNCTION_NAME: Final[str] = "find_hotel_videos"
FIND_REVIEWS_FUNCTION_NAME: Final[str] = "find_hotel_reviews"
_DEFAULT_TOOLS_DIR: Final[Path] = Path(__file__).resolve().parents[3] / "config" / "tools"


def _build_create_ticket_spec(hotel_config: HotelConfig) -> dict:
    """Build neutral JSON-schema spec for ``create_ticket`` from template JSON."""
    tool = _load_tool_from_json_template(
        hotel_config=hotel_config,
        file_path=_DEFAULT_TOOLS_DIR / "create_ticket.json",
    )
    function = tool.get("function") or {}
    if function.get("name") != CREATE_TICKET_FUNCTION_NAME:
        raise RuntimeError(
            "config/tools/create_ticket.json must define function.name='create_ticket'"
        )
    return function


def _to_pipecat_schema(spec: dict) -> FunctionSchema:
    """Convert a neutral spec to a Pipecat ``FunctionSchema``."""
    params = spec.get("parameters") or {}
    return FunctionSchema(
        name=spec["name"],
        description=spec.get("description", ""),
        properties=params.get("properties", {}),
        required=params.get("required", []),
    )


def _to_openai_tool(spec: dict) -> dict:
    """Convert a neutral spec to OpenAI ``tools`` item format."""
    return {"type": "function", "function": spec}


def _load_tool_from_json_template(*, hotel_config: HotelConfig, file_path: Path) -> dict:
    """Load one OpenAI tool JSON file and interpolate hotel placeholders."""
    allowed = [c.value for c in hotel_config.allowed_categories]
    raw = file_path.read_text(encoding="utf-8")
    raw = raw.replace('"$allowed_categories"', json.dumps(allowed))
    raw = raw.replace("$official_language", hotel_config.official_language)
    return json.loads(raw)


def _load_openai_tools_from_json(hotel_config: HotelConfig, tools_dir: Path) -> list[dict]:
    """Load OpenAI tool definitions from ``*.json`` with placeholder expansion."""
    tools: list[dict] = []
    for fp in sorted(tools_dir.glob("*.json")):
        tools.append(_load_tool_from_json_template(hotel_config=hotel_config, file_path=fp))
    return tools


def _build_builtin_tool_specs(hotel_config: HotelConfig) -> dict[str, dict]:
    """Build built-in neutral tool specs keyed by function name."""
    builders: dict[str, callable] = {
        CREATE_TICKET_FUNCTION_NAME: _build_create_ticket_spec,
    }
    specs: dict[str, dict] = {}
    for name, builder in builders.items():
        spec = builder(hotel_config)
        if isinstance(spec, dict):
            specs[name] = spec
    return specs


def build_openai_tools(hotel_config: HotelConfig, tools_dir: Path | None = None) -> list[dict]:
    """Build OpenAI ``tools`` from one source with optional JSON overrides.

    Built-ins come from this module. Files in ``config/tools/*.json`` are then
    merged by function name and override built-ins with the same name.
    """
    merged: dict[str, dict] = {
        name: _to_openai_tool(spec)
        for name, spec in _build_builtin_tool_specs(hotel_config).items()
    }

    directory = tools_dir or _DEFAULT_TOOLS_DIR
    if directory.exists():
        for tool in _load_openai_tools_from_json(hotel_config, directory):
            fn = tool.get("function") or {}
            name = fn.get("name")
            if isinstance(name, str) and name:
                merged[name] = tool

    return [merged[name] for name in sorted(merged)]


def build_create_ticket_tool(hotel_config: HotelConfig) -> FunctionSchema:
    """Build the ``create_ticket`` FunctionSchema for a given hotel.

    The returned schema's ``category`` parameter is constrained to the
    hotel's allowed categories. Other fields are required so Claude cannot
    file an underspecified ticket — the system prompt instructs Claude to
    gather missing info from the guest before calling the tool.
    """
    return _to_pipecat_schema(_build_create_ticket_spec(hotel_config))


def _build_web_search_spec() -> dict:
    """Build neutral JSON-schema spec for ``web_search`` from template JSON."""
    file_path = _DEFAULT_TOOLS_DIR / "web_search.json"
    raw = file_path.read_text(encoding="utf-8")
    tool = json.loads(raw)
    function = tool.get("function") or {}
    if function.get("name") != WEB_SEARCH_FUNCTION_NAME:
        raise RuntimeError(
            "config/tools/web_search.json must define function.name='web_search'"
        )
    return function


def build_web_search_tool() -> FunctionSchema:
    """Build the ``web_search`` FunctionSchema.

    Unlike ``create_ticket``, the web_search tool has no hotel-specific
    parameters — the schema is static.
    """
    return _to_pipecat_schema(_build_web_search_spec())


def _build_static_spec(file_name: str, expected_name: str) -> dict:
    """Load a static (no placeholders) tool spec from ``config/tools/<file_name>``."""
    file_path = _DEFAULT_TOOLS_DIR / file_name
    raw = file_path.read_text(encoding="utf-8")
    tool = json.loads(raw)
    function = tool.get("function") or {}
    if function.get("name") != expected_name:
        raise RuntimeError(
            f"config/tools/{file_name} must define function.name={expected_name!r}"
        )
    return function


def build_find_videos_tool() -> FunctionSchema:
    """Build the ``find_hotel_videos`` FunctionSchema (static, no hotel-specific fields)."""
    return _to_pipecat_schema(
        _build_static_spec("find_hotel_videos.json", FIND_VIDEOS_FUNCTION_NAME)
    )


def build_find_reviews_tool() -> FunctionSchema:
    """Build the ``find_hotel_reviews`` FunctionSchema (static, no hotel-specific fields)."""
    return _to_pipecat_schema(
        _build_static_spec("find_hotel_reviews.json", FIND_REVIEWS_FUNCTION_NAME)
    )
