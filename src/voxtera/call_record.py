"""Per-call conversation record — one self-contained record per voice call.

All state lives on the call's :class:`~voxtera.call_context.CallContext`,
so one process can host many concurrent calls (the WhatsApp service) without
mixing records. The module-level functions are facades that resolve the
active context; single-call processes (Daily, local CLI) never activate a
context and transparently use the process-wide default. Per call, this
module collects:

- **call metadata**: session id, hotel, transport, providers, start/end time,
  duration, the set of languages the guest used, interruption count, per-turn
  counts, LLM token usage, and total characters the bot synthesised (a proxy
  for Google Chirp 3 HD billing, which counts every character incl. spaces);
- **a turn-by-turn transcript**: every guest utterance and every bot reply,
  each with an ISO-8601 UTC timestamp, speaker role, detected language and —
  for bot turns — the reply latency;
- **the call audio**: a stereo WAV (guest on the left channel, bot on the
  right) captured by :class:`CallAudioRecorder`.

Everything for one call lands in ``logs/calls/<session_id>/``::

    record.json      metadata header + turns
    recording.wav    stereo 16 kHz call audio

``record.json`` is rewritten atomically after every turn, so a usable record
survives even if the process is force-killed — the Daily on-demand path
``os._exit()``s a few seconds after the guest hangs up.

This is the building block for the post-call CRM summary webhook: a finished
``record.json`` is everything the summariser needs.

Wiring:
- :func:`init_call` is called once from ``voxtera.bot.run_bot`` before the
  pipeline starts; :func:`finalize` is called from its ``finally`` block.
- :func:`record_user_turn` is called by ``TranscriptStageTimer`` (and the
  keyboard loop) when a guest utterance is transcribed.
- :func:`record_bot_turn`, :func:`record_usage` and :func:`record_interruption`
  are called by ``PipelineTracer`` as the bot replies.
- :class:`CallAudioRecorder` is appended to the pipeline after
  ``transport.output()`` and writes ``recording.wav`` when the call ends.
"""

from __future__ import annotations

import json
import threading
import wave
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from loguru import logger
from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame, StartFrame
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voxtera import call_context as _call_context
from voxtera.call_context import CallContext

# ``logs/`` lives at the repo root — two parents up from ``src/voxtera/``.
# Matches the directory ``conversation_logger`` uses for its JSONL files.
_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_DEFAULT_CALLS_DIR = _LOG_DIR / "calls"

# Audio is captured at 16 kHz mono-per-channel: ample for speech intelligibility
# and roughly a third the size of the 48 kHz WebRTC transport rate. Two channels
# keeps the guest and the bot on separate sides (left/right) so the recording is
# speaker-separated and ready for review or diarisation.
RECORDING_SAMPLE_RATE = 16000
RECORDING_CHANNELS = 2

# Bumped when the record.json shape changes, so downstream consumers (the CRM
# summary webhook, the admin dashboard) can branch on an explicit version.
SCHEMA_VERSION = 1


