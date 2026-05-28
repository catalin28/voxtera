"""Transport health monitor for Daily WebRTC sessions.

Solves the problem where daily-python's Rust transport degrades after sitting
idle in an empty room for hours (the SFU connection breaks but the Python
process never crashes, so systemd considers it healthy).

Two exit triggers:

1. **Empty room** — no human participants for ``EMPTY_ROOM_TIMEOUT_SECS``
   (default 5 min). Restart is invisible since nobody is connected.

2. **Idle session** — a participant is connected but there has been no speech
   activity (VAD start events) for ``IDLE_SESSION_TIMEOUT_SECS`` (default
   30 min). Covers the case where a user opens the page, talks, then walks
   away without closing the tab. The browser stays "connected" but nobody
   is speaking, and the transport will degrade overnight.

In both cases the process exits with code 0 so systemd restarts it cleanly.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from loguru import logger
from pipecat.frames.frames import VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Default timeouts (seconds). Override with env vars.
_DEFAULT_EMPTY_ROOM_TIMEOUT = 300  # 5 minutes
_DEFAULT_IDLE_SESSION_TIMEOUT = 300  # 5 minutes


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


class TransportHealthMonitor:
    """Monitors Daily room occupancy and speech activity.

    Exits the process when the room is empty too long OR when a connected
    session has been idle (no speech) too long.

    Usage::

        monitor = TransportHealthMonitor()
        monitor.register(transport)
    """

    def __init__(self, *, bot_name: str = "Voxtera") -> None:
        self._bot_name = bot_name
        self._empty_timeout = _env_float("EMPTY_ROOM_TIMEOUT_SECS", _DEFAULT_EMPTY_ROOM_TIMEOUT)
        self._idle_timeout = _env_float("IDLE_SESSION_TIMEOUT_SECS", _DEFAULT_IDLE_SESSION_TIMEOUT)
        self._participant_count = 0
        # Start the empty-room clock immediately — if no human ever joins,
        # the watchdog will still fire after _empty_timeout seconds.
        self._empty_since: float | None = time.monotonic()
        self._last_activity: float = time.monotonic()
        self._watchdog_task: asyncio.Task | None = None

    def register(self, transport) -> None:
        """Register event handlers on a DailyTransport instance."""

        @transport.event_handler("on_participant_joined")
        async def _on_joined(transport, participant):
            self._on_participant_joined(participant)

        @transport.event_handler("on_participant_left")
        async def _on_left(transport, participant, reason):
            self._on_participant_left(participant)

        # Start the watchdog as soon as the bot connects to the room.
        @transport.event_handler("on_joined")
        async def _on_bot_joined(transport, data):
            self._ensure_watchdog()

    def notify_activity(self) -> None:
        """Call this whenever meaningful activity occurs (speech, user input).

        Resets the idle timer so the session stays alive while the user is
        actively interacting.
        """
        self._last_activity = time.monotonic()

    def _on_participant_joined(self, participant: dict) -> None:
        user_name = participant.get("info", {}).get("userName", "")
        if user_name == self._bot_name:
            return
        self._participant_count += 1
        self._empty_since = None
        self._last_activity = time.monotonic()
        self._ensure_watchdog()
        logger.debug(
            "[health] participant joined (count={})",
            self._participant_count,
        )

    def _on_participant_left(self, participant: dict) -> None:
        user_name = participant.get("info", {}).get("userName", "")
        if user_name == self._bot_name:
            return
        self._participant_count = max(0, self._participant_count - 1)
        logger.debug(
            "[health] participant left (count={})",
            self._participant_count,
        )
        if self._participant_count == 0:
            self._empty_since = time.monotonic()
            self._ensure_watchdog()

    def _ensure_watchdog(self) -> None:
        """Start the watchdog coroutine if not already running."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._watchdog_task = loop.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        """Periodically check both exit conditions."""
        while True:
            await asyncio.sleep(30)

            now = time.monotonic()

            # Condition 1: room is empty
            if self._participant_count == 0 and self._empty_since is not None:
                elapsed = now - self._empty_since
                if elapsed >= self._empty_timeout:
                    logger.info(
                        "[health] room empty for {:.0f}s (threshold {:.0f}s) — "
                        "exiting for fresh restart",
                        elapsed,
                        self._empty_timeout,
                    )
                    self._hard_exit()

            # Condition 2: participant connected but no speech activity
            if self._participant_count > 0:
                idle_elapsed = now - self._last_activity
                if idle_elapsed >= self._idle_timeout:
                    logger.info(
                        "[health] session idle for {:.0f}s (threshold {:.0f}s) — "
                        "exiting for fresh restart",
                        idle_elapsed,
                        self._idle_timeout,
                    )
                    self._hard_exit()

    @staticmethod
    def _hard_exit() -> None:
        """Force-exit the process. sys.exit() can be caught or blocked by
        lingering threads/event loops, so schedule os._exit as a fallback."""
        import threading

        def _force():
            time.sleep(5)
            os._exit(0)

        t = threading.Timer(5.0, lambda: os._exit(0))
        t.daemon = True
        t.start()
        sys.exit(0)


class ActivityNotifier(FrameProcessor):
    """Lightweight pipeline processor that resets the health monitor's idle timer.

    Insert anywhere in the pipeline after the VAD processor. It passes all
    frames through unchanged — its only job is to call
    ``monitor.notify_activity()`` on speech start events.
    """

    def __init__(self, monitor: TransportHealthMonitor) -> None:
        super().__init__()
        self._monitor = monitor

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._monitor.notify_activity()
        await self.push_frame(frame, direction)
