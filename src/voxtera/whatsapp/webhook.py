"""Inbound WhatsApp webhook — verification, signature checks, and routing.

aiohttp routes (mounted under ``/whatsapp`` by ``create_app``):

  GET  /whatsapp/webhook   — Meta verification handshake (echo hub.challenge)
  POST /whatsapp/webhook   — incoming message events (X-Hub signature checked)

Flow for a text message:
    verify signature -> parse message -> ConciergePipeline.run(session_id=wa_id)
    -> send the rendered answer back via WhatsAppClient.

The sender's ``wa_id`` (their WhatsApp number) is used as the concierge
``session_id``, which gives per-contact conversation memory and region
persistence for free via the existing Redis-backed SessionStore.

Meta retries a webhook if it isn't answered with 200 quickly, so the POST
handler ACKs immediately and processes the turn in a background task. Inbound
``message.id`` values are de-duplicated so retries don't double-answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections import OrderedDict
from typing import Any

import aiohttp
from aiohttp import web
from loguru import logger

from voxtera.whatsapp.client import WhatsAppClient
from voxtera.whatsapp.config import WhatsAppSettings, load_whatsapp_settings

# App keys for objects stashed on the aiohttp Application.
KEY_SETTINGS = "wa_settings"
KEY_HTTP = "wa_http_session"
KEY_CLIENT = "wa_client"
KEY_DEPS = "wa_concierge_deps"
KEY_SEEN = "wa_seen_message_ids"
KEY_CALL_CLIENT = "wa_call_client"  # Pipecat WhatsAppClient for voice calls

# Bound the de-dupe cache so a long-running server doesn't grow unbounded.
_SEEN_LIMIT = 2000


# --------------------------------------------------------------------------- #
# Signature verification                                                       #
# --------------------------------------------------------------------------- #
def verify_signature(*, app_secret: str, payload: bytes, header: str | None) -> bool:
    """Validate Meta's ``X-Hub-Signature-256`` header against the raw body.

    The header is ``sha256=<hex hmac>`` computed over the exact request bytes
    using the app secret. Returns False on any malformed/absent header.
    """
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
    provided = header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


# --------------------------------------------------------------------------- #
# Payload parsing                                                              #
# --------------------------------------------------------------------------- #
def is_calls_event(body: dict[str, Any]) -> bool:
    """True if this webhook payload carries a voice-call event (field == 'calls')."""
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            if change.get("field") == "calls":
                return True
    return False


def extract_text_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    """Pull text messages out of a webhook payload.

    Returns a list of ``{"from": wa_id, "id": msg_id, "text": body}`` dicts.
    Non-text messages (images, statuses, reactions) are ignored for v1.
    """
    out: list[dict[str, str]] = []
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for msg in value.get("messages", []) or []:
                if msg.get("type") != "text":
                    continue
                text = (msg.get("text") or {}).get("body", "").strip()
                wa_id = msg.get("from", "")
                msg_id = msg.get("id", "")
                if text and wa_id:
                    out.append({"from": wa_id, "id": msg_id, "text": text})
    return out


# --------------------------------------------------------------------------- #
# Concierge wiring (mirrors demo-hotel/serve.py /api/concierge)                #
# --------------------------------------------------------------------------- #
async def _build_concierge_deps(http: aiohttp.ClientSession) -> dict[str, Any]:
    """Heavy shared deps for the concierge, created once and reused."""
    import os

    from voxtera.call_center.classifier import EscalationClassifier
    from voxtera.call_center.concierge import (
        _build_anthropic_converse,
        _build_anthropic_render,
        _build_anthropic_web_query,
        _build_anthropic_web_synth,
    )
    from voxtera.call_center.decompose import QueryDecomposer
    from voxtera.call_center.session import SessionStore

    model = os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001")
    return {
        "http": http,
        "store": SessionStore(),
        "classifier": EscalationClassifier(),
        "decomposer": QueryDecomposer(),
        "render_fn": _build_anthropic_render(model),
        "web_synth_fn": _build_anthropic_web_synth(model),
        "converse_fn": _build_anthropic_converse(model),
        "web_query_fn": _build_anthropic_web_query(model),
    }


async def run_concierge(
    *, deps: dict[str, Any], utterance: str, session_id: str, region: str | None
) -> dict[str, Any]:
    """Build a per-request pipeline around the shared deps and run one turn."""
    from voxtera.call_center.compound import CompoundAndDiscovery
    from voxtera.call_center.pipeline import ConciergePipeline
    from voxtera.call_center.resolver import HotelResolver
    from voxtera.call_center.router import SourceRouter
    from voxtera.call_center.triage import Triage
    from voxtera.call_center.web_retriever import WebRetriever

    pipeline = ConciergePipeline(
        session_store=deps["store"],
        classifier=deps["classifier"],
        decomposer=deps["decomposer"],
        triage=Triage(),
        router=SourceRouter(),
        compound=CompoundAndDiscovery(session=deps["http"]),
        resolver=HotelResolver(session=deps["http"]),
        web_retriever=WebRetriever(),
        render_fn=deps["render_fn"],
        web_synth_fn=deps["web_synth_fn"],
        converse_fn=deps["converse_fn"],
        web_query_fn=deps["web_query_fn"],
    )
    return await pipeline.run(utterance=utterance, session_id=session_id, region=region)


async def _process_message(app: web.Application, msg: dict[str, str]) -> None:
    """Run the concierge for one inbound message and reply. Best-effort."""
    settings: WhatsAppSettings = app[KEY_SETTINGS]
    client: WhatsAppClient = app[KEY_CLIENT]
    deps: dict[str, Any] = app[KEY_DEPS]
    wa_id = msg["from"]

    try:
        await client.mark_read(message_id=msg["id"])
    except Exception as e:  # noqa: BLE001 — read receipt is non-critical
        logger.debug("mark_read failed for {}: {}", msg["id"], e)

    try:
        result = await run_concierge(
            deps=deps,
            utterance=msg["text"],
            session_id=wa_id,
            region=settings.default_region,
        )
        answer = (result.get("answer") or "").strip() or (
            "Sorry, I couldn't put together a reply just now."
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Concierge failed for {}: {}", wa_id, e)
        answer = "Sorry — something went wrong on our side. Please try again."

    try:
        await client.send_text(to=wa_id, body=answer)
        logger.info("Replied to {} ({} chars)", wa_id, len(answer))
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to send WhatsApp reply to {}: {}", wa_id, e)


async def _process_call(app: web.Application, body: dict[str, Any]) -> None:
    """Hand a `calls` webhook to Pipecat's WhatsAppClient, which terminates the
    WebRTC media (SDP answer + pre_accept/accept) and runs the voice bot."""
    from pipecat.transports.whatsapp.api import WhatsAppWebhookRequest

    from voxtera.whatsapp.call_bot import run_call_bot

    client = app[KEY_CALL_CLIENT]
    try:
        request = WhatsAppWebhookRequest(**body)
        # Signature already validated by our handler, so the Pipecat client is
        # constructed without a secret and skips its own re-validation.
        await client.handle_webhook_request(request, connection_callback=run_call_bot)
    except Exception as e:  # noqa: BLE001
        logger.exception("WhatsApp call handling failed: {}", e)


def _already_seen(app: web.Application, message_id: str) -> bool:
    """Return True if this message id was processed before; record it if not."""
    seen: OrderedDict[str, None] = app[KEY_SEEN]
    if message_id in seen:
        return True
    seen[message_id] = None
    while len(seen) > _SEEN_LIMIT:
        seen.popitem(last=False)
    return False


# --------------------------------------------------------------------------- #
# Route handlers                                                               #
# --------------------------------------------------------------------------- #
async def handle_verify(request: web.Request) -> web.Response:
    """GET handshake: echo hub.challenge when the verify token matches."""
    settings: WhatsAppSettings = request.app[KEY_SETTINGS]
    params = request.query
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == settings.verify_token:
        logger.info("WhatsApp webhook verified")
        return web.Response(text=challenge)
    logger.warning("WhatsApp webhook verification failed (mode={}, token mismatch)", mode)
    return web.Response(status=403, text="verification failed")


async def handle_webhook(request: web.Request) -> web.Response:
    """POST handler: verify signature, ACK fast, process in background."""
    settings: WhatsAppSettings = request.app[KEY_SETTINGS]
    raw = await request.read()

    if not verify_signature(
        app_secret=settings.app_secret,
        payload=raw,
        header=request.headers.get("X-Hub-Signature-256"),
    ):
        logger.warning("Rejected WhatsApp webhook: bad signature")
        return web.Response(status=401, text="invalid signature")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=400, text="invalid json")

    # Voice call events go to the Pipecat WhatsApp client (WebRTC); text
    # messages go to the concierge. ACK fast in both cases (Meta retries on
    # slow responses), so the actual work runs as a background task.
    if is_calls_event(body):
        asyncio.create_task(_process_call(request.app, body))
    else:
        for msg in extract_text_messages(body):
            if _already_seen(request.app, msg["id"]):
                logger.debug("Skipping duplicate message {}", msg["id"])
                continue
            asyncio.create_task(_process_message(request.app, msg))

    return web.Response(text="EVENT_RECEIVED")


# --------------------------------------------------------------------------- #
# App factory                                                                  #
# --------------------------------------------------------------------------- #
async def _on_startup(app: web.Application) -> None:
    from pipecat.transports.whatsapp.client import WhatsAppClient as PipecatWhatsAppClient

    http = aiohttp.ClientSession()
    settings: WhatsAppSettings = app[KEY_SETTINGS]
    app[KEY_HTTP] = http
    app[KEY_CLIENT] = WhatsAppClient(settings=settings, session=http)
    app[KEY_DEPS] = await _build_concierge_deps(http)
    app[KEY_SEEN] = OrderedDict()
    # Pipecat WhatsApp client handles the WebRTC call media + Calls API.
    # whatsapp_secret omitted: our handler validates the signature before dispatch.
    app[KEY_CALL_CLIENT] = PipecatWhatsAppClient(
        whatsapp_token=settings.access_token,
        phone_number_id=settings.phone_number_id,
        session=http,
    )


async def _on_cleanup(app: web.Application) -> None:
    http: aiohttp.ClientSession = app[KEY_HTTP]
    await http.close()


def register_whatsapp_routes(app: web.Application, *, settings: WhatsAppSettings) -> None:
    """Attach WhatsApp routes + lifecycle hooks to an existing aiohttp app."""
    app[KEY_SETTINGS] = settings
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/whatsapp/webhook", handle_verify)
    app.router.add_post("/whatsapp/webhook", handle_webhook)


def create_app(settings: WhatsAppSettings | None = None) -> web.Application:
    """Create a standalone aiohttp app serving only the WhatsApp webhook."""
    app = web.Application()
    register_whatsapp_routes(app, settings=settings or load_whatsapp_settings())
    return app
