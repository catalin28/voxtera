"""Tests for Daily pinless dial-in webhook HMAC verification."""

from __future__ import annotations

import base64
import time

import pytest

from voxtera.pstn_auth import sign_pinless_payload, verify_pinless_signature

# A stable base64-encoded secret for tests (32 random-ish bytes).
SECRET_B64 = base64.b64encode(b"\x01" * 32).decode()
BODY = b'{"callId":"abc123","callDomain":"d","From":"+15550001111","To":"+12262120379"}'


def _valid_headers(body: bytes = BODY, secret: str = SECRET_B64, ts: str | None = None):
    ts = ts or str(int(time.time()))
    sig = sign_pinless_payload(body, ts, secret)
    return sig, ts


def test_valid_signature_accepted():
    sig, ts = _valid_headers()
    assert verify_pinless_signature(BODY, sig, ts, SECRET_B64) is None


def test_millisecond_timestamp_accepted():
    ts = "1000000000000"
    sig = sign_pinless_payload(BODY, ts, SECRET_B64)
    assert verify_pinless_signature(BODY, sig, ts, SECRET_B64, now=1000000000.1) is None


def test_tampered_body_rejected():
    sig, ts = _valid_headers()
    tampered = BODY + b" "  # one byte changed → different signature
    assert verify_pinless_signature(tampered, sig, ts, SECRET_B64) == "invalid_signature"


def test_wrong_secret_rejected():
    sig, ts = _valid_headers()
    other_secret = base64.b64encode(b"\x02" * 32).decode()
    assert verify_pinless_signature(BODY, sig, ts, other_secret) == "invalid_signature"


def test_stale_timestamp_rejected():
    old_ts = str(int(time.time()) - 10_000)
    sig = sign_pinless_payload(BODY, old_ts, SECRET_B64)
    # Signature itself is valid, but the timestamp is far outside the window.
    assert verify_pinless_signature(BODY, sig, old_ts, SECRET_B64) == "stale_timestamp"


def test_future_timestamp_rejected():
    future_ts = str(int(time.time()) + 10_000)
    sig = sign_pinless_payload(BODY, future_ts, SECRET_B64)
    assert verify_pinless_signature(BODY, sig, future_ts, SECRET_B64) == "stale_timestamp"


def test_stale_millisecond_timestamp_rejected():
    ts = "999990000000"
    sig = sign_pinless_payload(BODY, ts, SECRET_B64)
    assert (
        verify_pinless_signature(BODY, sig, ts, SECRET_B64, now=1000000000.0) == "stale_timestamp"
    )


def test_missing_headers_rejected():
    assert verify_pinless_signature(BODY, "", "", SECRET_B64) == "missing_signature"


def test_no_secret_configured():
    sig, ts = _valid_headers()
    assert verify_pinless_signature(BODY, sig, ts, "") == "hmac_not_configured"


def test_malformed_timestamp_rejected():
    sig, _ = _valid_headers()
    assert verify_pinless_signature(BODY, sig, "not-a-number", SECRET_B64) == "invalid_timestamp"


def test_secret_not_base64():
    sig, ts = _valid_headers()
    # "!" is not valid base64 → misconfig, not a forged request.
    assert verify_pinless_signature(BODY, sig, ts, "!!!not-base64!!!") == "hmac_misconfigured"


def test_now_override_keeps_old_signature_valid_within_window():
    """With an explicit ``now`` close to the signing ts, a valid sig passes."""
    ts = "1000000000"
    sig = sign_pinless_payload(BODY, ts, SECRET_B64)
    assert verify_pinless_signature(BODY, sig, ts, SECRET_B64, now=1000000100) is None


@pytest.mark.parametrize("bad_window", [0, 1])
def test_tight_tolerance_rejects_skew(bad_window: int):
    ts = "1000000000"
    sig = sign_pinless_payload(BODY, ts, SECRET_B64)
    result = verify_pinless_signature(
        BODY, sig, ts, SECRET_B64, now=1000000000 + 100, tolerance_secs=bad_window
    )
    assert result == "stale_timestamp"
