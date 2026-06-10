"""Shared concierge wiring — warm deps + per-request pipeline assembly.

Exactly ONE copy of this wiring existed in three places (serve.py's
``_concierge_deps``, ``whatsapp/webhook.py``'s ``_build_concierge_deps``, and
the replay handler); they drifted by accident. This module is now the single
source of truth, used by the concierge service (and anything else that needs
a ConciergePipeline).

Split rationale:

- :func:`build_concierge_deps` — the HEAVY, shareable singletons: the warm
  aiohttp session (ES/Qdrant), the Redis-backed SessionStore, and the LLM fn
  closures whose Anthropic/OpenAI clients keep TLS/HTTP2 connections alive.
  Create once per process, on the loop that will run the pipeline.
- :func:`build_pipeline` — the CHEAP per-request objects wired around those
  deps, so per-run pipeline state stays isolated across concurrent requests.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp

DEFAULT_LLM_MODEL = "claude-haiku-4-5-20251001"


def llm_model() -> str:
    """The concierge LLM model (env-overridable, one place)."""
    return os.environ.get("LLM_MODEL_OVERRIDE", DEFAULT_LLM_MODEL)


async def build_concierge_deps(http: aiohttp.ClientSession | None = None) -> dict[str, Any]:
    """Heavy shared deps for the concierge, created once per process and reused.

    Args:
        http: An existing warm ``aiohttp.ClientSession`` to share; a new one
            is created when omitted. The caller owns its lifecycle either way
            (close it on shutdown).
    """
    from voxtera.call_center.classifier import EscalationClassifier
    from voxtera.call_center.concierge import (
        _build_anthropic_converse,
        _build_anthropic_render,
        _build_anthropic_web_query,
        _build_anthropic_web_synth,
    )
    from voxtera.call_center.decompose import QueryDecomposer
    from voxtera.call_center.property_kb import PropertyKBRetriever
    from voxtera.call_center.session import SessionStore

    model = llm_model()
    return {
        "http": http or aiohttp.ClientSession(),
        "store": SessionStore(),
        "classifier": EscalationClassifier(),
        "decomposer": QueryDecomposer(),
        "render_fn": _build_anthropic_render(model),
        "web_synth_fn": _build_anthropic_web_synth(model),
        "converse_fn": _build_anthropic_converse(model),
        "web_query_fn": _build_anthropic_web_query(model),
        # P1.4: hotel-scoped requests read the property's own guide (per-hotel
        # SQLite RAG) instead of the Qdrant travel listings. Shared so chunk/
        # result caches stay warm across requests.
        "property_kb": PropertyKBRetriever(),
    }


def build_pipeline(
    deps: dict[str, Any],
    *,
    render_fn: Any | None = None,
    decomposer: Any | None = None,
):
    """Wire a per-request ConciergePipeline around the shared deps.

    Args:
        deps: The dict from :func:`build_concierge_deps`.
        render_fn: Override the render step (the /stream endpoint passes a
            teeing renderer that also pushes deltas to the client).
        decomposer: Override the decomposer (the /replay debug endpoint passes
            a fixed one that returns an operator-edited decomposition).
    """
    from voxtera.call_center.compound import CompoundAndDiscovery
    from voxtera.call_center.pipeline import ConciergePipeline
    from voxtera.call_center.resolver import HotelResolver
    from voxtera.call_center.router import SourceRouter
    from voxtera.call_center.triage import Triage
    from voxtera.call_center.web_retriever import WebRetriever

    return ConciergePipeline(
        session_store=deps["store"],
        classifier=deps["classifier"],
        decomposer=decomposer or deps["decomposer"],
        triage=Triage(),
        router=SourceRouter(),
        compound=CompoundAndDiscovery(session=deps["http"]),
        resolver=HotelResolver(session=deps["http"]),
        web_retriever=WebRetriever(),
        render_fn=render_fn or deps["render_fn"],
        web_synth_fn=deps["web_synth_fn"],
        converse_fn=deps["converse_fn"],
        web_query_fn=deps["web_query_fn"],
        property_kb=deps.get("property_kb"),
    )
