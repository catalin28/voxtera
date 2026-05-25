"""Admin surface for Voxtera — Daily REST helpers and operator endpoints.

The admin package is intentionally small. It owns:

- :mod:`voxtera.admin.daily_client` — a thin wrapper around the Daily REST
  endpoints we use to list and eject participants. This is the single place
  in the codebase that talks to ``api.daily.co``; both the always-on bot's
  ``_eject_stale_bots`` cleanup and the operator-facing ``/api/admin/*``
  endpoints in ``demo-hotel/serve.py`` go through it.
"""

from voxtera.admin.daily_client import (
    DailyAPIError,
    DailyParticipant,
    create_room,
    delete_room,
    eject_participants,
    list_room_participants,
)

__all__ = [
    "DailyAPIError",
    "DailyParticipant",
    "create_room",
    "delete_room",
    "eject_participants",
    "list_room_participants",
]
