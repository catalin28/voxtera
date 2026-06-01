"""HMAC verification for Daily pinless dial-in webhooks.

Daily secures the ``room_creation_api`` webhook with an HMAC signature so the
receiver can prove a request genuinely came from Daily (and wasn't forged or
replayed). Daily signs the string ``f"{timestamp}.{body}"`` with HMAC-SHA256
using a base64-encoded shared secret, base64-encodes the digest, and sends it
as ``X-Pinless-Signature`` alongside ``X-Pinless-Timestamp``.

Reference:
https://docs.daily.co/guides/products/dial-in-dial-out/dialin-pinless

This module is dependency-free and has no import-time side effects so it can be
unit-tested in isolation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time

# Default replay window: reject timestamps more than this many seconds away
# from now (in either direction).
DEFAULT_TOLERANCE_SECS = 300

# Daily currently sends X-Pinless-Timestamp as Unix milliseconds on real PSTN
# webhook traffic, while docs and local test helpers commonly use seconds.
# Accept both for replay-window validation; signature computation still uses the
# raw header value exactly as received.
_PINLESS_TIMESTAMP_MILLISECONDS_CUTOFF = 10_000_000_000


def _normalize_pinless_timestamp(ts_val: float) -> float:
    """Convert a raw pinless timestamp to epoch seconds for skew checks."""
    if abs(ts_val) >= _PINLESS_TIMESTAMP_MILLISECONDS_CUTOFF:
        return ts_val / 1000.0
    return ts_val


def verify_pinless_signature(
    raw_body: bytes,
    sig_header: str,
    ts_header: str,
    secret_b64: str,
    *,
    now: float | None = None,
    tolerance_secs: int = DEFAULT_TOLERANCE_SECS,
) -> str | None:
    """Verify a Daily pinless dial-in webhook signature.

    Args:
        raw_body: The exact request body bytes Daily POSTed.
        sig_header: Value of the ``X-Pinless-Signature`` header.
        ts_header: Value of the ``X-Pinless-Timestamp`` header.
        secret_b64: The base64-encoded shared HMAC secret (``PSTN_WEBHOOK_HMAC``).
        now: Override for the current epoch time (testing). Defaults to ``time.time()``.
        tolerance_secs: Max allowed clock skew for the timestamp, to block replay.

    Returns:
        ``None`` if the signature is valid, otherwise a short machine-readable
        error code: ``"hmac_not_configured"``, ``"missing_signature"``,
        ``"invalid_timestamp"``, ``"stale_timestamp"``, ``"hmac_misconfigured"``,
        or ``"invalid_signature"``.
    """
    if not secret_b64:
        return "hmac_not_configured"
    if not sig_header or not ts_header:
        return "missing_signature"

    try:
        ts_val = float(ts_header)
    except ValueError:
        return "invalid_timestamp"
    ts_val = _normalize_pinless_timestamp(ts_val)

    current = now if now is not None else time.time()
    if abs(current - ts_val) > tolerance_secs:
        return "stale_timestamp"

    try:
        # validate=True so malformed secrets raise instead of silently
        # dropping non-alphabet chars (which would mask a config error as a
        # signature mismatch).
        secret = base64.b64decode(secret_b64, validate=True)
    except (ValueError, binascii.Error):
        return "hmac_misconfigured"
    if not secret:
        return "hmac_misconfigured"

    sign_payload = ts_header + "." + raw_body.decode("utf-8")
    computed = base64.b64encode(
        hmac.new(secret, sign_payload.encode(), hashlib.sha256).digest()
    ).decode()

    if not hmac.compare_digest(computed, sig_header):
        return "invalid_signature"
    return None


def sign_pinless_payload(raw_body: bytes, ts: str, secret_b64: str) -> str:
    """Produce the signature Daily would send for ``raw_body`` at time ``ts``.

    Exposed so tests (and local webhook simulators) can generate a valid
    ``X-Pinless-Signature`` without duplicating the signing logic.
    """
    secret = base64.b64decode(secret_b64)
    sign_payload = ts + "." + raw_body.decode("utf-8")
    return base64.b64encode(
        hmac.new(secret, sign_payload.encode(), hashlib.sha256).digest()
    ).decode()
