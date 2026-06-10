"""Voxtera concierge service — the warm ConciergePipeline behind HTTP.

P0.2 of the architecture plan: extracted from ``demo-hotel/serve.py`` (which
hosted it on a bolted-on background loop inside a threaded std-lib HTTP
server) into a small standalone aiohttp app with its own systemd unit and
port. A slow render or webhook flood can no longer degrade voice-call setup,
and a crash here leaves the launcher/admin/static server untouched.

Routes (public URLs unchanged — Caddy routes them here):

  POST /api/concierge           — synchronous JSON Q&A (one pipeline run)
  POST /api/concierge/stream    — same pipeline, NDJSON token streaming
  POST /api/concierge/replay    — DEBUG: run from an operator-edited decomposition
  POST /api/concierge/feedback  — thumbs up/down on an answer
  GET  /health                  — liveness + deployed version
  GET/POST /whatsapp/webhook    — WhatsApp text + voice calls (mounted from
                                  ``voxtera.whatsapp.webhook``; shares this
                                  process's warm deps — previously a SECOND
                                  copy of the concierge stack on :8200)

Run:  ``python -m voxtera.concierge_service``  (port: CONCIERGE_PORT, default 8300)

Consumers:
  - ``TravelAgentBrain`` (Daily/web voice bots + WhatsApp calls) via
    ``VOXTERA_CONCIERGE_URL``
  - the travel-agent chat UI + admin debug drawer (via Caddy, or via
    serve.py's thin dev proxy when running locally without Caddy)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from aiohttp import web
from loguru import logger

# App keys for objects stashed on the aiohttp Application.
KEY_DEPS = "concierge_deps"
KEY_HTTP = "concierge_http_session"

DEFAULT_PORT = 8300


def _parse_request_fields(
    body: dict[str, Any],
) -> tuple[str, str | None, str | None, str | None]:
    """Common request fields: (utterance, region, session_id, hotel_id).

    Preserves empty-string region as an explicit "all regions" signal
    (distinct from None/absent) — the pipeline owns those semantics.
    ``hotel_id`` scopes the request to ONE property (hotel-concierge mode):
    KB retrieval reads that hotel's own guide instead of the travel listings.
    """
    utterance = (body.get("utterance") or "").strip()
    raw_region = body.get("region")
    region = raw_region.strip() if isinstance(raw_region, str) else None
    session_id = (body.get("session_id") or "").strip() or None
    hotel_id = (body.get("hotel_id") or "").strip() or None
    return utterance, region, session_id, hotel_id


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


# --------------------------------------------------------------------------- #
# Handlers                                                                     #
# --------------------------------------------------------------------------- #


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — liveness for the deploy gate and Caddy checks."""
    return web.json_response(
        {
            "ok": True,
            "service": "concierge",
            "version": os.environ.get("VOXTERA_VERSION", "dev"),
        }
    )


