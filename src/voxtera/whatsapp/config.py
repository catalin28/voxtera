"""Environment-based configuration for the WhatsApp channel.

All values are read from ``os.environ`` so the module stays a pure function
over the environment (mirrors ``voxtera.config``). The standalone runner in
``__main__`` is responsible for calling ``load_dotenv()`` before
``load_whatsapp_settings()`` so a developer's local ``.env`` is honoured.

Required to send/receive:
    WHATSAPP_ACCESS_TOKEN          permanent (system-user) token
    WHATSAPP_PHONE_NUMBER_ID       the "from" number's id (not the +E.164)

Required for the inbound webhook:
    WHATSAPP_WEBHOOK_VERIFY_TOKEN  any string; echoed back during handshake
    WHATSAPP_APP_SECRET            app secret, used to validate X-Hub signature

Optional:
    WHATSAPP_BUSINESS_ACCOUNT_ID   WABA id (informational / future use)
    WHATSAPP_GRAPH_VERSION         Graph API version (default: v25.0)
    WHATSAPP_DEFAULT_REGION        seed region for the discovery concierge;
                                   empty/unset means "ask the guest first"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_GRAPH_VERSION = "v25.0"


class WhatsAppConfigError(RuntimeError):
    """Raised when a required WhatsApp environment variable is missing."""


@dataclass(frozen=True)
class WhatsAppSettings:
    """Runtime configuration for the WhatsApp channel.

    Secrets are ``repr=False`` so they never surface in logs or tracebacks.
    """

    access_token: str = field(repr=False)
    phone_number_id: str
    verify_token: str = field(repr=False)
    app_secret: str = field(repr=False)
    business_account_id: str | None = None
    graph_version: str = DEFAULT_GRAPH_VERSION
    default_region: str | None = None

    @property
    def messages_url(self) -> str:
        """Full Graph API endpoint for sending messages from this number."""
        return f"https://graph.facebook.com/{self.graph_version}/{self.phone_number_id}/messages"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WhatsAppConfigError(
            f"Missing required environment variable: {name}. "
            "See .env.example for the WhatsApp channel settings."
        )
    return value


def property_hotel_id() -> str | None:
    """The travel↔hotel demo switch for the WhatsApp channel (P1.4).

    Set ``WHATSAPP_HOTEL_ID`` (or the global ``CONCIERGE_HOTEL_ID``) and both
    WhatsApp text and calls answer as that property's concierge from its own
    guide; unset → travel agent. Read per turn/call so flipping the env only
    needs a service restart.
    """
    return (
        os.environ.get("WHATSAPP_HOTEL_ID") or os.environ.get("CONCIERGE_HOTEL_ID") or ""
    ).strip() or None


def load_whatsapp_settings() -> WhatsAppSettings:
    """Build settings from the current environment.

    Raises ``WhatsAppConfigError`` if a required variable is missing so the
    server fails loudly at startup rather than on the first webhook.
    """
    region = os.environ.get("WHATSAPP_DEFAULT_REGION", "").strip()
    return WhatsAppSettings(
        access_token=_require("WHATSAPP_ACCESS_TOKEN"),
        phone_number_id=_require("WHATSAPP_PHONE_NUMBER_ID"),
        verify_token=_require("WHATSAPP_WEBHOOK_VERIFY_TOKEN"),
        app_secret=_require("WHATSAPP_APP_SECRET"),
        business_account_id=os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip() or None,
        graph_version=os.environ.get("WHATSAPP_GRAPH_VERSION", "").strip() or DEFAULT_GRAPH_VERSION,
        default_region=region or None,
    )
