"""TimeContextInjector — gives the LLM the guest's current local time each turn.

Without this the bot has no idea what time it is, so it cannot answer
time-sensitive questions: "is the spa still open?", "can I still get
breakfast?", "how long until checkout?". This FrameProcessor fixes that.

Where the time comes from
-------------------------
The browser sends the guest's IANA timezone once, in a ``voxtera-timezone``
app-message (the same message :class:`~voxtera.controllers.GreetingController`
uses for the time-of-day greeting). From that timezone the server computes the
*current* time fresh on every turn — the browser never has to send the clock
itself. When no timezone has been reported (phone line, Telegram, local
``make run``) the server's own clock is used as the fallback, which is correct
in those cases: there, the machine running the bot is effectively the listener.

Why the user message and not the system prompt
----------------------------------------------
The system prompt is a cached prefix for Anthropic prompt caching. Writing an
ever-changing timestamp into it would bust the cache on every turn (0 cache
reads, full re-prefill — see ``rag/injector.py``). So, exactly like
:class:`~voxtera.rag.injector.RAGContextInjector`, the time note is appended to
the *current* user message only; earlier turns are left byte-stable and the
cached prefix holds.

Safety: never raises — on any error the context is pushed through unmodified.
Idempotent: never injects twice into the same user message.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

try:
    from pipecat.transports.daily.transport import DailyInputTransportMessageFrame
except Exception:  # daily-python not available on Windows
    DailyInputTransportMessageFrame = None  # type: ignore[assignment,misc]

# Opening marker of the injected note — also the idempotency guard.
_TIME_NOTE_PREFIX = "[Time check —"


class TimeContextInjector(FrameProcessor):
    """Appends the guest's current local time to each user turn for the LLM."""

    def __init__(self) -> None:
        super().__init__()
        # IANA timezone reported by the browser (e.g. "Europe/Paris"). None
        # until a voxtera-timezone message arrives — or forever, for
        # non-browser transports, in which case the server clock is used.
        self._timezone: str | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Learn the guest's timezone from the browser app-message.
        if DailyInputTransportMessageFrame is not None and isinstance(
            frame, DailyInputTransportMessageFrame
        ):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("type") == "voxtera-timezone":
                tz = msg.get("tz")
                if tz:
                    self._timezone = tz
                    logger.info("[time-context] guest timezone set to {!r}", tz)

        # Inject the current time into the latest user message.
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            try:
                self._inject_time(frame)
            except Exception:
                # Time context is a nice-to-have; never break the voice loop.
                logger.opt(exception=True).error("[time-context] injection failed")

        await self.push_frame(frame, direction)

    def _now(self) -> tuple[datetime, str]:
        """Return ``(current_time, source_label)``.

        Uses the guest's reported timezone when available; otherwise the
        server's own local clock. An unrecognised timezone also degrades to
        the server clock rather than failing.
        """
        if self._timezone:
            try:
                return datetime.now(ZoneInfo(self._timezone)), self._timezone
            except (ZoneInfoNotFoundError, ValueError, OSError):
                logger.warning(
                    "[time-context] unknown timezone {!r}; using server clock",
                    self._timezone,
                )
        return datetime.now(), "server local time"

    def _inject_time(self, frame: LLMContextFrame) -> None:
        """Append a current-time note to the latest user message.

        Only that message is rewritten — every earlier turn stays byte-for-byte
        immutable, so the cached prompt prefix holds (see module docstring).
        """
        messages = frame.context.messages

        # Find the latest user message.
        user_idx: int | None = None
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if isinstance(msg, dict) and msg.get("role") == "user":
                user_idx = i
                break
        if user_idx is None:
            return

        user_msg = messages[user_idx]
        content = user_msg.get("content", "")
        if not isinstance(content, str) or not content.strip():
            return

        # Idempotency: if this message already carries a time note (e.g. the
        # context frame is reprocessed), do not inject again.
        if _TIME_NOTE_PREFIX in content:
            return

        now, source = self._now()
        # Built without %-d / %-I so it stays portable across OSes.
        hour_12 = now.hour % 12 or 12
        ampm = "AM" if now.hour < 12 else "PM"
        stamp = f"{now:%A}, {now.day} {now:%B} {now.year}, {hour_12}:{now.minute:02d} {ampm}"

        note = (
            f"{_TIME_NOTE_PREFIX} for context only, do not read this aloud: the "
            f"current local time is {stamp} ({source}). Use it only when the "
            f"guest asks something time-sensitive, e.g. opening hours.]"
        )

        messages[user_idx] = {
            **user_msg,
            "content": f"{content}\n\n{note}",
        }
        logger.debug("[time-context] injected time {!r} (source={})", stamp, source)
