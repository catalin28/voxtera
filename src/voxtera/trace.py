"""In-process trace bus + turn correlation + forwarder to serve.py.

The trace plane has three pieces:

- :class:`TraceBus` — a singleton in-process pub/sub. Every probe, processor,
  or controller that wants to emit a trace event calls :func:`emit`. Events
  flow into a bounded ring buffer and out to async subscribers. Lossy by
  design: a slow subscriber drops oldest events instead of blocking the
  pipeline.
- :class:`TurnTracker` — assigns a stable ``turn_id`` to every
  ``VADUserStartedSpeakingFrame`` so all events in one turn can be grouped.
  Greeting turns get ``turn_id="greeting"``.
- :class:`TraceForwarder` — async task that drains the bus and POSTs events
  to ``serve.py``'s ``/api/bot-event`` endpoint via :mod:`launcher_client`.
  When the launcher callback isn't configured (legacy ``make run``), the
  forwarder is a silent no-op: events still accumulate in the ring buffer
  for any in-process consumer (e.g. the bot's own ``/knobs`` HTTP endpoint).

The schema is versioned (``voxtera.trace.v1``) so a future v2 can add fields
without breaking existing dashboards.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import aiohttp
from loguru import logger

SCHEMA = "voxtera.trace.v1"

EventKind = Literal["frame", "stage", "error", "knob", "audio", "lifecycle", "frame_drop"]


@dataclass
class TraceEvent:
    """One trace record. Field order matches the JSON wire format."""

    kind: EventKind
    source: str
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    turn_id: str | None = None
    session_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict matching the wire format."""
        d = asdict(self)
        # Drop None values so the wire payload stays compact.
        return {k: v for k, v in d.items() if v is not None}


