"""Daily REST client — list participants in a room and eject them.

This module is the single place in the codebase that knows how to talk to
``api.daily.co``. Both the bot's startup cleanup (:func:`voxtera.pipeline._eject_stale_bots`)
and the operator-facing admin endpoints in ``demo-hotel/serve.py`` use these
helpers, so changes to Daily's API surface land here and only here.

We expose **synchronous** helpers built on :mod:`urllib` rather than async
ones for two reasons:

1. The only callers today are a synchronous ``http.server`` request handler
   (``demo-hotel/serve.py``) and the synchronous startup hook in
   ``pipeline.py``. Making them async would force both into event loops they
   don't otherwise need.
2. Daily REST round-trips are a few hundred milliseconds at most. The cost
   of running them on the request thread is tolerable for the demo's one-
   operator-at-a-time admin page.

If the admin surface ever grows to fan out across many rooms or many
operators, swap these for ``aiohttp`` variants — the function shapes already
fit cleanly into ``async def``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from loguru import logger

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DAILY_REST_BASE: str = "https://api.daily.co/v1"

# 5 s comfortably covers Daily's published p99 (~700 ms) without making the
# operator's browser hang. The request handler will surface a 504 if we hit
# this ceiling.
_REST_TIMEOUT_SECS: float = 5.0


# ---------------------------------------------------------------------------
# Errors and data shapes
# ---------------------------------------------------------------------------


class DailyAPIError(RuntimeError):
    """Raised when Daily REST returns a non-2xx response or is unreachable.

    ``status`` is the HTTP status from Daily when available, or ``None`` if
    the request failed before we got a response (DNS, TCP, timeout). The
    string form is suitable for surfacing to the operator.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DailyParticipant:
    """Normalised view of one row from ``GET /v1/presence``.

    Daily's response uses camelCase and a few duplicated fields; this struct
    keeps the rest of the codebase free of those quirks. ``raw`` holds the
    original dict so callers can opt back in if they need a field we did not
    surface here.
    """

    id: str
    user_name: str
    joined_at: str  # ISO 8601 UTC string, exactly as Daily returned it
    duration_secs: int
    room_name: str
    raw: dict[str, Any]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request_json(
    url: str,
    *,
    api_key: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a JSON request to Daily REST and return the parsed response.

    Wraps :func:`urllib.request.urlopen` with consistent auth, content-type,
    timeout and error normalisation. All failures bubble out as
    :class:`DailyAPIError` so callers don't need to know which urllib
    exception means what.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=_REST_TIMEOUT_SECS) as resp:
            payload = resp.read()
    except HTTPError as exc:
        # 4xx / 5xx — Daily often returns a JSON body explaining why; preserve it.
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        raise DailyAPIError(
            f"Daily REST {method} {url} returned {exc.code}: {err_body or exc.reason}",
            status=exc.code,
        ) from exc
    except URLError as exc:
        raise DailyAPIError(f"Daily REST {method} {url} unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise DailyAPIError(
            f"Daily REST {method} {url} timed out after {_REST_TIMEOUT_SECS}s"
        ) from exc

    try:
        result: dict[str, Any] = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DailyAPIError(f"Daily REST {method} {url} returned non-JSON body") from exc
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_room_participants(*, api_key: str, room_name: str) -> list[DailyParticipant]:
    """Return the list of participants currently in ``room_name``.

    Daily's ``/v1/presence`` endpoint returns the live participants for
    *every* room the API key can see, keyed by room name. We filter to one
    room here so the rest of the app never has to think about that fan-out.

    An empty list is returned when:
    - the room is not in the response at all (i.e. nobody has joined since
      the room was created), or
    - the room exists but has no participants right now.
    """
    if not api_key:
        raise DailyAPIError("DAILY_API_KEY is not set")
    if not room_name:
        raise DailyAPIError("room_name is required")

    payload = _request_json(f"{_DAILY_REST_BASE}/presence", api_key=api_key)
    raw_list = payload.get(room_name) or []

    participants: list[DailyParticipant] = []
    for entry in raw_list:
        # Daily occasionally renames or duplicates fields between dashboard
        # and REST. Defensive .get() with sensible defaults keeps the page
        # rendering even if a single field is missing.
        participants.append(
            DailyParticipant(
                id=str(entry.get("id", "")),
                user_name=str(entry.get("user_name") or entry.get("userName") or ""),
                joined_at=str(entry.get("joined_at") or entry.get("joinedAt") or ""),
                duration_secs=int(entry.get("duration") or 0),
                room_name=room_name,
                raw=entry,
            )
        )
    return participants