async def handle_concierge(request: web.Request) -> web.Response:
    """POST /api/concierge — synchronous JSON Q&A backed by ConciergePipeline.

    Request:  {"utterance": str, "region": str, "session_id": str|None,
               "brief": bool, "hotel_id": str|None}
    Response: full ConciergePipeline.run() dict.
    """
    from voxtera.call_center.deps import build_pipeline

    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid_json"}, status=400)
    utterance, region, session_id, hotel_id = _parse_request_fields(body)
    if not utterance:
        return web.json_response({"error": "utterance_required"}, status=400)

    try:
        pipeline = build_pipeline(request.app[KEY_DEPS])
        result = await pipeline.run(
            utterance=utterance,
            session_id=session_id,
            region=region,
            brief=bool(body.get("brief")),
            hotel_id=hotel_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[concierge] error: {}", exc)
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def handle_concierge_stream(request: web.Request) -> web.StreamResponse:
    """POST /api/concierge/stream — same pipeline, render streamed as NDJSON.

    Response lines, in order:
      {"type": "text",  "chunk": "<delta>"}            # render deltas
      {"type": "done",  "result": {<full run() dict>}}  # for debug + session
      {"type": "error", "error": "..."}

    Only the render is streamed (the one LLM step that writes the answer);
    upstream stages run identically to /api/concierge, so chat and voice stay
    the same pipeline. Native async now — the old serve.py implementation
    bridged a background loop to a blocking HTTP thread through a queue.
    """
    from voxtera.call_center.concierge import _build_anthropic_render_stream
    from voxtera.call_center.deps import build_pipeline, llm_model

    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid_json"}, status=400)
    utterance, region, session_id, hotel_id = _parse_request_fields(body)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "application/x-ndjson",
            "Cache-Control": "no-cache",
        },
    )
    await resp.prepare(request)

    async def push(obj: dict) -> None:
        await resp.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    if not utterance:
        await push({"type": "error", "error": "utterance_required"})
        await resp.write_eof()
        return resp

    render_stream = _build_anthropic_render_stream(llm_model())

    async def _teeing_render(payload: dict) -> str:
        # Stream deltas to the client AND return the joined string so
        # ConciergePipeline.run still gets its answer for the result dict.
        parts: list[str] = []
        async for delta in render_stream(payload):
            parts.append(delta)
            await push({"type": "text", "chunk": delta})
        return "".join(parts).strip()

    try:
        pipeline = build_pipeline(request.app[KEY_DEPS], render_fn=_teeing_render)
        result = await asyncio.wait_for(
            pipeline.run(
                utterance=utterance,
                session_id=session_id,
                region=region,
                brief=bool(body.get("brief")),
                hotel_id=hotel_id,
            ),
            timeout=120,
        )
        await push({"type": "done", "result": result})
    except (ConnectionResetError, asyncio.CancelledError):
        return resp  # client hung up mid-stream
    except TimeoutError:
        await push({"type": "error", "error": "timeout"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("[concierge-stream] error: {}", exc)
        await push({"type": "error", "error": str(exc)})
    await resp.write_eof()
    return resp


async def handle_concierge_replay(request: web.Request) -> web.Response:
    """POST /api/concierge/replay — DEBUG: run from an edited decomposition.

    The decomposition is used VERBATIM (no LLM decompose, no coerce), so the
    operator can isolate decomposer bugs from retrieval bugs in the admin
    debug drawer. Request adds {"decomposition": {...}} to the usual fields;
    response shape matches /api/concierge.
    """
    from voxtera.call_center.deps import build_pipeline

    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid_json"}, status=400)
    utterance, region, session_id, hotel_id = _parse_request_fields(body)
    edited = body.get("decomposition")
    if not utterance or not isinstance(edited, dict):
        return web.json_response({"error": "utterance_and_decomposition_required"}, status=400)

    class _FixedDecomposer:
        """Returns the operator's edited decomposition verbatim."""

        async def decompose(self, _utterance: str, _ctx: dict) -> dict:
            return dict(edited)

    try:
        pipeline = build_pipeline(request.app[KEY_DEPS], decomposer=_FixedDecomposer())
        result = await pipeline.run(
            utterance=utterance, session_id=session_id, region=region, hotel_id=hotel_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[concierge-replay] error: {}", exc)
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def handle_concierge_feedback(request: web.Request) -> web.Response:
    """POST /api/concierge/feedback — store a thumbs up/down rating + comment.

    Appended as a ``{"type": "feedback"}`` NDJSON record to the same daily
    ``travel_agent_consierge-*.jsonl`` log as the dialog records.
    """
    from voxtera.call_center.pipeline import append_feedback_record

    body = await _read_json(request)
    if body is None:
        return web.json_response({"error": "invalid_json"}, status=400)
    rating = (body.get("rating") or "").strip().lower()
    if rating not in ("up", "down"):
        return web.json_response({"error": "rating_must_be_up_or_down"}, status=400)
    append_feedback_record(
        {
            "session_id": (body.get("session_id") or "").strip() or None,
            "utterance": str(body.get("utterance") or "")[:2000],
            "answer": str(body.get("answer") or "")[:4000],
            "rating": rating,
            "comment": str(body.get("comment") or "")[:2000],
        }
    )
    return web.json_response({"ok": True})


# --------------------------------------------------------------------------- #
# App factory                                                                  #
# --------------------------------------------------------------------------- #


async def _on_startup(app: web.Application) -> None:
    import aiohttp

    from voxtera.call_center.deps import build_concierge_deps

    http = aiohttp.ClientSession()
    app[KEY_HTTP] = http
    app[KEY_DEPS] = await build_concierge_deps(http)
    # Warm the expensive bits so the first guest turn doesn't pay them:
    # Redis connection, the Anthropic TLS/HTTP2 connection the decompose/
    # render calls reuse, and the e5 embedding weights (~3s cold).
    try:
        await app[KEY_DEPS]["store"].load("warmup")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[warmup] redis ping failed: {}", exc)
    try:
        if os.environ.get("ANTHROPIC_API_KEY"):
            from voxtera.call_center.clients import anthropic_client
            from voxtera.call_center.deps import llm_model

            await anthropic_client().messages.create(
                model=llm_model(),
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            logger.info("[warmup] anthropic connection warm")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[warmup] anthropic pre-warm failed: {}", exc)

    def _warm_embed() -> None:
        try:
            from voxtera.call_center.embeddings import embed_query

            embed_query("warmup")
            logger.info("[warmup] call_center embed model ready")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[warmup] embed pre-warm failed: {}", exc)

    asyncio.get_running_loop().run_in_executor(None, _warm_embed)


async def _on_cleanup(app: web.Application) -> None:
    await app[KEY_HTTP].close()


def create_app(*, with_whatsapp: bool | None = None) -> web.Application:
    """Build the concierge app; optionally mount the WhatsApp webhook routes.

    Args:
        with_whatsapp: Mount /whatsapp/* when True. Default (None) = auto:
            mount when WhatsApp env credentials are configured, skip (with a
            log line) when they aren't — so dev machines without Meta secrets
            still get the concierge API.
    """
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/concierge", handle_concierge)
    app.router.add_post("/api/concierge/stream", handle_concierge_stream)
    app.router.add_post("/api/concierge/replay", handle_concierge_replay)
    app.router.add_post("/api/concierge/feedback", handle_concierge_feedback)

    if with_whatsapp is not False:
        try:
            from voxtera.whatsapp.config import load_whatsapp_settings
            from voxtera.whatsapp.webhook import register_whatsapp_routes

            settings = load_whatsapp_settings()
        except Exception as exc:  # noqa: BLE001
            if with_whatsapp is True:
                raise
            logger.info("[concierge-service] WhatsApp routes disabled ({})", exc)
        else:
            # share_app_deps: the webhook reuses THIS app's warm concierge
            # deps instead of building a second copy of the stack.
            register_whatsapp_routes(app, settings=settings, shared_deps_key=KEY_DEPS)
            logger.info("[concierge-service] WhatsApp routes mounted (text + calls)")

    return app


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    port = int(os.environ.get("CONCIERGE_PORT", str(DEFAULT_PORT)))
    logger.info("Starting concierge service on http://0.0.0.0:{} ", port)
    web.run_app(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
