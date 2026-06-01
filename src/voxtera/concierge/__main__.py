"""Entry point for the Leads API service: ``python -m voxtera.concierge``.

Loads ``.env``, builds settings, opens the MySQL pool, and serves the aiohttp
app. The pool is opened on startup and drained on cleanup via the app's
lifecycle hooks so a clean shutdown doesn't leak connections.
"""

from __future__ import annotations

from aiohttp import web
from dotenv import load_dotenv
from loguru import logger

from voxtera.concierge.config import load_concierge_settings
from voxtera.concierge.db import MySQLLeadsStore
from voxtera.concierge.leads_api import create_app


def main() -> None:
    load_dotenv()
    settings = load_concierge_settings()
    store = MySQLLeadsStore(settings)

    async def _on_startup(_app: web.Application) -> None:
        await store.connect()

    async def _on_cleanup(_app: web.Application) -> None:
        await store.close()

    app = create_app(store, token=settings.api_token)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    logger.info("Starting Leads API on {}:{}", settings.api_host, settings.api_port)
    web.run_app(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    main()