def eject_participants(
    *,
    api_key: str,
    room_name: str,
    participant_ids: list[str],
) -> list[str]:
    """Eject one or more participants from ``room_name``.

    Returns the list of IDs Daily confirms it ejected. Any IDs the operator
    asked to kick that don't appear in the result are surfaced by the caller
    so the UI can flag them (typically the participant left between the
    presence fetch and the eject call — a benign race).

    Calling this with an empty list is a no-op and returns an empty list.
    We do not call Daily in that case to avoid wasting a round-trip and to
    avoid Daily's "ids cannot be empty" 400.
    """
    if not api_key:
        raise DailyAPIError("DAILY_API_KEY is not set")
    if not room_name:
        raise DailyAPIError("room_name is required")
    if not participant_ids:
        return []

    payload = _request_json(
        f"{_DAILY_REST_BASE}/rooms/{room_name}/eject",
        api_key=api_key,
        method="POST",
        body={"ids": participant_ids},
    )
    ejected = payload.get("ejectedIds") or []
    if not isinstance(ejected, list):
        # Defensive — Daily has shipped shape changes before; log and treat
        # an unexpected shape as "we don't know what was ejected".
        logger.warning("[daily] /eject returned unexpected ejectedIds: {!r}", ejected)
        return []
    return [str(pid) for pid in ejected]


def create_room(
    *,
    api_key: str,
    room_name: str,
    expiry_secs: int = 600,
    max_participants: int = 2,
) -> dict[str, Any]:
    """Create an ephemeral Daily room for one session.

    ``expiry_secs`` sets a hard Daily-side time-to-live so orphan rooms are
    cleaned up even if the launcher crashes before calling :func:`delete_room`.
    ``max_participants`` controls who can join:

    - 2 = bot + guest (default, private call)
    - 3+ = bot + guest + supervisor(s) for quality-monitoring use-cases
    """
    import time

    if not api_key:
        raise DailyAPIError("DAILY_API_KEY is not set")
    if not room_name:
        raise DailyAPIError("room_name is required")

    body: dict[str, Any] = {
        "name": room_name,
        "properties": {
            "exp": int(time.time()) + expiry_secs,
            "enable_prejoin_ui": False,
            "max_participants": max_participants,
        },
    }
    result = _request_json(
        f"{_DAILY_REST_BASE}/rooms",
        api_key=api_key,
        method="POST",
        body=body,
    )
    logger.info("[daily] created room {} (max_participants={})", room_name, max_participants)
    return result


def delete_room(*, api_key: str, room_name: str) -> bool:
    """Delete a Daily room.

    Returns True whether the room existed or not (idempotent — 404 is not an
    error; the room is gone either way).

    Daily's ``DELETE /v1/rooms/{name}`` returns ``200`` with a JSON body
    ``{"deleted": true, "name": "..."}``.  If Daily ever changes this to
    ``204 No Content``, :func:`_request_json` will raise ``JSONDecodeError``
    — in that case add an ``allow_empty`` flag or use raw ``urlopen`` here.
    """
    if not api_key:
        raise DailyAPIError("DAILY_API_KEY is not set")
    if not room_name:
        raise DailyAPIError("room_name is required")

    try:
        _request_json(
            f"{_DAILY_REST_BASE}/rooms/{room_name}",
            api_key=api_key,
            method="DELETE",
        )
    except DailyAPIError as exc:
        if exc.status == 404:
            logger.debug("[daily] delete_room {}: already gone (404)", room_name)
            return True
        raise
    logger.info("[daily] deleted room {}", room_name)
    return True
