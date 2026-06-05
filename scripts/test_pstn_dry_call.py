#!/usr/bin/env python3
"""PSTN dry-call test — measures bot startup time without a real phone.

Simulates the Daily webhook flow and measures how long it takes from
"call arrives" to "bot is ready to speak". This is the hold time a
real caller would experience.

Usage:
    # Against local serve.py (webhook + room creation only, bot won't start on Windows):
    python scripts/test_pstn_dry_call.py --host http://localhost:8000

    # Against the droplet (full end-to-end, bot actually spawns):
    python scripts/test_pstn_dry_call.py --host http://<DROPLET_IP>:8000

    # Rate-limit test (fire 5 calls rapidly from same number):
    python scripts/test_pstn_dry_call.py --host http://localhost:8000 --burst 5

    # Concurrent limit test:
    python scripts/test_pstn_dry_call.py --host http://localhost:8000 --concurrent 12
"""

import argparse
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _post_webhook(host: str, caller: str = "+15551234567") -> dict:
    """POST a fake PSTN webhook and return (status_code, body, elapsed_ms)."""
    payload = {
        "callId": uuid.uuid4().hex,
        "callDomain": "voxtera.daily.co",
        "From": caller,
        "To": "+12262120379",
    }
    req = Request(
        f"{host}/pstn/webhook",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=30) as resp:  # noqa: S310
            body = json.loads(resp.read())
            elapsed = (time.perf_counter() - t0) * 1000
            return {"status": resp.status, "body": body, "elapsed_ms": elapsed}
    except HTTPError as e:
        raw = e.read() if e.fp else b""
        try:
            body = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            body = {"error": raw.decode(errors="replace")[:200]}
        elapsed = (time.perf_counter() - t0) * 1000
        return {"status": e.code, "body": body, "elapsed_ms": elapsed}
    except URLError as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return {"status": 0, "body": {"error": str(e.reason)}, "elapsed_ms": elapsed}