def write_wav(path: Path, pcm: bytes, *, sample_rate: int, num_channels: int) -> float:
    """Write 16-bit signed PCM bytes to a WAV file.

    Args:
        path: Destination ``.wav`` path. Parent directories are created.
        pcm: Raw little-endian 16-bit PCM samples (interleaved if multi-channel).
        sample_rate: Sample rate in Hz.
        num_channels: 1 for mono, 2 for stereo.

    Returns:
        The duration of the written audio in seconds.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(num_channels)
        wav_file.setsampwidth(2)  # 16-bit samples
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    bytes_per_frame = 2 * max(num_channels, 1)
    frame_count = len(pcm) // bytes_per_frame
    return frame_count / sample_rate if sample_rate else 0.0


class CallRecord:
    """Mutable, thread-safe record of a single call.

    One instance == one call. The instance lives on the call's
    :class:`~voxtera.call_context.CallContext`; the module-level functions
    below are thin facades that resolve the active context. Every mutator
    takes the lock and rewrites ``record.json`` atomically (temp file +
    ``os.replace``), so a process killed mid-call still leaves a coherent
    record on disk.

    When :meth:`start` is never called (the feature is disabled), every
    mutator is a no-op and nothing is written.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        """Create an empty record.

        Args:
            base_dir: Directory under which ``<session_id>/`` call folders are
                created. Defaults to ``logs/calls``. Overridable for tests.
        """
        self._lock = threading.RLock()
        self._base_dir = base_dir or _DEFAULT_CALLS_DIR

        self._enabled = False
        self._session_id: str | None = None
        self._hotel_id: str | None = None
        self._bot_name = "Voxtera"
        self._transport_mode = "local"
        self._providers: dict[str, str] = {}

        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None

        self._turn_counter = 0
        self._turns: list[dict] = []
        self._languages: set[str] = set()

        self._interruptions = 0
        self._user_turns = 0
        self._bot_turns = 0

        # LLM token usage, summed across every turn of the call.
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cache_read_tokens = 0
        self._cache_creation_tokens = 0

        # Total characters the bot synthesised — a proxy for Chirp 3 HD billing.
        self._bot_char_total = 0

        self._audio_file: str | None = None
        self._audio_duration_secs: float | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(
        self,
        *,
        enabled: bool,
        session_id: str,
        hotel_id: str | None,
        bot_name: str,
        transport_mode: str,
        providers: dict[str, str],
    ) -> None:
        """Begin recording a call. Idempotent — a second call is ignored."""
        with self._lock:
            if self._started_at is not None:
                return
            self._enabled = enabled
            self._session_id = session_id
            self._hotel_id = hotel_id
            self._bot_name = bot_name
            self._transport_mode = transport_mode
            self._providers = dict(providers)
            self._started_at = datetime.now(tz=UTC)
            if not enabled:
                logger.debug("[call-record] disabled — not recording call {}", session_id)
                return
            logger.info("[call-record] recording call {} -> {}", session_id, self.call_dir())
            self._write_locked()

    def finalize(self) -> None:
        """Stamp the end time and write the final ``record.json``.

        Safe to call when the feature is disabled or was never started.
        """
        with self._lock:
            if not self._enabled or self._started_at is None:
                return
            self._ended_at = datetime.now(tz=UTC)
            self._write_locked()
            logger.info(
                "[call-record] call {} finalised — {} turns, {} languages, "
                "{} interruptions, {} bot chars, duration {:.0f}s",
                self._session_id,
                len(self._turns),
                len(self._languages),
                self._interruptions,
                self._bot_char_total,
                self._duration_secs() or 0.0,
            )

    # ------------------------------------------------------------------ #
    # Mutators                                                           #
    # ------------------------------------------------------------------ #

    def add_user_turn(self, *, text: str, language: str = "") -> None:
        """Record a transcribed guest utterance as a transcript turn."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            if not self._enabled:
                return
            self._turn_counter += 1
            self._user_turns += 1
            lang = (language or "").strip()
            if lang:
                self._languages.add(lang)
            self._turns.append(
                {
                    "turn_id": self._turn_counter,
                    "role": "user",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "text": text,
                    "language": lang or None,
                    "latency_ms": None,
                }
            )
            self._write_locked()

    def add_bot_turn(self, *, text: str, latency_ms: float | None = None) -> None:
        """Record a bot reply as a transcript turn."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            if not self._enabled:
                return
            self._turn_counter += 1
            self._bot_turns += 1
            self._bot_char_total += len(text)
            self._turns.append(
                {
                    "turn_id": self._turn_counter,
                    "role": "bot",
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "text": text,
                    "language": None,
                    "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
                }
            )
            self._write_locked()

    def add_interruption(self) -> None:
        """Increment the barge-in counter (a guest cutting the bot off)."""
        with self._lock:
            if not self._enabled:
                return
            self._interruptions += 1
            self._write_locked()

    def add_usage(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> None:
        """Accumulate one turn's LLM token usage into the call totals."""
        with self._lock:
            if not self._enabled:
                return
            self._prompt_tokens += prompt_tokens or 0
            self._completion_tokens += completion_tokens or 0
            self._cache_read_tokens += cache_read_tokens or 0
            self._cache_creation_tokens += cache_creation_tokens or 0
            self._write_locked()

    def set_audio(self, *, file_name: str, duration_secs: float) -> None:
        """Register the saved WAV recording on the record."""
        with self._lock:
            if not self._enabled:
                return
            self._audio_file = file_name
            self._audio_duration_secs = round(duration_secs, 2)
            self._write_locked()

    # ------------------------------------------------------------------ #
    # Accessors                                                          #
    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        """True once :meth:`start` has been called with ``enabled=True``."""
        return self._enabled

    def call_dir(self) -> Path | None:
        """Return the ``logs/calls/<session_id>/`` directory, or None."""
        if self._session_id is None:
            return None
        return self._base_dir / self._session_id

    def snapshot(self) -> dict:
        """Return the full record as a JSON-serialisable dict."""
        with self._lock:
            return self._snapshot_locked()

    # ------------------------------------------------------------------ #
    # Internals (must be called while holding the lock)                  #
    # ------------------------------------------------------------------ #

    def _duration_secs(self) -> float | None:
        """Wall-clock call duration in seconds, or None before any activity."""
        if self._started_at is None:
            return None
        end = self._ended_at
        if end is None and self._turns:
            # Mid-call: fall back to the most recent turn's timestamp.
            try:
                end = datetime.fromisoformat(self._turns[-1]["timestamp"])
            except (ValueError, KeyError):
                end = None
        if end is None:
            return None
        return (end - self._started_at).total_seconds()

    def _snapshot_locked(self) -> dict:
        audio = None
        if self._audio_file is not None:
            audio = {
                "file": self._audio_file,
                "duration_secs": self._audio_duration_secs,
                "sample_rate": RECORDING_SAMPLE_RATE,
                "channels": RECORDING_CHANNELS,
                "layout": "left=guest, right=bot",
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self._session_id,
            "hotel_id": self._hotel_id,
            "bot_name": self._bot_name,
            "transport_mode": self._transport_mode,
            "providers": dict(self._providers),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "ended_at": self._ended_at.isoformat() if self._ended_at else None,
            "duration_secs": self._duration_secs(),
            "languages": sorted(self._languages),
            "metrics": {
                "user_turns": self._user_turns,
                "bot_turns": self._bot_turns,
                "interruptions": self._interruptions,
                "bot_character_total": self._bot_char_total,
                "llm_prompt_tokens": self._prompt_tokens,
                "llm_completion_tokens": self._completion_tokens,
                "llm_cache_read_tokens": self._cache_read_tokens,
                "llm_cache_creation_tokens": self._cache_creation_tokens,
            },
            "audio": audio,
            "turns": list(self._turns),
        }

    def _write_locked(self) -> None:
        """Atomically (re)write ``record.json``. Never raises."""
        call_dir = self.call_dir()
        if call_dir is None:
            return
        try:
            call_dir.mkdir(parents=True, exist_ok=True)
            target = call_dir / "record.json"
            with NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=call_dir, suffix=".tmp"
            ) as tmp:
                json.dump(self._snapshot_locked(), tmp, ensure_ascii=False, indent=2, default=str)
                tmp.write("\n")
                tmp_path = Path(tmp.name)
            tmp_path.replace(target)
        except OSError as exc:
            # Disk full / permission error must not crash the voice loop.
            logger.warning("[call-record] could not write record.json: {}", exc)


