"""Outbound WhatsApp Cloud API client.

A minimal async wrapper over the Graph API ``/messages`` endpoint. Only the
pieces the concierge needs today are implemented:

  * ``send_text``     — free-form text reply (valid inside the 24h customer
                        service window opened by an inbound user message).
  * ``mark_read``     — optional read receipt for the triggering message.

Template messages (for business-initiated conversations outside the 24h
window) are intentionally out of scope for v1 and can be added later.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from loguru import logger

from voxtera.whatsapp.config import WhatsAppSettings

# WhatsApp hard-caps a text message body at 4096 characters.
MAX_BODY_CHARS = 4096


class WhatsAppClient:
    """Sends messages from one business number via the Graph API.

    The ``aiohttp.ClientSession`` is injected so it can be shared with the
    rest of the app (warm connection pool) and swapped for a fake in tests.
    """

    def __init__(self, *, settings: WhatsAppSettings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.access_token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, *, to: str, body: str, preview_url: bool = False) -> dict[str, Any]:
        """Send a plain-text message to a WhatsApp user.

        ``to`` is the recipient's WhatsApp id (digits, E.164 without ``+``),
        which is exactly the ``wa_id`` delivered on the inbound webhook.
        Over-long bodies are truncated to the API limit rather than rejected.
        """
        if len(body) > MAX_BODY_CHARS:
            logger.warning(
                "WhatsApp body {} chars > {} limit; truncating", len(body), MAX_BODY_CHARS
            )
            body = body[: MAX_BODY_CHARS - 1] + "…"

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": preview_url, "body": body},
        }
        return await self._post(payload)

    async def mark_read(self, *, message_id: str) -> dict[str, Any]:
        """Send a read receipt for an inbound message (best-effort)."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        return await self._post(payload)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._session.post(
            self._settings.messages_url, headers=self._headers, json=payload
        ) as resp:
            data: dict[str, Any] = await resp.json()
            if resp.status >= 400:
                logger.error("WhatsApp send failed ({}): {}", resp.status, data)
                resp.raise_for_status()
            return data
