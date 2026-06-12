"""Conversational fillers — mask LLM thinking time with the bot's own voice.

A guest stops speaking; the answer takes 2.5-3.5s to reach their ear (STT
tail + render TTFT + TTS). Humans fill that hole with voice — "mm, one
moment" — so the line never feels dead. :class:`FillerPlayer` does exactly
that:

- ARM a timer when the guest's turn ends (``VADUserStoppedSpeakingFrame``);
- if no real bot speech started within ``delay_secs`` (default 1.2s), play
  ONE pre-rendered clip in the bot's own TTS voice (chosen per detected
  language, rotated so it never repeats twice in a row);
- CANCEL instantly when real speech arrives (TTS audio / bot-started) or the
  guest starts talking again — and never fire more than once per turn.

Clips are plain 16-bit mono WAVs pre-rendered with the SAME voice the bot
speaks with (see ``scripts/generate_fillers.py``), so the filler is
indistinguishable from the bot taking a small breath before answering.
They're loaded once and pushed as ``TTSAudioRawFrame`` chunks, which keeps
every downstream behaviour consistent: the call recorder captures them as
bot audio and the leakage guard treats them as bot speech.

Env knobs (read by the call bot at wiring time):
    FILLERS_ENABLED       default "true"
    FILLER_DELAY_SECS     default "1.2"
    FILLER_DIR            default "assets/fillers" (repo-relative)
"""

from __future__ import annotations

import asyncio
import random
import wave
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    CancelFrame,
    EndFrame,
    InterruptionFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Push the clip in transport-sized chunks so playback starts immediately and
# a barge-in can cut it off mid-clip like any other bot speech. 20 ms.
_CHUNK_MS = 20

# Repo root (two parents up from src/voxtera/).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILLER_DIR = _REPO_ROOT / "assets" / "fillers"


def load_filler_clips(
    directory: Path | str = DEFAULT_FILLER_DIR,
    *,
    sample_rate: int,
) -> dict[str, list[bytes]]:
    """Load filler WAVs grouped by language subdirectory.

    Layout: ``<dir>/<lang>/*.wav`` (mono 16-bit, at the transport's output
    sample rate — ``generate_fillers.py`` produces exactly this). Files with
    a mismatched rate or channel count are skipped with a warning rather
    than resampled — a wrong-pitch filler is worse than none.
    """
    clips: dict[str, list[bytes]] = {}
    base = Path(directory)
    if not base.is_dir():
        return clips
    for lang_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for wav_path in sorted(lang_dir.glob("*.wav")):
            try:
                with wave.open(str(wav_path), "rb") as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                        logger.warning("[fillers] {} not mono/16-bit — skipped", wav_path.name)
                        continue
                    if wav.getframerate() != sample_rate:
                        logger.warning(
                            "[fillers] {} is {} Hz, transport wants {} — skipped",
                            wav_path.name,
                            wav.getframerate(),
                            sample_rate,
                        )
                        continue
                    pcm = wav.readframes(wav.getnframes())
            except (OSError, wave.Error) as e:
                logger.warning("[fillers] could not read {}: {}", wav_path.name, e)
                continue
            if pcm:
                clips.setdefault(lang_dir.name, []).append(pcm)
    if clips:
        logger.info(
            "[fillers] loaded {} clip(s): {}",
            sum(len(v) for v in clips.values()),
            {k: len(v) for k, v in clips.items()},
        )
    return clips


class FillerPlayer(FrameProcessor):
    """Plays one short voice filler when the bot's answer is slow to start.

    Place AFTER the TTS service and BEFORE ``transport.output()`` so it sees
    both the user-side VAD frames (they propagate downstream too) and the
    real TTS audio it must yield to.
    """

    def __init__(
        self,
        clips: dict[str, list[bytes]],
        *,
        sample_rate: int,
        delay_secs: float = 1.2,
        default_language: str = "en",
    ) -> None:
        super().__init__()
        self._clips = clips
        self._sample_rate = sample_rate
        self._delay_secs = delay_secs
        self._language = default_language
        self._default_language = default_language
        self._timer: asyncio.Task | None = None
        self._playing: asyncio.Task | None = None
        self._fired_this_turn = False
        self._last_clip: bytes | None = None

    # ------------------------------------------------------------------ #
    # Frame handling                                                      #
    # ------------------------------------------------------------------ #

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            # A FINAL transcript is the arming signal: it means a turn is
            # being processed and an answer IS coming. (Arming on VAD-stop —
            # the first version — fired after trailing-off "umm"s that never
            # produced a transcript: the bot said "one moment" and then had
            # nothing to follow it with.)
            lang = str(getattr(frame, "language", "") or "").strip().lower()
            if lang:
                self._language = lang.split("-")[0]
            if (frame.text or "").strip():
                self._fired_this_turn = False
                self._arm()
        elif isinstance(frame, VADUserStartedSpeakingFrame | InterruptionFrame):
            # Guest is talking (or barged in) — stop everything, including a
            # clip mid-play (the interruption also clears the transport queue).
            self._disarm(stop_playback=True)
        elif isinstance(frame, TTSStartedFrame | TTSAudioRawFrame | BotStartedSpeakingFrame):
            # Real speech arrived. Cancel a PENDING filler — but if a clip is
            # already playing, let it FINISH: the answer queues right behind
            # it, whereas truncating mid-syllable leaves a weird half-sound
            # ("Mm—") that callers can't identify.
            self._disarm(stop_playback=False)
        elif isinstance(frame, EndFrame | CancelFrame):
            self._disarm(stop_playback=True)
        elif isinstance(frame, StartFrame):
            pass

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------ #
    # Timer + playback                                                    #
    # ------------------------------------------------------------------ #

    def _arm(self) -> None:
        self._disarm(stop_playback=True)
        if not self._clips or self._fired_this_turn:
            return
        self._timer = asyncio.create_task(self._fire_after_delay())

    def _disarm(self, *, stop_playback: bool) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if stop_playback and self._playing is not None:
            self._playing.cancel()
            self._playing = None

    async def _fire_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._delay_secs)
        except asyncio.CancelledError:
            return
        if self._fired_this_turn:
            return
        self._fired_this_turn = True
        self._playing = asyncio.create_task(self._play_clip())

    def _pick_clip(self) -> bytes | None:
        pool = self._clips.get(self._language) or self._clips.get(self._default_language)
        if not pool:
            return None
        candidates = [c for c in pool if c is not self._last_clip] or pool
        clip = random.choice(candidates)
        self._last_clip = clip
        return clip

    async def _play_clip(self) -> None:
        clip = self._pick_clip()
        if clip is None:
            return
        logger.debug("[fillers] playing filler ({} bytes, lang={})", len(clip), self._language)
        chunk_bytes = int(self._sample_rate * _CHUNK_MS / 1000) * 2  # 16-bit mono
        try:
            for i in range(0, len(clip), chunk_bytes):
                await self.push_frame(
                    TTSAudioRawFrame(
                        audio=clip[i : i + chunk_bytes],
                        sample_rate=self._sample_rate,
                        num_channels=1,
                    ),
                    FrameDirection.DOWNSTREAM,
                )
                # Pace roughly in real time so a cancel (real speech arriving)
                # can stop the tail of the clip instead of it being fully
                # queued the instant the timer fires.
                await asyncio.sleep(_CHUNK_MS / 1000 / 2)
        except asyncio.CancelledError:
            pass
