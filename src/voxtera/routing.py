"""Branch gates and routers for parallel STT/TTS pipelines (Daily mode).

In Daily mode every STT and TTS provider with valid credentials is built and
run as a parallel branch. ``STTRouter`` / ``TTSRouter`` listen for browser
messages and flip per-branch ``Gate.active`` flags so only the chosen branch
sees the frames that would otherwise trigger transcription / synthesis.

The corresponding output gates also block any audio/transcript frames that
the inactive branch's already-in-flight work might still emit, so a provider
switch never produces overlapping audio (the source of the previous "echo"
bug surfaced through the demo page).

The :class:`TTSGate` keeps a small pair of counters (audio-passed,
audio-leaked) that surface in the logs and make the dual-branch issue
trivially observable if it ever resurfaces.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

try:
    from pipecat.transports.daily.transport import DailyInputTransportMessageFrame
except Exception:  # daily-python not available on Windows
    DailyInputTransportMessageFrame = None  # type: ignore[assignment,misc]

from voxtera.stt import _STT_BUILDERS
from voxtera.tts import _TTS_BUILDERS
from voxtera.tunables import update_current as _update_knob_current


def _schedule_branch_transition(coro) -> None:
    """Fire-and-forget an async lazy-connect call from sync code.

    ``STTRouter._activate`` is sync but ``lazy_connect``/``lazy_disconnect``
    on lazy STT services are async (they open/close WebSockets). We
    schedule them on the running event loop if one exists, otherwise drop
    the coroutine — at boot time before the pipeline has started there's
    no loop and no STT to connect to either.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    loop.create_task(coro)


class STTGate(FrameProcessor):
    """Branch gate that drops STT-relevant frames when the branch is inactive.

    Each STT branch in the parallel pipeline is bracketed by two gates:

    * an *input* gate (kind="input") that drops :class:`InputAudioRawFrame`
      **and** the VAD speaking frames so the inactive STT receives no audio
      and never triggers a transcription cycle. This matters in particular
      for :class:`SegmentedSTTService` subclasses (e.g. Whisper) which would
      otherwise flush an empty audio buffer on every ``VADUserStoppedSpeakingFrame``
      and burn one API call per utterance.
    * an *output* gate (kind="output") that drops
      :class:`TranscriptionFrame` and :class:`InterimTranscriptionFrame` so
      stale transcripts from the inactive STT never leak downstream.

    All other frames (lifecycle, control, system, settings, errors, etc.)
    pass through regardless of the active state — pipecat needs those for
    pipeline coordination, and ``STTUpdateSettingsFrame`` must reach the
    active STT (each STT filters by ``frame.service`` itself).
    """

    def __init__(self, *, kind: str, label: str) -> None:
        if kind not in ("input", "output"):
            raise ValueError(f"STTGate kind must be 'input' or 'output', got {kind!r}")
        super().__init__()
        self._kind = kind
        self._label = label
        # Toggled atomically by STTRouter. Default off so an unrouted gate
        # never leaks frames; STTRouter activates the chosen branch on init.
        self.active: bool = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not self.active:
            if self._kind == "input" and isinstance(
                frame,
                InputAudioRawFrame | VADUserStartedSpeakingFrame | VADUserStoppedSpeakingFrame,
            ):
                return
            if self._kind == "output" and isinstance(
                frame, TranscriptionFrame | InterimTranscriptionFrame
            ):
                return
        await self.push_frame(frame, direction)


