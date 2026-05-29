#!/usr/bin/env python3
"""Daily PSTN phone number management CLI.

Usage:
    python scripts/phones/manage_phone_numbers.py list-available [--region CA] [--areacode 415] [--contains 777]
    python scripts/phones/manage_phone_numbers.py list-purchased
    python scripts/phones/manage_phone_numbers.py buy <number>
    python scripts/phones/manage_phone_numbers.py buy --random
    python scripts/phones/manage_phone_numbers.py release <id>

Requires DAILY_API_KEY in the environment (loaded from .env automatically).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests

# ---------------------------------------------------------------------------
# Load .env from project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    """Minimal .env loader — no external dependency required."""
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

# ---------------------------------------------------------------------------
# Daily API helpers
# ---------------------------------------------------------------------------
DAILY_BASE_URL = "https://api.daily.co/v1"


def _get_api_key() -> str:
    key = os.environ.get("DAILY_API_KEY", "").strip()
    if not key:
        print("ERROR: DAILY_API_KEY not set. Add it to .env or export it.", file=sys.stderr)
        sys.exit(1)
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_api_key()}",
        "Content-Type": "application/json",
    }


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list_available(args: argparse.Namespace) -> None:
    """Search for phone numbers available to purchase."""
    params: dict[str, str] = {}
    if args.region:
        params["region"] = args.region
    if args.areacode:
        params["areacode"] = args.areacode
    if args.city:
        params["city"] = args.city
    if args.contains:
        params["contains"] = args.contains
    if args.starts_with:
        params["starts_with"] = args.starts_with
    if args.ends_with:
        params["ends_with"] = args.ends_with

    url = f"{DAILY_BASE_URL}/list-available-numbers"
    if params:
        url += "?" + urlencode(params)

    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    total = data.get("total_count", 0)
    numbers = data.get("data", [])

    print(f"\n{'='*60}")
    print(f" Available Phone Numbers ({total} found)")
    print(f"{'='*60}\n")

    if not numbers:
        print("  No numbers found matching your criteria.")
        return

    print(f"  {'#':<4} {'Number':<18} {'Region':<8}")
    print(f"  {'-'*4} {'-'*18} {'-'*8}")
    for i, entry in enumerate(numbers, 1):
        number = entry.get("number", "")
        region = entry.get("region", "")
        # Format: +1 (XXX) XXX-XXXX
        if len(number) == 12 and number.startswith("+1"):
            formatted = f"+1 ({number[2:5]}) {number[5:8]}-{number[8:]}"
        else:
            formatted = number
        print(f"  {i:<4} {formatted:<18} {region:<8}")

    print(f"\n  To buy a number: python {Path(__file__).name} buy {numbers[0]['number']}")


def cmd_list_purchased(args: argparse.Namespace) -> None:
    """List all phone numbers you already own."""
    url = f"{DAILY_BASE_URL}/purchased-phone-numbers"
    resp = requests.get(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    total = data.get("total_count", 0)
    numbers = data.get("data", [])

    print(f"\n{'='*60}")
    print(f" Purchased Phone Numbers ({total} total)")
    print(f"{'='*60}\n")

    if not numbers:
        print("  You haven't purchased any numbers yet.")
        print(f"  Run: python {Path(__file__).name} list-available")
        return

    print(f"  {'#':<4} {'Number':<18} {'ID':<40} {'Name':<15}")
    print(f"  {'-'*4} {'-'*18} {'-'*40} {'-'*15}")
    for i, entry in enumerate(numbers, 1):
        number = entry.get("number", "")
        phone_id = entry.get("id", "")
        name = entry.get("name", "")
        if len(number) == 12 and number.startswith("+1"):
            formatted = f"+1 ({number[2:5]}) {number[5:8]}-{number[8:]}"
        else:
            formatted = number
        print(f"  {i:<4} {formatted:<18} {phone_id:<40} {name:<15}")

    print(f"\n  To release a number: python {Path(__file__).name} release <id>")
    print("  Note: Numbers cannot be released within 14 days of purchase.")


def cmd_buy(args: argparse.Namespace) -> None:
    """Buy a phone number."""
    url = f"{DAILY_BASE_URL}/buy-phone-number"
    body: dict[str, str] = {}

    if args.number and not args.random:
        body["number"] = args.number

    if not args.random and not args.number:
        print("ERROR: Provide a phone number or use --random", file=sys.stderr)
        sys.exit(1)

    # Confirmation prompt
    if args.number:
        print(f"\n  About to purchase: {args.number}")
    else:
        print("\n  About to purchase a random number (California)")

    confirm = input("  Confirm? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("  Cancelled.")
        return

    resp = requests.post(url, headers=_headers(), json=body, timeout=30)
    if resp.status_code != 200:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()
    print(f"\n  SUCCESS! Phone number purchased:")
    print(f"    Number: {result.get('number')}")
    print(f"    ID:     {result.get('id')}")
    print(f"\n  This number can now receive PSTN dial-in calls to your Voxtera bot.")
    print(f"  Configure pinless dial-in in your Daily dashboard or via API.")


def cmd_release(args: argparse.Namespace) -> None:
    """Release (delete) a purchased phone number."""
    phone_id = args.id

    print(f"\n  About to RELEASE phone number with ID: {phone_id}")
    print("  WARNING: This cannot be undone. The number will no longer be yours.")
    print("  Note: Numbers cannot be released within 14 days of purchase.")
    confirm = input("  Confirm? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("  Cancelled.")
        return

    url = f"{DAILY_BASE_URL}/release-phone-number/{phone_id}"
    resp = requests.delete(url, headers=_headers(), timeout=30)
    if resp.status_code != 200:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Phone number released successfully.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Daily PSTN phone numbers for Voxtera",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List available numbers in New York
  python scripts/phones/manage_phone_numbers.py list-available --region NY

  # List available numbers with area code 415
  python scripts/phones/manage_phone_numbers.py list-available --areacode 415

  # Search for numbers containing "777"
  python scripts/phones/manage_phone_numbers.py list-available --contains 777

  # List your purchased numbers
  python scripts/phones/manage_phone_numbers.py list-purchased

  # Buy a specific number
  python scripts/phones/manage_phone_numbers.py buy +12029316372

  # Buy a random California number
  python scripts/phones/manage_phone_numbers.py buy --random

  # Release a number by its ID
  python scripts/phones/manage_phone_numbers.py release 0cb313e1-211f-4be0-833d-8c7305b19902
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # list-available
    p_avail = subparsers.add_parser("list-available", help="Search available numbers to purchase")
    p_avail.add_argument("--region", help="ISO 3166-2 state/province code (e.g. CA, NY, ON)")
    p_avail.add_argument("--areacode", help="Area code to search (e.g. 415, 212)")
    p_avail.add_argument("--city", help="City name (must be used with --region)")
    p_avail.add_argument("--contains", help="3-7 digits that appear somewhere in the number")
    p_avail.add_argument("--starts-with", dest="starts_with", help="3-7 digits the number starts with")
    p_avail.add_argument("--ends-with", dest="ends_with", help="3-7 digits the number ends with")
    p_avail.set_defaults(func=cmd_list_available)

    # list-purchased
    p_owned = subparsers.add_parser("list-purchased", help="List your purchased numbers")
    p_owned.set_defaults(func=cmd_list_purchased)

    # buy
    p_buy = subparsers.add_parser("buy", help="Buy a phone number")
    p_buy.add_argument("number", nargs="?", help="Phone number to buy (e.g. +12029316372)")
    p_buy.add_argument("--random", action="store_true", help="Buy a random California number")
    p_buy.set_defaults(func=cmd_buy)

    # release
    p_release = subparsers.add_parser("release", help="Release a purchased phone number")
    p_release.add_argument("id", help="UUID of the phone number to release")
    p_release.set_defaults(func=cmd_release)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
