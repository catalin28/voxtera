"""Outbound WhatsApp Cloud API client.

A minimal async wrapper over the Graph API ``/messages`` endpoint. Only the
pieces the concierge needs today are implemented:

  * ``send_text``     — free-form text reply (valid inside the 24h customer
                        service window opened by an inbound user message).
  * ``mark_read``     — optional read receipt for the triggering message.
  * ``upload_media``  — upload a local file to the Graph API media store and
                        return its reusable ``media_id``. Call once at startup
                        and cache the id; the same id can be sent to many
                        recipients without re-uploading.
  * ``send_image``    — send an image either by ``media_id`` (preferred, no
                        public URL required) or by public ``link``.

Template messages (for business-initiated conversations outside the 24h
window) are intentionally out of scope for v1 and can be added later.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
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

    @property
    def _media_url(self) -> str:
        """Graph API endpoint for uploading media for this phone number."""
        return (
            f"https://graph.facebook.com/{self._settings.graph_version}"
            f"/{self._settings.phone_number_id}/media"
        )

    async def upload_media(self, file_path: str | Path) -> str:
        """Upload a local file to the Graph API media store.

        Returns the ``id`` string that can be passed to ``send_image`` as
        ``media_id``. Meta keeps uploaded media for 30 days; cache the id in
        your application and re-upload only when it expires.

        Raises ``aiohttp.ClientResponseError`` on failure.
        """
        path = Path(file_path)
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = aiohttp.FormData()
        data.add_field("messaging_product", "whatsapp")
        data.add_field("type", mime)
        data.add_field(
            "file",
            open(path, "rb"),  # noqa: WPS515 — aiohttp streams the file
            filename=path.name,
            content_type=mime,
        )
        headers = {"Authorization": f"Bearer {self._settings.access_token}"}
        async with self._session.post(self._media_url, headers=headers, data=data) as resp:
            result: dict[str, Any] = await resp.json()
            if resp.status >= 400:
                logger.error("WhatsApp media upload failed ({}): {}", resp.status, result)
                resp.raise_for_status()
            media_id: str = result["id"]
            logger.info("WhatsApp media uploaded: id={} file={}", media_id, path.name)
            return media_id

    async def send_image(
        self,
        *,
        to: str,
        media_id: str | None = None,
        link: str | None = None,
        caption: str = "",
    ) -> dict[str, Any]:
        """Send an image message to a WhatsApp user.

        Provide exactly one of ``media_id`` (from ``upload_media``) or
        ``link`` (a publicly reachable HTTPS URL). ``media_id`` is strongly
        preferred: it avoids Meta fetching a public URL and works even on
        private servers.

        ``caption`` is optional overlay text shown below the image (max 1024
        chars). Leave empty for a clean, captionless image.
        """
        if not media_id and not link:
            raise ValueError("send_image requires either media_id or link")

        image_payload: dict[str, Any] = {}
        if media_id:
            image_payload["id"] = media_id
        else:
            image_payload["link"] = link
        if caption:
            image_payload["caption"] = caption[:1024]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": image_payload,
        }
        return await self._post(payload)

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