class TraceBus:
    """Singleton in-process pub/sub with a bounded ring buffer.

    Designed to be safe to call from anywhere in the pipeline without coupling
    to the consumer. Subscribers receive a per-subscriber ``asyncio.Queue``.
    A full queue causes the oldest event to be dropped (lossy fan-out) so
    a slow subscriber never blocks the pipeline.
    """

    _instance: TraceBus | None = None

    def __init__(self, *, buffer_size: int = 5000, subscriber_queue_size: int = 1000) -> None:
        self._buffer: deque[TraceEvent] = deque(maxlen=buffer_size)
        self._subscribers: list[asyncio.Queue[TraceEvent]] = []
        self._subscriber_queue_size = subscriber_queue_size
        self._lock = asyncio.Lock()  # guards _subscribers list mutations

    @classmethod
    def instance(cls) -> TraceBus:
        """Return the process-wide bus, creating it on first access."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def emit(self, event: TraceEvent) -> None:
        """Publish an event. Synchronous and non-blocking.

        Safe to call from sync contexts (frame processors are async, but the
        emit itself touches no I/O). Dropped events on full subscriber queues
        are silently discarded — the ring buffer still keeps them available
        for late-joining subscribers via :meth:`recent`.
        """
        self._buffer.append(event)
        # Fan out to every live subscriber.
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest from this subscriber's queue and retry once.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(event)

    async def subscribe(self) -> asyncio.Queue[TraceEvent]:
        """Register a new subscriber and return its event queue."""
        q: asyncio.Queue[TraceEvent] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[TraceEvent]) -> None:
        """Remove a subscriber. Safe to call multiple times."""
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def recent(self, limit: int = 200) -> list[TraceEvent]:
        """Return the last ``limit`` events from the ring buffer."""
        if limit >= len(self._buffer):
            return list(self._buffer)
        return list(self._buffer)[-limit:]

    def buffer_size(self) -> int:
        """Current number of events held."""
        return len(self._buffer)

    def subscriber_count(self) -> int:
        """Number of live subscribers."""
        return len(self._subscribers)


def emit(
    kind: EventKind,
    source: str,
    *,
    turn_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    """Convenience wrapper around :meth:`TraceBus.emit`.

    Most callers should use this rather than handling the bus directly.
    """
    TraceBus.instance().emit(
        TraceEvent(
            kind=kind,
            source=source,
            turn_id=turn_id,
            data=data or {},
        )
    )


class DropAccumulator:
    """Counts dropped/silenced frames per reason and emits batched events.

    Used by frame processors that filter or mute frames (PlaybackLeakageGuard,
    BotActiveUserFrameSuppressor, STTGate, TTSGate, …) so the dashboard can
    surface a per-stage "X dropped" counter without flooding the trace bus
    with one event per dropped frame (which at 50 fps audio rate would be
    hundreds of events per second).

    Flush policy: every ``batch_count`` drops *or* every ``batch_ms`` since
    the first un-flushed drop, whichever comes first. ``record`` is sync and
    non-blocking — it just bumps counters and, when a threshold is hit,
    synchronously calls :func:`emit`.

    Pass ``action="silenced"`` for stages that push the frame with the audio
    zeroed out (leakage_guard), ``action="dropped"`` for stages that return
    without pushing (suppressor, gates). The dashboard renders both the
    same way but the action field is visible in the tooltip.
    """

    def __init__(
        self,
        *,
        stage: str,
        action: str = "dropped",
        batch_count: int = 50,
        batch_ms: int = 1000,
    ) -> None:
        self._stage = stage
        self._action = action
        self._batch_count = batch_count
        self._batch_ms = batch_ms
        self._counts: dict[str, int] = {}
        self._first_drop_ts: int | None = None
        self._total_in_batch = 0

    def record(self, reason: str, *, turn_id: str | None = None) -> None:
        """Register one dropped/silenced frame. Flushes if a threshold trips."""
        now_ms = int(time.time() * 1000)
        if self._first_drop_ts is None:
            self._first_drop_ts = now_ms
        self._counts[reason] = self._counts.get(reason, 0) + 1
        self._total_in_batch += 1
        if (
            self._total_in_batch >= self._batch_count
            or (now_ms - self._first_drop_ts) >= self._batch_ms
        ):
            self.flush(turn_id=turn_id)

    def flush(self, *, turn_id: str | None = None) -> None:
        """Emit a ``frame_drop`` event with the accumulated counts and reset."""
        if not self._counts:
            return
        emit(
            "frame_drop",
            source=self._stage,
            turn_id=turn_id,
            data={
                "action": self._action,
                "count": self._total_in_batch,
                "by_reason": dict(self._counts),
            },
        )
        self._counts = {}
        self._total_in_batch = 0
        self._first_drop_ts = None


class TurnTracker:
    """Assigns a stable ``turn_id`` to each user turn AND holds shared per-turn
    timing anchors that any pipeline processor can read or write.

    The id format is human-debuggable: ``turn-<isoformat>-<seq>``. ``seq`` is
    a per-process monotonically-increasing counter so two turns at the same
    millisecond are still distinguishable.

    The anchor map is the trick that lets stage timing work across pipeline
    positions. Three frames Pipecat routes don't all reach a single processor:
    ``TranscriptionFrame`` is consumed by ``context_aggregator.user()`` before
    it reaches anything LLM-side; ``TTSStartedFrame`` is emitted *downstream*
    of the LLM and never bubbles back up. So timing one stage end-to-end
    requires reading anchors set by a different processor at a different
    position. Putting the anchors on this shared tracker is what makes that
    possible.

    Greeting turns are reported as ``turn_id="greeting"`` (one constant value)
    since they don't follow the user-utterance shape.
    """

    GREETING_TURN_ID = "greeting"

    def __init__(self) -> None:
        self._current: str | None = None
        self._seq = 0
        # Shared per-turn timing anchors. Keys are short strings agreed
        # between the processors that write and read each anchor:
        #   user_stopped  — set by PipelineTracer on VADUserStoppedSpeakingFrame
        #   transcript    — set by TranscriptStageTimer on TranscriptionFrame
        #   llm_ended     — set by PipelineTracer on LLMFullResponseEndFrame
        #   tts_started   — set by TTSStageTimer on TTSStartedFrame
        # All values are ``time.monotonic()`` floats. Cleared at end_turn().
        self._anchors: dict[str, float] = {}

    def start_user_turn(self) -> str:
        """Begin a new user turn and return its id."""
        self._seq += 1
        # Use UTC, to-the-millisecond, then a 3-digit zero-padded sequence.
        ms = int(time.time() * 1000)
        self._current = f"turn-{ms}-{self._seq:03d}"
        # Fresh turn — drop any anchors left over from the previous turn.
        self._anchors.clear()
        return self._current

    def current(self) -> str | None:
        """Return the current turn's id, or None if no turn is active."""
        return self._current

    def end_turn(self) -> None:
        """Mark the end of the current turn. Subsequent emits use ``None``."""
        self._current = None
        # Belt-and-braces: no anchor outlives a turn so a follow-up typed
        # turn (with no VAD events of its own) can't read a stale one.
        self._anchors.clear()

    def for_greeting(self) -> str:
        """Return the constant greeting turn id (and set as current)."""
        self._current = self.GREETING_TURN_ID
        self._anchors.clear()
        return self.GREETING_TURN_ID

    # ------------------------------------------------------------------
    # Shared per-turn anchor API.
    # ------------------------------------------------------------------

    def stamp(self, key: str, *, value: float | None = None) -> float:
        """Stamp ``key`` with the current monotonic time (or an explicit value).

        Returns the timestamp written so the caller can chain it into a
        latency calculation in the same expression.
        """
        t = time.monotonic() if value is None else value
        self._anchors[key] = t
        return t

    def anchor(self, key: str) -> float | None:
        """Return the anchor value for ``key`` or ``None`` if unset."""
        return self._anchors.get(key)

    def measure_ms_from(self, key: str, *, end: float | None = None) -> int | None:
        """Return rounded milliseconds elapsed since the anchor at ``key``.

        Returns ``None`` when the anchor is unset (e.g. the upstream stage
        didn't fire — typical for typed-text turns that have no VAD).
        """
        start = self._anchors.get(key)
        if start is None:
            return None
        end_t = time.monotonic() if end is None else end
        return round((end_t - start) * 1000)

    def clear_anchor(self, key: str) -> None:
        """Drop a single anchor. Safe to call when the key isn't set."""
        self._anchors.pop(key, None)


