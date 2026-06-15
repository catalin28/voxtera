"""Ask the concierge a question from the CLI — no HTTP server, no voice.

Runs the same ConciergePipeline that ``/api/concierge`` runs, in-process. Use
this to reproduce or verify the P0 scaffold-leak fix without spinning up the
service: pass ``--session-id`` across calls to build a multi-turn dialogue
through Redis (same code path as a real guest turn, minus STT and TTS).

Examples:
    # Single turn, fresh session
    python scripts/ask.py "When is breakfast served?" --hotel-id kempinski

    # Multi-turn — same session_id reuses the Redis history
    python scripts/ask.py "table for two tomorrow 7pm" -s demo123 --hotel-id kempinski
    python scripts/ask.py "we are in-house guests" -s demo123 --hotel-id kempinski

    # See the full pipeline dict (path, decomposition, retrieval, timings...)
    python scripts/ask.py "pool hours?" --hotel-id kempinski --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from dotenv import load_dotenv


def _force_utf8_stdio() -> None:
    """Avoid UnicodeEncodeError on Windows cp1252 consoles for Turkish/Cyrillic output."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


async def _run(args: argparse.Namespace) -> int:
    # Imported lazily so ``--help`` doesn't trigger the heavy app deps.
    from voxtera.call_center.deps import build_concierge_deps, build_pipeline
    from voxtera.call_center.session import new_session_id

    deps = await build_concierge_deps()
    try:
        # Pre-seed session language when the caller specifies one. Text mode
        # has no STT-detected language, so without this the render falls back
        # to English even for Turkish input.
        sid = args.session_id
        if args.language:
            sid = sid or new_session_id()
            store = deps["store"]
            sess = await store.load(sid)
            sess["language"] = args.language.lower()
            await store.save(sess)

        pipeline = build_pipeline(deps)
        result = await pipeline.run(
            utterance=args.utterance,
            session_id=sid,
            region=args.region,
            brief=args.brief,
            hotel_id=args.hotel_id,
            images=False,
            menus=False,
        )
    finally:
        await deps["http"].close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        answer = (result or {}).get("answer") or "(no answer)"
        sid = (result or {}).get("session_id") or "(no session)"
        path = (result or {}).get("path") or "?"
        print(f"[session_id={sid} path={path}]")
        print(answer)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ask the concierge (text-only, in-process).")
    p.add_argument("utterance", help="The guest's message.")
    p.add_argument(
        "-s",
        "--session-id",
        default=None,
        help="Reuse a session to build multi-turn dialogue (default: new session each call).",
    )
    p.add_argument(
        "--hotel-id",
        default=None,
        help="Scope to one property (hotel-concierge mode); omit for travel-agent mode.",
    )
    p.add_argument(
        "--region",
        default=None,
        help="Region hint for travel-agent mode. Pass '' for explicit all-regions.",
    )
    p.add_argument("--brief", action="store_true", help="Ask the render for a shorter reply.")
    p.add_argument(
        "--language",
        default=None,
        help=(
            "Force reply language (e.g. en, tr, ru, ro, fr, hy). "
            "Text mode has no STT to detect it."
        ),
    )
    p.add_argument("--json", action="store_true", help="Print the full pipeline result dict.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    load_dotenv()
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
