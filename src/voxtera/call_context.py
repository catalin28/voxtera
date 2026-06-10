"""Per-call context — the single owner of all per-call mutable state.

Voxtera has two process models:

- **Daily / local**: one call == one subprocess (spawned by ``serve.py``'s
  ``/api/start-session``). Process-global state is safe there.
- **WhatsApp**: ONE long-lived process answers MANY calls. Anything
  process-global (the old ``call_record`` singleton, ``VOXTERA_SESSION_ID``
  env mutation, the trace ``TurnTracker``) mixes state across concurrent
  calls: two simultaneous callers used to share WAVs, transcripts, and trace
  session ids.

:class:`CallContext` fixes this by owning every piece of per-call state:

- the :class:`~voxtera.call_record.CallRecord` (transcript + metadata),
- the trace :class:`~voxtera.trace.TurnTracker` (turn ids + timing anchors),
- references to the audio recorders that need flushing on shutdown.

The active context is tracked with a :class:`contextvars.ContextVar`.
``asyncio`` copies the context into every task at creation, so a context
activated at the top of ``run_call_bot()`` is automatically visible to every
pipeline task that call spawns — and invisible to other concurrent calls.

The legacy single-call paths need no changes: when no context was activated,
:func:`get_active` falls back to a lazily-created process-wide default
context, which behaves exactly like the old module-level singletons.

Usage (multi-call service, e.g. the WhatsApp call bot)::

    ctx = new_call_context(session_id=sid, channel="wa")
    activate(ctx)              # current asyncio task + children see ctx
    ...                        # build pipeline, run call
    deactivate()               # optional; task teardown also isolates it
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voxtera.call_record import CallAudioRecorder, CallRecord, RawInputRecorder, StageRecorder
    from voxtera.trace import TurnTracker


@dataclass
class CallContext:
    """All mutable state for one voice call.

    Create via :func:`new_call_context` (which wires up the record and
    tracker) rather than directly.
    """

    session_id: str
    hotel_id: str | None = None
    # Channel tag used by the trace dashboard: "daily", "wa", "local", …
    channel: str = "daily"
    record: CallRecord = None  # type: ignore[assignment]  # set by new_call_context
    tracker: TurnTracker = None  # type: ignore[assignment]  # set by new_call_context

    # Recorders register themselves here so the shutdown / force-exit path can
    # flush exactly this call's buffers (and nobody else's).
    audio_recorder: CallAudioRecorder | None = None
    raw_input_recorder: RawInputRecorder | None = None
    stage_recorders: list[StageRecorder] = field(default_factory=list)

    def call_dir(self) -> Path | None:
        """This call's ``logs/calls/<session_id>/`` directory, or None."""
        return self.record.call_dir() if self.record is not None else None


def new_call_context(
    *,
    session_id: str,
    hotel_id: str | None = None,
    channel: str = "daily",
    base_dir: Path | None = None,
) -> CallContext:
    """Build a fresh :class:`CallContext` with its own record and tracker.

    Args:
        session_id: Filesystem-safe id; becomes the ``logs/calls/<id>/`` folder.
        hotel_id: Hotel scope, if any.
        channel: Trace channel tag ("daily", "wa", "local", …).
        base_dir: Override the calls directory (tests).
    """
    # Imported lazily to avoid an import cycle (call_record/trace import us).
    from voxtera.call_record import CallRecord
    from voxtera.trace import TurnTracker

    return CallContext(
        session_id=session_id,
        hotel_id=hotel_id,
        channel=channel,
        record=CallRecord(base_dir=base_dir),
        tracker=TurnTracker(),
    )


# The active per-call context. ``None`` means "no multi-call service activated
# a context" — i.e. the classic one-call-per-process paths.
_current: ContextVar[CallContext | None] = ContextVar("voxtera_call_context", default=None)

# Lazily-created fallback for the single-call-per-process paths (Daily, local
# CLI). Exactly one call lives in those processes, so one shared default
# context reproduces the old singleton behaviour bit-for-bit.
_default: CallContext | None = None


def current() -> CallContext | None:
    """Return the explicitly-activated context, or None."""
    return _current.get()


def get_active() -> CallContext:
    """Return the active context, falling back to the process-wide default.

    This is the resolution every module-level facade uses: explicit context
    (multi-call services) wins; otherwise the default keeps legacy behaviour.
    """
    ctx = _current.get()
    if ctx is not None:
        return ctx
    return _get_default()


def _get_default() -> CallContext:
    global _default
    if _default is None:
        from voxtera.call_record import CallRecord
        from voxtera.trace import TurnTracker

        # The default context's session id is resolved later by
        # ``call_record.init_call`` (launcher-assigned id or per-process UUID);
        # an empty placeholder is fine until then.
        _default = CallContext(
            session_id="",
            record=CallRecord(),
            tracker=TurnTracker(),
        )
    return _default


def activate(ctx: CallContext) -> None:
    """Make ``ctx`` the active context for the current task and its children."""
    _current.set(ctx)


def deactivate() -> None:
    """Clear the active context for the current task."""
    _current.set(None)


def reset_default_for_tests() -> None:
    """Drop the process-wide default context (test isolation only)."""
    global _default
    _default = None