def _poll_session_ready(host: str, session_id: str, timeout: float = 30.0) -> float | None:
    """Poll session status until bot posts 'ready'. Returns elapsed seconds or None."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            req = Request(f"{host}/api/sessions", method="GET")
            with urlopen(req, timeout=5) as resp:  # noqa: S310
                sessions = json.loads(resp.read())
                for s in sessions:
                    if s.get("session_id") == session_id and s.get("state") == "running":
                        return time.perf_counter() - t0
        except Exception:
            pass
        time.sleep(0.5)
    return None


def test_single_call(host: str, caller: str = "+15551234567") -> None:
    """Simulate one PSTN call and measure timing."""
    print(f"\n{'='*60}")
    print("  PSTN DRY CALL — Single Call Test")
    print(f"  Target: {host}")
    print(f"  Caller: {caller}")
    print(f"{'='*60}\n")

    t_start = time.perf_counter()

    # Step 1: POST webhook
    print("[1/4] Posting webhook (simulating incoming call)...")
    result = _post_webhook(host, caller)
    time.perf_counter() - t_start

    print(f"      Status: {result['status']}")
    print(f"      Response: {result['body']}")
    print(f"      Webhook processing: {result['elapsed_ms']:.0f}ms")

    if result["status"] != 200:
        print(f"\n  FAILED — webhook returned {result['status']}")
        print(f"  Error: {result['body'].get('error', 'unknown')}")
        return

    session_id = result["body"].get("session_id", "")
    room_name = result["body"].get("room_name", "")
    print(f"      Session: {session_id[:12]}...")
    print(f"      Room: {room_name}")

    # Step 2: Wait for bot ready
    print("\n[2/4] Waiting for bot to join room and become ready...")
    ready_elapsed = _poll_session_ready(host, session_id, timeout=30.0)

    if ready_elapsed is not None:
        print(f"      Bot ready in: {ready_elapsed:.1f}s")
    else:
        print("      Bot did NOT become ready within 30s")
        print("      (Expected on Windows — Daily transport unavailable)")

    # Step 3: Summary
    total = time.perf_counter() - t_start
    print("\n[3/4] Timing Summary:")
    print(f"      Webhook processing:  {result['elapsed_ms']:>7.0f}ms")
    if ready_elapsed:
        print(f"      Bot startup:         {ready_elapsed * 1000:>7.0f}ms")
        print("      ─────────────────────────────────")
        print(f"      TOTAL HOLD TIME:     {ready_elapsed:>7.1f}s")
        print("\n      This is what the caller hears as hold music.")
    else:
        print("      Bot startup:         (not measured — bot didn't start)")
        print(f"      Total elapsed:       {total:.1f}s")

    # Step 4: Verdict
    print("\n[4/4] Verdict:")
    if ready_elapsed and ready_elapsed < 5.0:
        print("      GOOD — caller waits < 5s")
    elif ready_elapsed and ready_elapsed < 10.0:
        print("      ACCEPTABLE — caller waits < 10s")
    elif ready_elapsed:
        print("      SLOW — caller may hang up before bot answers")
    else:
        print("      INCOMPLETE — full test requires Linux/droplet with Daily transport")


def test_rate_limit(host: str, burst: int = 5, caller: str = "+15551234567") -> None:
    """Fire multiple calls from the same number to test rate limiting."""
    print(f"\n{'='*60}")
    print("  PSTN DRY CALL — Rate Limit Test")
    print(f"  Target: {host}")
    print(f"  Caller: {caller}")
    print(f"  Burst:  {burst} calls")
    print(f"{'='*60}\n")

    results = []
    for i in range(burst):
        result = _post_webhook(host, caller)
        status_icon = "✓" if result["status"] == 200 else "✗"
        print(
            f"  [{i+1}/{burst}] {status_icon} status={result['status']} "
            f"elapsed={result['elapsed_ms']:.0f}ms "
            f"{result['body'].get('error', '')}"
        )
        results.append(result)
        time.sleep(0.1)  # Small gap between calls

    accepted = sum(1 for r in results if r["status"] == 200)
    rejected_429 = sum(1 for r in results if r["status"] == 429)
    rejected_503 = sum(1 for r in results if r["status"] == 503)

    print("\n  Summary:")
    print(f"    Accepted (200): {accepted}")
    print(f"    Rate-limited (429): {rejected_429}")
    print(f"    Capacity-limited (503): {rejected_503}")

    if rejected_429 > 0:
        print(f"\n  PASS — rate limiter triggered after {accepted} calls")
    else:
        print(f"\n  NOTE — all {burst} calls accepted (limit may be > {burst})")


def test_concurrent(host: str, count: int = 12) -> None:
    """Fire calls from different numbers simultaneously to test concurrent limit."""
    print(f"\n{'='*60}")
    print("  PSTN DRY CALL — Concurrent Limit Test")
    print(f"  Target: {host}")
    print(f"  Calls:  {count} (each from a different number)")
    print(f"{'='*60}\n")

    callers = [f"+1555{i:07d}" for i in range(count)]

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = {pool.submit(_post_webhook, host, c): c for c in callers}
        results = []
        for f in as_completed(futures):
            caller = futures[f]
            result = f.result()
            results.append((caller, result))

    # Sort by caller for readability
    results.sort(key=lambda x: x[0])

    for caller, result in results:
        status_icon = "✓" if result["status"] == 200 else "✗"
        print(
            f"  {status_icon} {caller} → {result['status']} "
            f"({result['elapsed_ms']:.0f}ms) {result['body'].get('error', '')}"
        )

    accepted = sum(1 for _, r in results if r["status"] == 200)
    rejected = sum(1 for _, r in results if r["status"] != 200)

    print("\n  Summary:")
    print(f"    Accepted: {accepted}")
    print(f"    Rejected: {rejected}")
    print(f"\n  {'PASS' if rejected > 0 else 'NOTE'} — "
          f"concurrent limit {'triggered' if rejected > 0 else 'not reached'}")


def test_webhook_validation(host: str) -> None:
    """Test webhook input validation and error handling."""
    print(f"\n{'='*60}")
    print("  PSTN DRY CALL — Webhook Validation Tests")
    print(f"  Target: {host}")
    print(f"{'='*60}\n")

    tests = [
        ("Test probe (no callId)", {"To": "+12262120379"}, 200),
        ("Invalid JSON", b"not-json{{{", 400),
        ("Empty body", b"", 400),
        ("Valid call", {"callId": "abc123", "callDomain": "v.daily.co",
                        "From": "+19999999999", "To": "+12262120379"}, 200),
    ]

    passed = 0
    for name, payload, expected_status in tests:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

        req = Request(
            f"{host}/pstn/webhook",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=10) as resp:  # noqa: S310
                status = resp.status
        except HTTPError as e:
            status = e.code
        except URLError as e:
            print(f"  ✗ {name}: connection failed ({e.reason})")
            continue

        icon = "✓" if status == expected_status else "✗"
        result = "PASS" if status == expected_status else f"FAIL (got {status})"
        print(f"  {icon} {name}: expected {expected_status} → {result}")
        if status == expected_status:
            passed += 1

    print(f"\n  {passed}/{len(tests)} tests passed")


def main():
    parser = argparse.ArgumentParser(description="PSTN dry-call testing")
    parser.add_argument("--host", default="http://localhost:8000", help="serve.py base URL")
    parser.add_argument("--burst", type=int, help="Rate limit test: fire N calls from same number")
    parser.add_argument("--concurrent", type=int, help="Concurrent limit test: fire N simultaneous calls")
    parser.add_argument("--validate", action="store_true", help="Run webhook validation tests")
    parser.add_argument("--all", action="store_true", help="Run all tests")
    parser.add_argument("--caller", default="+15551234567", help="Caller phone number to simulate")
    args = parser.parse_args()

    if args.all:
        test_webhook_validation(args.host)
        test_single_call(args.host, args.caller)
        test_rate_limit(args.host, burst=5, caller=args.caller)
        test_concurrent(args.host, count=12)
    elif args.validate:
        test_webhook_validation(args.host)
    elif args.burst:
        test_rate_limit(args.host, burst=args.burst, caller=args.caller)
    elif args.concurrent:
        test_concurrent(args.host, count=args.concurrent)
    else:
        test_single_call(args.host, args.caller)


if __name__ == "__main__":
    main()
