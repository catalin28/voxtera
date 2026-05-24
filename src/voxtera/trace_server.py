"""Localhost-only HTTP server for tune commands and knob snapshots.

Bound to ``127.0.0.1:VOXTERA_BOT_PORT`` (port chosen by ``serve.py`` at
spawn time, defaulting to 9091 when running standalone). Serves three
endpoints:

- ``GET /knobs`` — current snapshot of every registered tunable (JSON).
- ``POST /tune`` — apply a live-edit. Body: ``{"knob": "...", "value": ...}``.
- ``GET /health`` — liveness probe; returns ``{"ok": true, "session": ...}``.

There is no auth on this endpoint. The port is bound to loopback only and
``serve.py`` (running as the bot's parent process) is the only intended
caller. Outside callers would need SSH tunneling — at which point you're
already on the box and have bigger access than this endpoint provides.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from aiohttp import web
from loguru import logger

from voxtera.tunables import TunablesRegistry


class TuneServer:
    """Tiny aiohttp server for the bot's tune / knobs endpoints.

    Designed to start and stop alongside the pipeline. The server runs in
    the same event loop as the pipeline so apply callbacks that touch
    pipeline state don't need cross-thread synchronisation.
    """

    def __init__(self, port: int, *, session_id: str | None = None) -> None:
        self._port = port
        self._session_id = session_id
        self._speak_callback: Callable[[str], None] | None = None
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/knobs", self._handle_knobs)
        self._app.router.add_post("/tune", self._handle_tune)
        self._app.router.add_post("/speak", self._handle_speak)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    def register_speak_callback(self, cb: Callable[[str], None]) -> None:
        """Register a callback that will be invoked with text to speak.

        The callback should queue a TTSSpeakFrame into the bot's pipeline task.
        """
        self._speak_callback = cb

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """Begin serving. Returns when the server is bound and accepting."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        # 127.0.0.1 only — never bind to all interfaces.
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=self._port)
        await self._site.start()
        logger.info(
            "[trace-server] listening on 127.0.0.1:{} (session={})",
            self._port,
            self._session_id or "standalone",
        )

    async def stop(self) -> None:
        """Stop the server. Safe to call multiple times."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("[trace-server] stopped")

    async def _handle_speak(self, request: web.Request) -> web.Response:
        """POST /speak — inject text directly into the bot's TTS pipeline.

        Body: ``{"text": "Your session is about to end."}``

        Used by the launcher's watchdog timer to announce session timeout
        before forcibly disconnecting. Requires a speak callback registered
        via :meth:`register_speak_callback`.
        """
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "missing_text"}, status=400)

        if self._speak_callback is None:
            return web.json_response({"ok": False, "error": "no_speak_callback"}, status=503)

        try:
            self._speak_callback(text)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

        logger.info("[trace-server] /speak queued: {!r}", text[:80])
        return web.json_response({"ok": True, "text": text})

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "session_id": self._session_id,
                "port": self._port,
            }
        )

    async def _handle_knobs(self, request: web.Request) -> web.Response:
        reg = TunablesRegistry.instance()
        return web.json_response({"knobs": reg.snapshot()})

    async def _handle_tune(self, request: web.Request) -> web.Response:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return web.json_response({"applied": False, "error": "invalid_json"}, status=400)

        knob = body.get("knob")
        if not isinstance(knob, str):
            return web.json_response({"applied": False, "error": "missing_knob"}, status=400)
        value = body.get("value")

        reg = TunablesRegistry.instance()
        applied, err, old, new = reg.apply(knob, value)
        if applied:
            return web.json_response({"applied": True, "knob": knob, "old": old, "new": new})
        # Map known error codes to HTTP statuses for clearer client handling.
        status = 400
        if err == "unknown_knob":
            status = 404
        elif err == "not_live_tunable":
            status = 409
        return web.json_response(
            {"applied": False, "error": err, "knob": knob, "value": value},
            status=status,
        )


def resolve_port() -> int:
    """Read VOXTERA_BOT_PORT or fall back to 9091.

    serve.py sets this when spawning a bot subprocess so each bot can have
    its own port. Standalone ``make run`` uses the default.
    """
    raw = os.environ.get("VOXTERA_BOT_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("[trace-server] VOXTERA_BOT_PORT={!r} is not an int; using 9091", raw)
    return 9091
