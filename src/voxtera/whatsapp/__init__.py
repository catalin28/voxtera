"""WhatsApp Business Cloud API channel for the Voxtera concierge.

A thin inbound/outbound bridge between Meta's WhatsApp Cloud API and the
existing call-center ``ConciergePipeline``:

  * ``config``  — environment-driven WhatsApp settings (token, IDs, secrets).
  * ``client``  — outbound: send messages via the Graph API.
  * ``webhook`` — inbound: verify handshake + receive messages, run the
    concierge keyed by the sender's WhatsApp id, and reply.

Run as a standalone aiohttp server with ``python -m voxtera.whatsapp``.
"""

from __future__ import annotations
