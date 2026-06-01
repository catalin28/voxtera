#!/usr/bin/env python3
"""Configure pinless dial-in on Daily for the purchased phone number.

This script calls the Daily domain config API to set up pinless dial-in,
which tells Daily to trigger a webhook on our server whenever someone
calls the purchased phone number.

Usage:
    python scripts/phones/configure_dialin.py setup --webhook-url https://your-server.com/pstn/webhook
    python scripts/phones/configure_dialin.py status
    python scripts/phones/configure_dialin.py remove

Requires DAILY_API_KEY and PSTN_PHONE_NUMBER in the environment (loaded from .env).
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Load .env from project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    """Minimal .env loader."""
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not os.environ.get(key):
            os.environ[key] = value


_load_dotenv()

API_KEY = os.environ.get("DAILY_API_KEY", "")
PHONE_NUMBER = os.environ.get("PSTN_PHONE_NUMBER", "")
EXISTING_HMAC = os.environ.get("PSTN_WEBHOOK_HMAC", "")
DAILY_API = "https://api.daily.co/v1"


def _get_or_create_secret() -> str:
    """Return a stable base64-encoded HMAC secret.

    If ``PSTN_WEBHOOK_HMAC`` is already set in the environment we reuse it so
    re-running setup (e.g. to change the webhook URL) does NOT rotate the
    secret out from under the running server. Otherwise we generate a fresh
    256-bit secret and base64-encode it (Daily requires the secret to be
    base64-encoded).

    This is the fix for the rotation bug: previously setup passed no ``hmac``
    field, so Daily minted a brand-new secret on every call — invalidating the
    value saved in ``.env`` and breaking webhook verification.
    """
    if EXISTING_HMAC:
        return EXISTING_HMAC
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def cmd_status(args: argparse.Namespace) -> None:
    """Show current domain config (pinless_dialin section)."""
    resp = requests.get(f"{DAILY_API}/", headers=_headers(), timeout=15)
    resp.raise_for_status()
    config = resp.json().get("config", {})
    pinless = config.get("pinless_dialin", [])
    if not pinless:
        print("No pinless dial-in configured on this domain.")
        return
    print(f"Pinless dial-in entries ({len(pinless)}):\n")
    for i, entry in enumerate(pinless, 1):
        print(f"  [{i}]")
        print(f"      Phone:       {entry.get('phone_number', '(SIP only)')}")
        print(f"      SIP URI:     {entry.get('sip_uri', 'N/A')}")
        print(f"      Webhook:     {entry.get('room_creation_api', 'N/A')}")
        print(f"      Prefix:      {entry.get('name_prefix', 'N/A')}")
        print(f"      HMAC:        {'***' + entry['hmac'][-8:] if entry.get('hmac') else 'N/A'}")
        print()


def cmd_setup(args: argparse.Namespace) -> None:
    """Configure pinless dial-in for the purchased number."""
    webhook_url = args.webhook_url
    if not webhook_url:
        print("ERROR: --webhook-url is required.", file=sys.stderr)
        sys.exit(1)
    if not PHONE_NUMBER:
        print("ERROR: PSTN_PHONE_NUMBER not set in .env", file=sys.stderr)
        sys.exit(1)

    # Pin our own secret so Daily doesn't rotate it on every reconfigure.
    hmac_secret = _get_or_create_secret()
    reused = bool(EXISTING_HMAC)

    print("Configuring pinless dial-in:")
    print(f"  Phone number: {PHONE_NUMBER}")
    print(f"  Webhook URL:  {webhook_url}")
    print("  Room prefix:  VCI")
    print(f"  HMAC secret:  {'reused from .env' if reused else 'newly generated'}")
    print()

    payload = {
        "properties": {
            "pinless_dialin": [
                {
                    "phone_number": PHONE_NUMBER,
                    "room_creation_api": webhook_url,
                    "name_prefix": "VCI",
                    # Pass our own secret explicitly. Without this, Daily mints
                    # a new HMAC on every setup call (rotation bug).
                    "hmac": hmac_secret,
                }
            ]
        }
    }

    resp = requests.post(
        f"{DAILY_API}/",
        headers=_headers(),
        json=payload,
        timeout=30,
    )

    if resp.status_code >= 400:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()
    config = result.get("config", {})
    pinless = config.get("pinless_dialin", [])

    print("SUCCESS! Pinless dial-in configured.\n")
    if pinless:
        entry = pinless[0]
        print(f"  SIP URI: {entry.get('sip_uri', 'N/A')}")
        print()

    # Daily echoes back the hmac we sent; fall back to the one we generated.
    hmac_val = (pinless[0].get("hmac") if pinless else "") or hmac_secret

    if reused:
        print("HMAC secret unchanged — your existing PSTN_WEBHOOK_HMAC in .env is")
        print("still valid. Nothing to update. Just restart the server.")
        return

    print("IMPORTANT: Save this HMAC secret in your .env as PSTN_WEBHOOK_HMAC")
    print(f"  PSTN_WEBHOOK_HMAC={hmac_val}")
    print()
    # Offer to append to .env
    answer = input("Append PSTN_WEBHOOK_HMAC to .env? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        with open(_ENV_FILE, "a", encoding="utf-8") as f:
            f.write(f"\nPSTN_WEBHOOK_HMAC={hmac_val}\n")
        print("  → Appended to .env")


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove pinless dial-in configuration."""
    print("Removing pinless dial-in config...")
    payload = {"properties": {"pinless_dialin": []}}
    resp = requests.post(
        f"{DAILY_API}/",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)
    print("Done. Pinless dial-in removed.")


def main() -> None:
    if not API_KEY:
        print("ERROR: DAILY_API_KEY not set. Check your .env file.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Configure Daily pinless dial-in")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current pinless dial-in config")

    setup_p = sub.add_parser("setup", help="Configure pinless dial-in")
    setup_p.add_argument(
        "--webhook-url",
        required=True,
        help="Public URL for the webhook (e.g. https://your-server.com/pstn/webhook)",
    )

    sub.add_parser("remove", help="Remove pinless dial-in config")

    args = parser.parse_args()
    if args.command == "status":
        cmd_status(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "remove":
        cmd_remove(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
