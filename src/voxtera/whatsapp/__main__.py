"""Run the WhatsApp webhook server: ``python -m voxtera.whatsapp``.

Loads ``.env`` (so local secrets are honoured) then starts an aiohttp server.
Expose it publicly (e.g. ``ngrok http 8200``) and register the resulting
``https://…/whatsapp/webhook`` URL + verify token in the Meta app dashboard.
"""

from __future__ import annotations

import os

from aiohttp import web
from dotenv import load_dotenv
from loguru import logger

from voxtera.whatsapp.config import load_whatsapp_settings
from voxtera.whatsapp.webhook import create_app

if __name__ == "__main__":
    load_dotenv()
    settings = load_whatsapp_settings()
    port = int(os.environ.get("WHATSAPP_PORT", "8200"))
    logger.info(
        "Starting WhatsApp webhook on http://0.0.0.0:{}/whatsapp/webhook "
        "(phone_number_id={}, default_region={!r})",
        port,
        settings.phone_number_id,
        settings.default_region,
    )
    web.run_app(create_app(settings), host="0.0.0.0", port=port)