# ---------------------------------------------------------------------- #
# Module-level API — thin facades over the ACTIVE CallContext's record,    #
# mirroring the call style of ``conversation_logger``. Single-call paths   #
# (Daily/local) resolve to the process-wide default context and behave     #
# exactly as the old module singleton did; multi-call services (WhatsApp)  #
# activate a per-call context and these facades become call-local.         #
# ---------------------------------------------------------------------- #


def _active_record() -> CallRecord:
    """The active call's :class:`CallRecord` (explicit context or default)."""
    return _call_context.get_active().record


def _resolve_session_id() -> str:
    """Pick the canonical session id for this call.

    Prefers the launcher-assigned ``VOXTERA_SESSION_ID`` (so a call's record
    folder matches the id the launcher and trace dashboard use); falls back to
    the per-process UUID ``conversation_logger`` already generates.
    """
    from voxtera import launcher_client

    if launcher_client.SESSION_ID:
        return launcher_client.SESSION_ID
    from voxtera.conversation_logger import SESSION_ID

    return SESSION_ID


def init_call(
    *,
    enabled: bool,
    hotel_id: str | None,
    bot_name: str,
    transport_mode: str,
    stt_provider: str,
    tts_provider: str,
    llm_model: str,
    context: CallContext | None = None,
) -> None:
    """Begin recording a call. Call once at bot startup.

    Args:
        context: The call's context. When omitted, the active context is
            used (Daily/local single-call paths), and its session id is
            resolved the legacy way (launcher env var or per-process UUID).
    """
    ctx = context or _call_context.get_active()
    if not ctx.session_id:
        ctx.session_id = _resolve_session_id()
    ctx.record.start(
        enabled=enabled,
        session_id=ctx.session_id,
        hotel_id=hotel_id,
        bot_name=bot_name,
        transport_mode=transport_mode,
        providers={"stt": stt_provider, "tts": tts_provider, "llm": llm_model},
    )


def finalize(context: CallContext | None = None) -> None:
    """Stamp the end time and write the final record. Call once at shutdown."""
    (context or _call_context.get_active()).record.finalize()


