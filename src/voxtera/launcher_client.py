"""HTTP callback client for the on-demand bot launcher.

When the bot is spawned by ``serve.py``'s ``/api/start-session`` handler, the
launcher creates a ``queue.Queue`` keyed by a session id and blocks on
``q.get()`` waiting for the bot to confirm it has joined the Daily room. This
module is the bot's side of that handshake: it ``POST``s small JSON events to
the launcher's ``/api/bot-event`` endpoint, the launcher's handler does
``q.put(event)``, and the start-session request unblocks.

Why HTTP-callback + queue (vs. stdout, sockets, multiprocessing.Queue):
- Stdout mixes with logs and breaks the moment someone adds a ``print()``.
- Unix sockets work but cost ~150 LOC of framing for a benefit (microsecond
  latency) we don't need.
- ``multiprocessing.Queue`` would force the bot to be a child of the launcher's
  Python interpreter, which we don't want.
- HTTP + ``queue.Queue`` reuses the HTTP server we already run, lands in
  access logs for free, and is trivial to swap for Redis later — the bot
  side of the contract doesn't change.

Backward compatibility: when ``VOXTERA_LAUNCHER_URL`` is unset, every call
becomes a silent no-op. This is the path used by ``make run`` for local
development and by the legacy always-on bot — neither needs a launcher.

Failure semantics: any error talking to the launcher is logged at WARNING
and swallowed. The bot must never crash because the launcher is unreachable;
that would break local-mode runs and add a fragile dependency in production.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp
from loguru import logger

# Read once at import time. The launcher sets these as environment variables
# when it spawns the bot subprocess (see ``serve.py``'s start-session handler).
# When unset (e.g. ``make run``), ``post_event`` short-circuits to a no-op.
SESSION_ID: str | None = os.environ.get("VOXTERA_SESSION_ID")
LAUNCHER_URL: str | None = os.environ.get("VOXTERA_LAUNCHER_URL")

# 2 s is comfortably above the localhost round-trip and well below anything
# the user would notice. A hung launcher should not stall the voice loop.
_HTTP_TIMEOUT_SECS: float = 2.0


def is_enabled() -> bool:
    """Return True if the launcher callback is configured for this process."""
    return bool(SESSION_ID and LAUNCHER_URL)


async def post_event(event_type: str, **payload: Any) -> None:
    """POST a JSON event to the launcher.

    Args:
        event_type: One of ``"ready"``, ``"error"``, ``"exiting"``. New types
            can be added freely; the launcher's handler dispatches by type
            and ignores unknown ones.
        **payload: Extra fields merged into the JSON body. ``session_id`` and
            ``type`` are always set by this function.

    Behaviour:
        - No-op when ``VOXTERA_LAUNCHER_URL`` or ``VOXTERA_SESSION_ID`` is unset.
        - Logs a warning and returns normally on any HTTP / connection error.
          Never raises.
    """
    if not is_enabled():
        return

    body = {"session_id": SESSION_ID, "type": event_type, **payload}
    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SECS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(LAUNCHER_URL, json=body) as resp,
        ):
            if resp.status >= 400:
                text = await resp.text()
                logger.warning(
                    "[launcher] POST {} -> {} {}",
                    event_type,
                    resp.status,
                    text[:200],
                )
            else:
                logger.debug("[launcher] POST {} -> {}", event_type, resp.status)
    except Exception as exc:  # noqa: BLE001 — broad except is intentional
        # Any connection / DNS / timeout error: log and move on. The voice
        # loop must not crash because the launcher is unreachable.
        logger.warning(
            "[launcher] POST {} failed: {} (launcher_url={})",
            event_type,
            exc,
            LAUNCHER_URL,
        )
