"""Tests for ``voxtera.admin.daily_client``.

These tests stub out :func:`urllib.request.urlopen` so we can verify the
exact requests we send to Daily REST and the way we normalise its
responses, without ever touching the network.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from voxtera.admin.daily_client import (
    DailyAPIError,
    eject_participants,
    list_room_participants,
)


class _FakeResponse:
    """Minimal stand-in for the urlopen() context manager value."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = json.dumps(payload).encode()

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class TestListRoomParticipants:
    def test_filters_to_requested_room(self) -> None:
        """``/v1/presence`` returns every room; we keep only the requested one."""
        presence = {
            "voxtera-demo": [
                {
                    "id": "p1",
                    "user_name": "Voxtera",
                    "joined_at": "2026-05-05T09:00:00Z",
                    "duration": 120,
                },
                {
                    "id": "p2",
                    "user_name": "Guest",
                    "joined_at": "2026-05-05T09:01:00Z",
                    "duration": 60,
                },
            ],
            "some-other-room": [{"id": "p3", "user_name": "Spy", "duration": 999}],
        }
        with patch(
            "voxtera.admin.daily_client.urlopen",
            return_value=_FakeResponse(presence),
        ):
            result = list_room_participants(api_key="k", room_name="voxtera-demo")
        assert [p.id for p in result] == ["p1", "p2"]
        assert result[0].user_name == "Voxtera"
        assert result[0].duration_secs == 120

    def test_room_absent_returns_empty(self) -> None:
        """Daily omits the key entirely when nobody has joined the room."""
        with patch(
            "voxtera.admin.daily_client.urlopen",
            return_value=_FakeResponse({}),
        ):
            result = list_room_participants(api_key="k", room_name="voxtera-demo")
        assert result == []

    def test_camelcase_fallbacks(self) -> None:
        """Daily has shipped both ``user_name`` and ``userName``; we accept either."""
        presence = {
            "demo": [
                {
                    "id": "p1",
                    "userName": "GuestX",
                    "joinedAt": "2026-05-05T09:00:00Z",
                    "duration": 30,
                }
            ]
        }
        with patch(
            "voxtera.admin.daily_client.urlopen",
            return_value=_FakeResponse(presence),
        ):
            result = list_room_participants(api_key="k", room_name="demo")
        assert result[0].user_name == "GuestX"
        assert result[0].joined_at == "2026-05-05T09:00:00Z"

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(DailyAPIError):
            list_room_participants(api_key="", room_name="demo")

    def test_missing_room_name_raises(self) -> None:
        with pytest.raises(DailyAPIError):
            list_room_participants(api_key="k", room_name="")

    def test_http_error_surfaces_status_and_message(self) -> None:
        err = HTTPError(
            "https://api.daily.co/v1/presence",
            403,
            "Forbidden",
            {},  # type: ignore[arg-type]
            BytesIO(b'{"error":"bad token"}'),
        )
        with (
            patch("voxtera.admin.daily_client.urlopen", side_effect=err),
            pytest.raises(DailyAPIError) as exc_info,
        ):
            list_room_participants(api_key="k", room_name="demo")
        assert exc_info.value.status == 403
        assert "bad token" in str(exc_info.value)

    def test_url_error_becomes_daily_api_error(self) -> None:
        with (
            patch(
                "voxtera.admin.daily_client.urlopen",
                side_effect=URLError("name resolution failed"),
            ),
            pytest.raises(DailyAPIError) as exc_info,
        ):
            list_room_participants(api_key="k", room_name="demo")
        assert exc_info.value.status is None
        assert "name resolution failed" in str(exc_info.value)


class TestEjectParticipants:
    def test_empty_ids_is_noop(self) -> None:
        """Skip the round-trip when there's nothing to eject — Daily 400s on empty."""
        with patch("voxtera.admin.daily_client.urlopen") as mock_urlopen:
            result = eject_participants(api_key="k", room_name="demo", participant_ids=[])
        assert result == []
        mock_urlopen.assert_not_called()

    def test_returns_ejected_ids(self) -> None:
        with patch(
            "voxtera.admin.daily_client.urlopen",
            return_value=_FakeResponse({"ejectedIds": ["p1", "p2"]}),
        ):
            result = eject_participants(api_key="k", room_name="demo", participant_ids=["p1", "p2"])
        assert result == ["p1", "p2"]

    def test_sends_post_with_ids_body(self) -> None:
        """Daily's contract is POST {ids:[...]} to /v1/rooms/{room}/eject."""
        captured: dict[str, Any] = {}

        def _fake_urlopen(req: Any, timeout: float = 0) -> _FakeResponse:
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            return _FakeResponse({"ejectedIds": ["p1"]})

        with patch("voxtera.admin.daily_client.urlopen", side_effect=_fake_urlopen):
            eject_participants(api_key="my-key", room_name="demo", participant_ids=["p1"])

        assert captured["method"] == "POST"
        assert captured["url"].endswith("/v1/rooms/demo/eject")
        assert captured["body"] == {"ids": ["p1"]}
        assert captured["auth"] == "Bearer my-key"

    def test_unexpected_eject_response_shape_is_safe(self) -> None:
        """If Daily ever returns a non-list, we don't crash — return []."""
        with patch(
            "voxtera.admin.daily_client.urlopen",
            return_value=_FakeResponse({"ejectedIds": "p1"}),  # bad shape
        ):
            result = eject_participants(api_key="k", room_name="demo", participant_ids=["p1"])
        assert result == []