# Process-wide tracker. All processors that emit per-turn events read from this.
_TRACKER: TurnTracker | None = None


def tracker() -> TurnTracker:
    """Return the process-wide :class:`TurnTracker`."""
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = TurnTracker()
    return _TRACKER


class TraceForwarder:
    """Drains :class:`TraceBus` and POSTs events to ``serve.py``.

    Uses the same callback URL as :mod:`launcher_client`. When the launcher
    callback isn't configured (no ``VOXTERA_LAUNCHER_URL``), the forwarder
    starts but immediately becomes a no-op: events still accumulate in the
    bus's ring buffer for in-process consumers (e.g. the bot's HTTP server).

    Batching: events are POSTed in small batches (up to ``batch_size`` events
    or every ``flush_interval`` seconds, whichever comes first) to keep the
    HTTP overhead low without adding noticeable latency to the trace view.
    """

    def __init__(
        self,
        launcher_url: str | None,
        session_id: str | None,
        *,
        batch_size: int = 25,
        flush_interval_secs: float = 0.25,
        http_timeout_secs: float = 2.0,
    ) -> None:
        self._launcher_url = launcher_url
        self._session_id = session_id
        self._batch_size = batch_size
        self._flush_interval = flush_interval_secs
        self._http_timeout = aiohttp.ClientTimeout(total=http_timeout_secs)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._queue: asyncio.Queue[TraceEvent] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._launcher_url and self._session_id)

    async def start(self) -> None:
        """Begin draining the bus into HTTP POSTs."""
        if self._task is not None:
            return
        self._queue = await TraceBus.instance().subscribe()
        self._task = asyncio.create_task(self._run(), name="trace-forwarder")
        if self.enabled:
            logger.info(
                "[trace] forwarder started (launcher={}, session={})",
                self._launcher_url,
                self._session_id,
            )
        else:
            logger.info(
                "[trace] forwarder started in standalone mode " "(no launcher callback configured)"
            )

    async def stop(self) -> None:
        """Stop draining and unsubscribe."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        if self._queue is not None:
            await TraceBus.instance().unsubscribe(self._queue)
        logger.info("[trace] forwarder stopped")

    async def _run(self) -> None:
        """Main drain loop: collect events, batch, POST."""
        assert self._queue is not None
        batch: list[TraceEvent] = []
        last_flush = time.monotonic()

        while not self._stop.is_set():
            timeout = max(0.0, self._flush_interval - (time.monotonic() - last_flush))
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout or 0.001)
                batch.append(event)
            except TimeoutError:
                pass

            now = time.monotonic()
            should_flush = len(batch) >= self._batch_size or (
                batch and (now - last_flush) >= self._flush_interval
            )

            if should_flush and batch:
                if self.enabled:
                    await self._post_batch(batch)
                batch = []
                last_flush = now

        # Drain remainder on shutdown.
        while True:
            try:
                event = self._queue.get_nowait()
                batch.append(event)
            except asyncio.QueueEmpty:
                break
        if batch and self.enabled:
            await self._post_batch(batch)

    async def _post_batch(self, batch: list[TraceEvent]) -> None:
        """POST a batch of events to the launcher.

        Failure handling matches :mod:`launcher_client`: any HTTP / connection
        error is logged at WARNING and swallowed. The pipeline must never
        crash because the dashboard isn't reachable.
        """
        body = {
            "session_id": self._session_id,
            "type": "trace",
            "events": [e.to_dict() for e in batch],
        }
        try:
            async with (
                aiohttp.ClientSession(timeout=self._http_timeout) as session,
                session.post(self._launcher_url, json=body) as resp,
            ):
                if resp.status >= 400:
                    text = await resp.text()
                    logger.debug(
                        "[trace] POST {} events -> {} {}",
                        len(batch),
                        resp.status,
                        text[:200],
                    )
        except Exception as exc:  # noqa: BLE001
            # Common in dev when serve.py is restarted while the bot keeps
            # running. Stay quiet at debug level — INFO would spam the logs.
            logger.debug("[trace] forwarder POST failed: {}", exc)