class STTRouter(FrameProcessor):
    """Routes runtime STT-provider selection to gated parallel branches.

    Listens to ``{type: 'voxtera-stt', provider: '...'}`` from the browser
    and flips the input/output gates of the matching branch on, while
    turning all others off. Switching is instantaneous (a couple of
    boolean writes) so audio never has to wait for STT services to
    rebuild or reconnect.
    """

    def __init__(
        self,
        branches: dict[str, dict],
        *,
        initial: str,
    ) -> None:
        super().__init__()
        if not branches:
            raise RuntimeError("STTRouter requires at least one branch")
        if initial not in branches:
            initial = next(iter(branches))
        self._branches = branches
        self._active: str = initial
        self._activate(initial)
        logger.info(
            "[stt-router] available providers={}, initial={!r}",
            list(branches.keys()),
            initial,
        )

    @property
    def active_provider(self) -> str:
        return self._active

    @property
    def active_stt(self) -> FrameProcessor:
        return self._branches[self._active]["stt"]

    def set_active(self, name: str) -> None:
        """Public switching API. Used by both the trace-tune endpoint and the
        ``voxtera-stt`` app-message handler in :meth:`process_frame`.

        Raises ``ValueError`` if ``name`` isn't a built branch. On a real
        change, also mirrors the new value into TunablesRegistry so the
        dashboard's session_providers panel reflects the live state instead
        of the boot-time default.
        """
        if name not in self._branches:
            raise ValueError(f"unknown STT branch: {name!r}")
        if name != self._active:
            logger.info("[stt-router] set_active {!r} → {!r}", self._active, name)
            self._activate(name)
        # Always mirror — covers the case where the registry got out of sync
        # with reality via some other path. Idempotent on no-change.
        _update_knob_current("stt_provider", name)

    def _activate(self, name: str) -> None:
        for prov, branch in self._branches.items():
            on = prov == name
            was_on = branch["input_gate"].active
            branch["input_gate"].active = on
            branch["output_gate"].active = on
            stt = branch["stt"]
            # Just deactivated: reset segmented-STT state so any audio that
            # was buffered mid-utterance (and never got a VAD-stopped because
            # the gate is now closed) doesn't get prepended to the next
            # activation's first transcription. Streaming STTs don't have
            # this buffer so the hasattr checks make this a no-op for them.
            if was_on and not on:
                buf = getattr(stt, "_audio_buffer", None)
                if buf is not None and hasattr(buf, "clear"):
                    buf.clear()
                if hasattr(stt, "_user_speaking"):
                    stt._user_speaking = False
                # Lazy-disconnect hook: streaming STTs that subclass with a
                # lazy_disconnect() method (currently only Gladia, to dodge
                # its 1-session free-tier cap) close their WebSocket here.
                # No-op for default STTs.
                lazy_dc = getattr(stt, "lazy_disconnect", None)
                if callable(lazy_dc):
                    _schedule_branch_transition(lazy_dc())
            elif not was_on and on:
                # Lazy-connect hook: opens the streaming STT's WebSocket
                # only when this branch becomes active. No-op for default
                # STTs (their WebSocket was already opened on StartFrame
                # at boot, which is fine for providers without a session
                # cap).
                lazy_c = getattr(stt, "lazy_connect", None)
                if callable(lazy_c):
                    _schedule_branch_transition(lazy_c())
        self._active = name

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("type") == "voxtera-stt":
                provider = msg.get("provider", "")
                if provider not in self._branches:
                    if provider in _STT_BUILDERS:
                        logger.warning(
                            "[stt-router] {!r} requested but not configured "
                            "(missing credentials) — staying on {!r}",
                            provider,
                            self._active,
                        )
                    else:
                        logger.warning("[stt-router] unknown provider {!r}, ignoring", provider)
                else:
                    # Funnel through set_active so the registry is mirrored
                    # in one place. set_active is a no-op on no-change.
                    self.set_active(provider)
        await self.push_frame(frame, direction)