def record_user_turn(*, text: str, language: str = "") -> None:
    """Record a transcribed guest utterance."""
    _active_record().add_user_turn(text=text, language=language)


def record_bot_turn(*, text: str, latency_ms: float | None = None) -> None:
    """Record a bot reply."""
    _active_record().add_bot_turn(text=text, latency_ms=latency_ms)


def record_interruption() -> None:
    """Record a barge-in (guest cutting the bot off mid-reply)."""
    _active_record().add_interruption()


def record_usage(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> None:
    """Accumulate one turn's LLM token usage."""
    _active_record().add_usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )


def get_call_dir() -> Path | None:
    """Return the active call's directory, or None if not recording."""
    return _active_record().call_dir()


def is_enabled() -> bool:
    """Return True if call recording is active for the current call."""
    return _active_record().enabled


async def flush_audio(context: CallContext | None = None) -> None:
    """Force the audio recorder to write its buffer to disk.

    Call this from the bot's shutdown path when the pipeline may have already
    been cancelled and EndFrame will never reach the recorder naturally.
    Safe to call when recording is disabled or the recorder was never started.
    """
    recorder = (context or _call_context.get_active()).audio_recorder
    if recorder is not None and recorder._auto_started:
        try:
            await recorder.stop_recording()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[call-record] flush_audio failed: {}", exc)


class CallAudioRecorder(AudioBufferProcessor):
    """Records the whole call to a stereo WAV.

    Placed at the tail of the pipeline (after ``transport.output()``), it sees
    both the guest's input audio and the bot's output audio. Recording starts
    automatically on the pipeline ``StartFrame``; the base class stops it on
    ``EndFrame``/``CancelFrame`` and fires ``on_audio_data``, which writes the
    WAV and registers it on the active :class:`CallRecord`.

    Guest audio is the left channel and bot audio the right
    (``num_channels=2``), so the recording is speaker-separated. With
    ``buffer_size=0`` the base class buffers the whole call in memory and emits
    one ``on_audio_data`` event when recording stops — fine for demo-length
    calls; a streaming-to-disk writer would be the next step for very long
    calls.
    """

    def __init__(self, context: CallContext | None = None) -> None:
        super().__init__(
            sample_rate=RECORDING_SAMPLE_RATE,
            num_channels=RECORDING_CHANNELS,
            buffer_size=0,
        )
        self._auto_started = False
        self.add_event_handler("on_audio_data", self._on_audio_data)
        # Bind to the call's context so concurrent calls can't mix WAVs, and
        # register for the shutdown flush path.
        self._context = context or _call_context.get_active()
        self._context.audio_recorder = self

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        """Start recording on the first ``StartFrame``; delegate the rest."""
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame) and not self._auto_started:
            self._auto_started = True
            await self.start_recording()
            logger.info("[call-record] audio recording started")

    async def _on_audio_data(
        self,
        _processor: AudioBufferProcessor,
        audio: bytes,
        sample_rate: int,
        num_channels: int,
    ) -> None:
        """Write the buffered call audio to ``recording.wav``."""
        if not audio:
            logger.warning("[call-record] no audio captured — skipping WAV")
            return
        call_dir = self._context.record.call_dir()
        if call_dir is None:
            logger.warning("[call-record] call not initialised — skipping WAV")
            return
        try:
            wav_path = call_dir / "recording.wav"
            duration = write_wav(
                wav_path, audio, sample_rate=sample_rate, num_channels=num_channels
            )
            self._context.record.set_audio(file_name=wav_path.name, duration_secs=duration)
            logger.info("[call-record] audio saved: {} ({:.1f}s)", wav_path, duration)
        except (OSError, wave.Error) as exc:
            logger.error("[call-record] failed to write WAV: {}", exc)


# ---------------------------------------------------------------------- #
# Raw browser input recorder                                              #
# ---------------------------------------------------------------------- #


class RawInputRecorder(FrameProcessor):
    """Captures the raw InputAudioRawFrame stream from the browser/transport.

    Placed immediately after ``transport.input()`` in the pipeline, it buffers
    every audio frame the browser sends (before denoising, VAD, STT) and writes
    it as a mono 16 kHz WAV (``input_raw.wav``) into the call's log folder on
    shutdown.

    This lets you hear *exactly* what arrived from the browser — useful for
    diagnosing STT issues, noise problems, or transport glitches.
    """

    def __init__(self, sample_rate: int = 16000, context: CallContext | None = None) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._chunks: list[bytes] = []
        self._started = False
        self._context = context or _call_context.get_active()
        self._context.raw_input_recorder = self

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if not self._started:
                self._started = True
            self._chunks.append(frame.audio)
        elif isinstance(frame, StartFrame):
            self._started = True
        elif isinstance(frame, EndFrame | CancelFrame):
            self._write_wav()
        await self.push_frame(frame, direction)

    def _write_wav(self) -> None:
        """Write buffered audio to input_raw.wav. Safe to call multiple times."""
        if not self._chunks:
            logger.debug("[call-record] raw input: no audio chunks to write")
            return
        call_dir = self._context.record.call_dir()
        if call_dir is None:
            logger.warning("[call-record] raw input: call not initialised — skipping")
            return
        try:
            pcm = b"".join(self._chunks)
            wav_path = call_dir / "input_raw.wav"
            duration = write_wav(wav_path, pcm, sample_rate=self._sample_rate, num_channels=1)
            logger.info("[call-record] raw input saved: {} ({:.1f}s)", wav_path, duration)
            # Clear buffer so a second call to _write_wav is a no-op.
            self._chunks.clear()
        except (OSError, wave.Error) as exc:
            logger.error("[call-record] failed to write input_raw.wav: {}", exc)

    def flush(self) -> None:
        """Synchronous flush — call from force-exit or finally blocks."""
        self._write_wav()


async def flush_raw_input(context: CallContext | None = None) -> None:
    """Force-write the raw input buffer to disk. Non-fatal if not started."""
    recorder = (context or _call_context.get_active()).raw_input_recorder
    if recorder is not None and recorder._started:
        recorder.flush()


# ---------------------------------------------------------------------- #
# Per-stage audio taps (diagnostic).                                      #
# ---------------------------------------------------------------------- #


class StageRecorder(FrameProcessor):
    """Tap the InputAudioRawFrame stream at one pipeline stage.

    A generalised :class:`RawInputRecorder`: drop one of these after any
    processor in the mic-side chain and it buffers every audio frame it sees,
    writing it to ``stage_<label>.wav`` in the call's log folder on shutdown.

    The point is diagnostic A/B comparison. The pre-gate stages
    (pre-emphasis → RNNoise → leakage-guard → audio-monitor) all *pass every
    frame through* — the leakage guard zeroes frames rather than dropping
    them — so every stage's WAV is the same length and overlays sample-for-
    sample on the same clock. Compare them and the first stage whose WAV
    shows a silence gap that the previous stage did not is the stage that
    introduced the break.

    Do NOT rely on this downstream of the STT gate: the gate *drops* frames
    rather than zeroing them, so its recording would be shorter and no longer
    time-aligned with the pre-gate stages.
    """

    def __init__(
        self, label: str, sample_rate: int = 16000, context: CallContext | None = None
    ) -> None:
        super().__init__()
        self._label = label
        self._sample_rate = sample_rate
        self._chunks: list[bytes] = []
        self._started = False
        self._context = context or _call_context.get_active()
        self._context.stage_recorders.append(self)

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._started = True
            self._chunks.append(frame.audio)
        elif isinstance(frame, StartFrame):
            self._started = True
        elif isinstance(frame, EndFrame | CancelFrame):
            self._write_wav()
        await self.push_frame(frame, direction)

    def _write_wav(self) -> None:
        """Write buffered audio to stage_<label>.wav. Safe to call repeatedly."""
        if not self._chunks:
            logger.debug("[stage-record:{}] no audio chunks to write", self._label)
            return
        call_dir = self._context.record.call_dir()
        if call_dir is None:
            logger.warning("[stage-record:{}] call not initialised — skipping", self._label)
            return
        try:
            pcm = b"".join(self._chunks)
            wav_path = call_dir / f"stage_{self._label}.wav"
            duration = write_wav(wav_path, pcm, sample_rate=self._sample_rate, num_channels=1)
            logger.info("[stage-record:{}] saved: {} ({:.1f}s)", self._label, wav_path, duration)
            self._chunks.clear()
        except (OSError, wave.Error) as exc:
            logger.error("[stage-record:{}] failed to write WAV: {}", self._label, exc)

    def flush(self) -> None:
        """Synchronous flush — call from force-exit or finally blocks."""
        self._write_wav()


async def flush_stage_recorders(context: CallContext | None = None) -> None:
    """Force-write every stage recorder's buffer. Non-fatal if none started."""
    for rec in (context or _call_context.get_active()).stage_recorders:
        if rec._started:
            rec.flush()