class TTSGate(FrameProcessor):
    """Branch gate that drops TTS-relevant frames when the branch is inactive.

    Mirrors :class:`STTGate` but for the TTS half of the pipeline.

    * an *input* gate (kind="input") drops the text frames that would cause
      the inactive TTS to synthesize: :class:`TextFrame`,
      :class:`LLMFullResponseStartFrame`, :class:`LLMFullResponseEndFrame`,
      :class:`TTSSpeakFrame`. This is what prevents the inactive provider
      from billing API calls.
    * an *output* gate (kind="output") drops the audio/text frames the
      inactive TTS has produced from prior in-flight work:
      :class:`TTSAudioRawFrame`, :class:`TTSStartedFrame`,
      :class:`TTSStoppedFrame`, :class:`TTSTextFrame`. This prevents stale
      audio bytes from leaking to the transport mid-switch.

    All other frames (lifecycle, control, system, settings, errors, etc.)
    pass through regardless of the active state, so ``TTSUpdateSettingsFrame``
    can reach the active TTS (each TTS filters by ``frame.service``).
    """

    def __init__(self, *, kind: str, label: str) -> None:
        if kind not in ("input", "output"):
            raise ValueError(f"TTSGate kind must be 'input' or 'output', got {kind!r}")
        super().__init__()
        self._kind = kind
        self._label = label
        self.active: bool = False
        # Diagnostic counters — incremented on every audio frame passed
        # (when active) or dropped (when inactive). Useful for spotting cases
        # where two TTS branches both emit audio = perceived echo.
        self._audio_passed = 0
        self._audio_leaked = 0

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not self.active:
            if self._kind == "input" and isinstance(
                frame,
                TextFrame | LLMFullResponseStartFrame | LLMFullResponseEndFrame | TTSSpeakFrame,
            ):
                logger.debug(
                    "[tts-gate:{}] dropping {} (inactive, dir={})",
                    self._label,
                    type(frame).__name__,
                    direction,
                )
                return
            if self._kind == "output" and isinstance(
                frame,
                TTSAudioRawFrame | TTSStartedFrame | TTSStoppedFrame | TTSTextFrame,
            ):
                # An inactive output gate dropping audio is the safety net
                # that prevents two TTS branches from both reaching the
                # transport. Log if it actually fires — it means the inactive
                # branch is still synthesizing despite its closed input gate.
                if isinstance(frame, TTSAudioRawFrame):
                    self._audio_leaked += 1
                    if self._audio_leaked == 1 or self._audio_leaked % 50 == 0:
                        logger.warning(
                            "[tts-gate:{}] BLOCKED TTSAudioRawFrame from inactive branch "
                            "(count={}); inactive TTS is still synthesizing — would have "
                            "caused echo if not blocked",
                            self._label,
                            self._audio_leaked,
                        )
                return
            # Trace any non-blocked frame passing through an inactive gate so we
            # can identify unexpected frame types if TTS synthesis leaks through.
            logger.trace(
                "[tts-gate:{}] passing {} through inactive gate (dir={})",
                self._label,
                type(frame).__name__,
                direction,
            )
        else:
            # Active branch — count audio frames so we can compare totals
            # per branch. If both branches show non-zero _audio_passed for
            # the same utterance, that's the echo source.
            if self._kind == "output" and isinstance(frame, TTSAudioRawFrame):
                self._audio_passed += 1
                if self._audio_passed == 1 or self._audio_passed % 100 == 0:
                    logger.info(
                        "[tts-gate:{}] passed TTSAudioRawFrame (count={}) — branch is active",
                        self._label,
                        self._audio_passed,
                    )
        await self.push_frame(frame, direction)


class TTSRouter(FrameProcessor):
    """Routes runtime TTS-provider selection to gated parallel branches.

    Listens for ``{type: 'voxtera-tts-provider', provider: 'openai'|'google'}``
    from the browser and flips gates instantaneously, mirroring
    :class:`STTRouter`.
    """

    def __init__(self, branches: dict[str, dict], *, initial: str) -> None:
        super().__init__()
        if not branches:
            raise RuntimeError("TTSRouter requires at least one branch")
        if initial not in branches:
            initial = next(iter(branches))
        self._branches = branches
        self._active: str = initial
        self._activate(initial)
        logger.info(
            "[tts-router] available providers={}, initial={!r}",
            list(branches.keys()),
            initial,
        )

    @property
    def active_provider(self) -> str:
        return self._active

    @property
    def active_tts(self) -> FrameProcessor:
        return self._branches[self._active]["tts"]

    def set_active(self, name: str) -> None:
        """Public switching API. Used by both the trace-tune endpoint and the
        ``voxtera-tts-provider`` app-message handler in :meth:`process_frame`.

        On a real change, also mirrors the new value into TunablesRegistry so
        the dashboard's session_providers panel reflects the live state.
        """
        if name not in self._branches:
            raise ValueError(f"unknown TTS branch: {name!r}")
        if name != self._active:
            logger.info("[tts-router] set_active {!r} → {!r}", self._active, name)
            self._activate(name)
        _update_knob_current("tts_provider", name)

    def _activate(self, name: str) -> None:
        for prov, branch in self._branches.items():
            on = prov == name
            branch["input_gate"].active = on
            branch["output_gate"].active = on
        self._active = name

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("type") == "voxtera-tts-provider":
                provider = msg.get("provider", "")
                if provider not in self._branches:
                    if provider in _TTS_BUILDERS:
                        logger.warning(
                            "[tts-router] {!r} requested but not configured "
                            "(missing credentials) — staying on {!r}",
                            provider,
                            self._active,
                        )
                    else:
                        logger.warning("[tts-router] unknown provider {!r}, ignoring", provider)
                else:
                    # Funnel through set_active so the registry is mirrored
                    # in one place. set_active is a no-op on no-change.
                    self.set_active(provider)
        await self.push_frame(frame, direction)
