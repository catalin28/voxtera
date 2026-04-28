"""Voxtera local voice loop (VOX-6).

Pipeline:

    Microphone
        -> LocalAudioTransport (in)
        -> Silero VAD (turn-taking + interruption, stop_secs configurable)
        -> Whisper STT  (OpenAI API; auto language detection)
        -> Claude LLM   (Haiku for low latency; system prompt locks language)
        -> OpenAI TTS   (tts-1, configurable voice; placeholder until VOX-E3)
        -> LocalAudioTransport (out)
    Speakers

Run with `make run` (which is `uv run python -m voxtera.bot`).

Tuning knobs all live in `.env` / `voxtera.config.Settings`:
    DEFAULT_TTS_VOICE   nova | alloy | echo | fable | onyx | shimmer
    VAD_STOP_SECS       seconds of silence before VAD ends a turn (0.2 default)
    RNNOISE_ENABLED     true/false mic denoiser before VAD (demo option)
    LOG_LEVEL           DEBUG | INFO | WARNING | ERROR
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer

try:
    from pyrnnoise import RNNoise
except Exception:  # pragma: no cover - optional dependency at runtime
    RNNoise = None
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    FatalErrorFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    LLMTextFrame,
    STTUpdateSettingsFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.whisper.base_stt import Transcription
from pipecat.transports.daily.transport import (
    DailyInputTransportMessageFrame,
    DailyOutputTransportMessageFrame,
    DailyParams,
    DailyTransport,
)
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voxtera.config import Settings, load_settings
from voxtera.conversation_logger import log_bot_reply, log_user_query
from voxtera.prompts import SYSTEM_PROMPT, resolve_greeting

# Default models. Change here (or factor to env vars) if you want to tune.
LLM_MODEL = "claude-haiku-4-5-20251001"  # fast; swap to claude-sonnet-4-5 for quality
STT_MODEL_WHISPER = "whisper-1"  # OpenAI Whisper API
STT_MODEL_DEEPGRAM = "nova-3-general"  # Deepgram Nova-3 multilingual (47+ languages)
TTS_MODEL = "tts-1"  # tts-1 is faster; tts-1-hd is higher quality


class _MultilingualWhisperSTT(OpenAISTTService):
    """Whisper STT with language auto-detection (omits the language param).

    Uses verbose_json to capture the detected language for downstream
    logging and consistency checks.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.last_detected_language: str | None = None

    async def _transcribe(self, audio: bytes) -> Transcription:
        kwargs: dict = {
            "file": ("audio.wav", audio, "audio/wav"),
            "model": self._settings.model,
        }
        if self._settings.prompt is not None:
            kwargs["prompt"] = self._settings.prompt
        if self._settings.temperature is not None:
            kwargs["temperature"] = self._settings.temperature
        kwargs["response_format"] = "verbose_json"
        result = await self._client.audio.transcriptions.create(**kwargs)
        detected_lang = getattr(result, "language", None)
        if detected_lang:
            self.last_detected_language = detected_lang
            logger.info("[stt] detected language: {}", detected_lang)
        return result


def _build_stt(settings: Settings) -> tuple[FrameProcessor, bool]:
    """Factory: build the STT service based on STT_PROVIDER config.

    Returns (stt_service, needs_vad) — Deepgram has built-in VAD so
    Silero VAD is not needed when using it.

    Supported providers:
        - whisper: OpenAI Whisper API (batch, auto language detection)
        - deepgram: Deepgram Nova-3 streaming (multilingual, native VAD)
    """
    provider = settings.stt_provider

    if provider == "deepgram":
        if not settings.deepgram_api_key:
            raise RuntimeError("STT_PROVIDER=deepgram requires DEEPGRAM_API_KEY to be set.")
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt = DeepgramSTTService(
            api_key=settings.deepgram_api_key,
            ttfs_p99_latency=0.8,
            settings=DeepgramSTTService.Settings(
                model=STT_MODEL_DEEPGRAM,
                language="multi",
                endpointing=300,  # ms silence before Deepgram finalizes
                interim_results=True,
            ),
        )
        # Attach a compatible interface for TranscriptionNoiseFilter.
        stt.last_detected_language = None  # type: ignore[attr-defined]
        logger.info("[stt] provider=deepgram model={}", STT_MODEL_DEEPGRAM)
        return stt, True  # use Silero VAD for turn detection

    if provider == "whisper":
        stt = _MultilingualWhisperSTT(
            api_key=settings.openai_api_key,
            settings=OpenAISTTService.Settings(
                model=STT_MODEL_WHISPER,
                prompt=settings.stt_prompt,
                temperature=0.0,
            ),
        )
        logger.info("[stt] provider=whisper model={}", STT_MODEL_WHISPER)
        return stt, True  # Whisper needs external Silero VAD

    raise RuntimeError(f"Unknown STT_PROVIDER={provider!r}. Supported: whisper, deepgram")


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9']+", text.lower())


def _repetition_ratio(tokens: list[str]) -> float:
    if not tokens:
        return 0.0
    unique = len(set(tokens))
    return 1.0 - (unique / len(tokens))


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(a, b) / denom)


class TranscriptionNoiseFilter(FrameProcessor):
    """Generic transcription filter with no hardcoded phrase/domain lists.

    Uses structural text signals + semantic similarity to the system prompt
    embedding. This avoids brittle word/phrase allow/deny lists.
    """

    def __init__(self, stt: FrameProcessor | None = None) -> None:
        super().__init__()
        self._stt = stt
        self._domain_vec: np.ndarray | None = None
        self._embed_sync = None
        self._last_text = ""
        self._last_text_at = 0.0
        # Language consistency tracking: detect suspicious switches on short
        # utterances which are almost always Whisper hallucinations.
        self._prev_language: str | None = None
        self._consistent_lang_turns: int = 0

        try:
            from voxtera.rag.embeddings import embed_sync

            self._embed_sync = embed_sync
            vec = np.asarray(embed_sync([SYSTEM_PROMPT])[0], dtype=np.float32)
            self._domain_vec = vec
            logger.info("[stt-filter] semantic mode enabled")
        except Exception as exc:
            logger.warning("[stt-filter] semantic mode unavailable (fallback heuristics): {}", exc)

    def _is_low_signal_noise(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return True

        tokens = _tokenize(t)
        n_tokens = len(tokens)
        if n_tokens == 0:
            return True

        sentence_like = t.count(".") + t.count("?") + t.count("!")
        punct_density = sentence_like / max(n_tokens, 1)
        rep = _repetition_ratio(tokens)

        # Generic noisy-narration profile (no phrase list): long + repetitive + sentence-heavy.
        if n_tokens >= 30 and rep >= 0.35:
            return True
        return sentence_like >= 3 and n_tokens >= 18 and punct_density >= 0.10 and rep >= 0.25

    def _is_semantically_relevant(self, text: str) -> float | None:
        if self._domain_vec is None or self._embed_sync is None:
            return None
        try:
            v = np.asarray(self._embed_sync([text])[0], dtype=np.float32)
            return _cosine_similarity(v, self._domain_vec)
        except Exception:
            return None

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if not text:
                return

            logger.debug("[stt-filter] raw transcription: {!r}", text)

            # Whisper has well-known hallucinations from YouTube/podcast training
            # data that appear on silent/unclear audio. Drop them unconditionally.
            normalized = re.sub(r"[^\w\s]", "", text.lower()).strip()
            whisper_hallucinations = {
                "thank you",
                "thanks for watching",
                "thank you for watching",
                "thanks for listening",
                "thank you so much",
                "subscribe to my channel",
                "like and subscribe",
                "you",
                "bye",
                "okay",
                "ok",
                # Prompt-echo: Whisper sometimes regurgitates its own prompt
                # when audio is too short or unclear.
                "do not paraphrase or invent words",
                "transcribe exactly what the user says",
                "detect the language automatically",
                "hotel guest speaking",
                "multiple languages possible",
                "hotel concierge conversation",
            }
            if normalized in whisper_hallucinations:
                logger.warning("[stt-filter] dropped known Whisper hallucination: {!r}", text)
                return

            # Deduplicate transcript bursts often produced by noisy overlaps.
            now = time.monotonic()
            if self._last_text:
                similarity = SequenceMatcher(None, self._last_text, text).ratio()
                if similarity >= 0.92 and (now - self._last_text_at) <= 2.0:
                    logger.debug("[stt-filter] dropped near-duplicate transcription")
                    return

            if self._is_low_signal_noise(text):
                logger.warning("[stt-filter] dropped low-signal transcription")
                return

            sim = self._is_semantically_relevant(text)
            if sim is not None:
                token_count = len(_tokenize(text))
                rep = _repetition_ratio(_tokenize(text))
                # Short hallucinations (1-4 words) with low relevance — classic
                # Whisper artifacts from noise bursts ("SHINY!", "Love that.").
                if sim < 0.25 and token_count <= 4:
                    logger.warning(
                        "[stt-filter] dropped short hallucination (sim={:.3f}): {!r}",
                        sim,
                        text,
                    )
                    return
                # Very low semantic relevance + narration shape => likely background media.
                if sim < 0.10 and token_count >= 8:
                    logger.warning(
                        "[stt-filter] dropped low-relevance transcription (sim={:.3f})",
                        sim,
                    )
                    return
                if sim < 0.14 and rep >= 0.40 and token_count >= 6:
                    logger.warning("[stt-filter] dropped repetitive low-relevance transcription")
                    return

            # Language consistency guard: if the user has been speaking one
            # language for several turns and the STT suddenly detects a
            # different language on a short utterance (≤5 words), it's almost
            # certainly a mis-detection. Drop the frame.
            detected_lang = (
                getattr(self._stt, "last_detected_language", None) if self._stt else None
            )
            if detected_lang:
                token_count_lang = len(_tokenize(text))
                if self._prev_language and detected_lang != self._prev_language:
                    if token_count_lang <= 5 and self._consistent_lang_turns >= 2:
                        logger.warning(
                            "[stt-filter] dropped likely language mis-detection: "
                            "prev={}, detected={}, text={!r} ({} tokens, {} consistent turns)",
                            self._prev_language,
                            detected_lang,
                            text,
                            token_count_lang,
                            self._consistent_lang_turns,
                        )
                        return
                    # Longer utterance or not enough history — accept the switch.
                    self._prev_language = detected_lang
                    self._consistent_lang_turns = 1
                else:
                    self._prev_language = detected_lang
                    self._consistent_lang_turns += 1

            self._last_text = text
            self._last_text_at = now
        await self.push_frame(frame, direction)


class LLMRunGuard(FrameProcessor):
    """Drop orphan LLMRunFrame events that are not tied to a recent user append."""

    def __init__(self, max_age_secs: float = 3.0, min_run_interval_secs: float = 2.5) -> None:
        super().__init__()
        self._max_age_secs = max_age_secs
        self._min_run_interval_secs = min_run_interval_secs
        self._last_user_append_at: float | None = None
        self._last_run_sent_at: float | None = None

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMMessagesAppendFrame):
            messages = getattr(frame, "messages", None) or []
            has_user_content = any(
                isinstance(msg, dict)
                and msg.get("role") == "user"
                and isinstance(msg.get("content"), str)
                and msg.get("content", "").strip()
                for msg in messages
            )
            if has_user_content:
                self._last_user_append_at = time.monotonic()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMRunFrame):
            now = time.monotonic()

            # Refractory window: suppress rapid run storms from noisy VAD churn.
            if (
                self._last_run_sent_at is not None
                and (now - self._last_run_sent_at) < self._min_run_interval_secs
            ):
                logger.debug("[llm-run-guard] dropped LLMRunFrame in refractory window")
                return

            if (
                self._last_user_append_at is None
                or (now - self._last_user_append_at) > self._max_age_secs
            ):
                logger.debug("[llm-run-guard] dropped orphan LLMRunFrame")
                return
            # Consume one run per append to avoid duplicate runs from noisy turns.
            self._last_user_append_at = None
            self._last_run_sent_at = now
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)


class AudioLevelMonitor(FrameProcessor):
    """Diagnostic processor that logs mic RMS at DEBUG level only.

    Quiet by default — when LOG_LEVEL=INFO this contributes zero output.
    Flip to DEBUG when troubleshooting "why isn't the bot hearing me?".
    """

    def __init__(self) -> None:
        super().__init__()
        self._frame_count = 0
        self._peak = 0.0

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.audio:
            samples = np.frombuffer(frame.audio, dtype=np.int16)
            if samples.size:
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0
                self._peak = max(self._peak, rms)
                self._frame_count += 1
                # Roughly once every 5s of audio at 50 frames/sec.
                if self._frame_count % 250 == 0:
                    logger.debug(
                        "[audio] RMS={:.4f} peak={:.4f}",
                        rms,
                        self._peak,
                    )
        await self.push_frame(frame, direction)


class PlaybackLeakageGuard(FrameProcessor):
    """Reduce mic audio while bot is speaking to avoid false barge-in.

    In noisy rooms (or with speaker leakage), VAD can trigger while TTS is
    playing, which interrupts the bot mid-answer. This processor ducks mic
    energy during bot speech so accidental interruptions are less likely,
    without requiring environment-specific tuning.
    """

    def __init__(self, allow_interruptions: bool = False) -> None:
        super().__init__()
        self._allow_interruptions = allow_interruptions
        self._bot_speaking = False
        self._bot_thinking = False
        self._cooldown_until = 0.0
        self._barge_in_open = False
        self._barge_in_frames = 0

        # Adaptive baseline for ambient/noise leakage level.
        self._noise_floor = 0.005
        self._noise_floor_alpha = 0.02

        # Auto-tuned gate settings (no env knobs required).
        self._open_ratio = 3.5
        # Built-in laptop mics often peak around 0.05-0.07 RMS for speech.
        # Keep threshold below that so intentional barge-in can open.
        self._min_open_rms = 0.045
        self._required_open_frames = 8  # ~160ms at 20ms frames
        self._post_tts_cooldown_secs = 0.25

    @staticmethod
    def _clone_audio_frame(frame: InputAudioRawFrame, audio_bytes: bytes) -> InputAudioRawFrame:
        output = InputAudioRawFrame(
            audio=audio_bytes,
            sample_rate=frame.sample_rate,
            num_channels=frame.num_channels,
        )
        output.pts = frame.pts
        output.metadata = dict(frame.metadata)
        output.transport_source = frame.transport_source
        output.transport_destination = frame.transport_destination
        output.broadcast_sibling_id = frame.broadcast_sibling_id
        return output

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._barge_in_open = False
            self._barge_in_frames = 0
            logger.debug("[leakage-guard] BotStartedSpeaking → bot_speaking=True")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._barge_in_open = False
            self._barge_in_frames = 0
            self._cooldown_until = now + self._post_tts_cooldown_secs
            logger.debug("[leakage-guard] BotStoppedSpeaking → bot_speaking=False")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            # Treat thinking as bot-active for leakage suppression; otherwise
            # background speech can cancel the in-flight reply and yield <empty>.
            self._bot_thinking = True
            self._barge_in_open = False
            self._barge_in_frames = 0
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            self._bot_thinking = False
            await self.push_frame(frame, direction)
            return

        # Some transports/services emit TTS lifecycle more reliably than bot
        # speaking frames. Handle both so state never gets stuck.
        if isinstance(frame, TTSStartedFrame):
            self._bot_speaking = True
            self._barge_in_open = False
            self._barge_in_frames = 0
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TTSStoppedFrame):
            self._bot_speaking = False
            self._barge_in_open = False
            self._barge_in_frames = 0
            self._cooldown_until = now + self._post_tts_cooldown_secs
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InputAudioRawFrame) and frame.audio:
            samples = np.frombuffer(frame.audio, dtype=np.int16)
            if not samples.size:
                await self.push_frame(frame, direction)
                return

            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0
            bot_active = self._bot_speaking or self._bot_thinking

            # Track ambient baseline when the bot is not speaking.
            if not bot_active and now >= self._cooldown_until:
                if rms < 0.12:
                    self._noise_floor = (
                        1.0 - self._noise_floor_alpha
                    ) * self._noise_floor + self._noise_floor_alpha * rms
                await self.push_frame(frame, direction)
                return

            # With interruptions enabled, do not gate mic audio here. This
            # guarantees user barge-in reaches VAD promptly.
            if self._allow_interruptions:
                await self.push_frame(frame, direction)
                return

            # While bot is speaking (or just stopped), only allow intentional
            # near-field barge-in that's clearly above leakage/noise floor.
            open_threshold = max(self._noise_floor * self._open_ratio, self._min_open_rms)

            if not self._allow_interruptions:
                # Strict mode: when interruptions are disabled, never open a
                # barge-in gate while bot is active/cooldown.
                silent = np.zeros_like(samples, dtype=np.int16)
                output = self._clone_audio_frame(frame, silent.tobytes())
                await self.push_frame(output, direction)
                return

            if rms >= open_threshold:
                self._barge_in_frames += 1
            else:
                self._barge_in_frames = max(0, self._barge_in_frames - 1)

            if self._barge_in_frames >= self._required_open_frames:
                if not self._barge_in_open:
                    logger.info(
                        "[barge-in] opened gate (rms={:.4f}, floor={:.4f}, threshold={:.4f})",
                        rms,
                        self._noise_floor,
                        open_threshold,
                    )
                self._barge_in_open = True

            if self._barge_in_open:
                await self.push_frame(frame, direction)
                return

            # Suppress likely playback leakage/background speech before VAD.
            silent = np.zeros_like(samples, dtype=np.int16)
            output = self._clone_audio_frame(frame, silent.tobytes())
            await self.push_frame(output, direction)
            return

        await self.push_frame(frame, direction)


class BotActiveUserFrameSuppressor(FrameProcessor):
    """Drop user-turn frames while bot is generating/speaking.

    In noisy environments, residual VAD/STT events can still appear while a
    bot response is in flight. If interruptions are disabled, these frames
    should not create/cancel turns; swallow them until the bot is idle.
    """

    def __init__(self, allow_interruptions: bool = False) -> None:
        super().__init__()
        self._allow_interruptions = allow_interruptions
        self._bot_active = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame | BotStartedSpeakingFrame | TTSStartedFrame):
            self._bot_active = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame | BotStoppedSpeakingFrame | TTSStoppedFrame):
            self._bot_active = False
            await self.push_frame(frame, direction)
            return

        if self._allow_interruptions:
            await self.push_frame(frame, direction)
            return

        if self._bot_active and isinstance(
            frame,
            VADUserStartedSpeakingFrame
            | VADUserStoppedSpeakingFrame
            | InterimTranscriptionFrame
            | TranscriptionFrame,
        ):
            logger.debug(
                "[barge-in] dropped user frame while bot active: {}",
                frame.__class__.__name__,
            )
            return

        await self.push_frame(frame, direction)


class RNNoiseDenoiser(FrameProcessor):
    """Apply RNNoise denoising on mic audio before VAD/STT."""

    def __init__(self, sample_rate: int = 16000) -> None:
        super().__init__()
        if RNNoise is None:
            raise RuntimeError(
                "RNNoise requested but 'pyrnnoise' is not installed. Run `uv sync` first."
            )
        self._sample_rate = sample_rate
        self._denoiser = RNNoise(sample_rate=sample_rate)
        self._disabled = False
        # Keep speech intelligibility first: if RNNoise output collapses
        # compared with the original frame, bypass denoising for that frame.
        self._min_input_rms_for_guard = 0.01
        self._suppression_guard_ratio = 0.18
        # Blend a little original signal back in to reduce metallic artifacts.
        self._dry_mix = 0.35

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if self._disabled or not isinstance(frame, InputAudioRawFrame) or not frame.audio:
            await self.push_frame(frame, direction)
            return

        if frame.sample_rate != self._sample_rate:
            logger.warning(
                "[rnnoise] bypassing frame: expected {}Hz, got {}Hz",
                self._sample_rate,
                frame.sample_rate,
            )
            await self.push_frame(frame, direction)
            return

        try:
            samples = np.frombuffer(frame.audio, dtype=np.int16)
            if samples.size == 0:
                await self.push_frame(frame, direction)
                return

            if frame.num_channels > 1:
                channel_audio = samples.reshape(-1, frame.num_channels).T
            else:
                channel_audio = samples.reshape(1, -1)

            input_rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0
            denoised_chunks: list[np.ndarray] = []
            for _, denoised in self._denoiser.denoise_chunk(channel_audio, partial=False):
                denoised_np = np.asarray(denoised, dtype=np.int16)
                if denoised_np.ndim == 1:
                    denoised_chunks.append(denoised_np)
                else:
                    denoised_chunks.append(denoised_np.T.reshape(-1))

            if not denoised_chunks:
                await self.push_frame(frame, direction)
                return

            denoised_audio = np.concatenate(denoised_chunks).astype(np.int16, copy=False)

            # Keep output length stable frame-to-frame to avoid timing drift.
            if denoised_audio.size != samples.size:
                if denoised_audio.size > samples.size:
                    denoised_audio = denoised_audio[: samples.size]
                else:
                    denoised_audio = np.pad(
                        denoised_audio,
                        (0, samples.size - denoised_audio.size),
                        mode="constant",
                    )

            denoised_rms = (
                float(np.sqrt(np.mean(denoised_audio.astype(np.float32) ** 2))) / 32768.0
                if denoised_audio.size
                else 0.0
            )

            # Guardrail: if denoiser crushes energy on likely-speech frames,
            # pass through original audio so STT doesn't lose words.
            if (
                input_rms >= self._min_input_rms_for_guard
                and denoised_rms < input_rms * self._suppression_guard_ratio
            ):
                logger.debug(
                    "[rnnoise] bypass frame: over-suppressed (in={:.4f}, out={:.4f})",
                    input_rms,
                    denoised_rms,
                )
                await self.push_frame(frame, direction)
                return

            # Wet/dry mix keeps consonants clearer while still lowering noise.
            mixed = (1.0 - self._dry_mix) * denoised_audio.astype(
                np.float32
            ) + self._dry_mix * samples.astype(np.float32)
            denoised_audio = np.clip(mixed, -32768.0, 32767.0).astype(np.int16)

            output = InputAudioRawFrame(
                audio=denoised_audio.tobytes(),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )
            output.pts = frame.pts
            output.metadata = dict(frame.metadata)
            output.transport_source = frame.transport_source
            output.transport_destination = frame.transport_destination
            output.broadcast_sibling_id = frame.broadcast_sibling_id
            await self.push_frame(output, direction)
        except Exception as exc:
            logger.warning("[rnnoise] disabling denoiser after processing error: {}", exc)
            self._disabled = True
            await self.push_frame(frame, direction)


class PipelineTracer(FrameProcessor):
    """Logs the few frames that meaningfully describe a turn, plus timing.

    What you get per turn at INFO:

      [voxtera] you started speaking
      [voxtera] heard: 'Hello, can you recommend a museum in Paris?'
      [voxtera] you stopped speaking
      [voxtera] bot is thinking...
      [voxtera] bot replied (thought 0.92s): 'The Louvre is...'
      [voxtera] bot is speaking (total latency 1.34s)

    Errors are always loud (no silent failures). Uncategorised frames are
    intentionally not logged at any level.
    """

    def __init__(self, label: str, *, hotel_id: str | None = None) -> None:
        super().__init__()
        self._label = label
        self._hotel_id = hotel_id
        # Per-turn state (cleared at the start of each user turn).
        self._user_stopped_at: float | None = None
        self._llm_started_at: float | None = None
        self._llm_chunks: list[str] = []

    def _reset_turn_state(self) -> None:
        self._user_stopped_at = None
        self._llm_started_at = None
        self._llm_chunks = []

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Errors — always loud.
        if isinstance(frame, FatalErrorFrame):
            logger.error("[{}] FATAL: {}", self._label, getattr(frame, "error", frame))
        elif isinstance(frame, ErrorFrame):
            logger.error("[{}] error: {}", self._label, getattr(frame, "error", frame))

        # Turn boundaries from VAD
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._reset_turn_state()
            logger.info("[{}] you started speaking", self._label)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_stopped_at = time.monotonic()
            logger.info("[{}] you stopped speaking", self._label)

        # What was heard
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.info("[{}] heard: {!r}", self._label, text)
                log_user_query(user_query=text, hotel_id=self._hotel_id)
        elif isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                logger.debug("[{}] interim: {!r}", self._label, text)

        # Bot turn lifecycle — collect chunks + measure timing
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_started_at = time.monotonic()
            self._llm_chunks = []
            logger.info("[{}] bot is thinking...", self._label)
        elif isinstance(frame, LLMTextFrame):
            chunk = frame.text or ""
            if chunk:
                self._llm_chunks.append(chunk)
        elif isinstance(frame, LLMFullResponseEndFrame):
            reply = "".join(self._llm_chunks).strip()
            think_ms = None
            if self._llm_started_at is not None:
                think_ms = (time.monotonic() - self._llm_started_at) * 1000
                logger.info(
                    "[{}] bot replied (thought {:.0f}ms): {!r}",
                    self._label,
                    think_ms,
                    reply or "<empty>",
                )
            else:
                logger.info("[{}] bot replied: {!r}", self._label, reply or "<empty>")

            # Structured conversation log for audit / evaluation.
            if reply:
                log_bot_reply(reply=reply, elapsed_ms=think_ms)

        # TTS lifecycle stays at DEBUG; latency is what matters at INFO.
        elif isinstance(frame, TTSStartedFrame):
            logger.debug("[{}] TTS started", self._label)
        elif isinstance(frame, TTSStoppedFrame):
            logger.debug("[{}] TTS stopped", self._label)

        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._user_stopped_at is not None:
                total_ms = (time.monotonic() - self._user_stopped_at) * 1000
                logger.info(
                    "[{}] bot is speaking (total latency {:.0f}ms)",
                    self._label,
                    total_ms,
                )
            else:
                # E.g. the startup greeting — no preceding user turn.
                logger.info("[{}] bot is speaking", self._label)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            logger.debug("[{}] bot stopped speaking", self._label)

        # Anything else: silent.

        await self.push_frame(frame, direction)


class UserTranscriptBroadcaster(FrameProcessor):
    """Captures user speech events early in the pipeline (before the context
    aggregator consumes them) and emits DailyOutputTransportMessageFrame
    events that flow downstream to the transport output."""

    def _evt(self, event: str, data: dict | None = None) -> DailyOutputTransportMessageFrame:
        return DailyOutputTransportMessageFrame(
            message={
                "type": "voxtera-event",
                "event": event,
                "ts": time.time(),
                "data": data or {},
            }
        )

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self.push_frame(self._evt("user-started"), FrameDirection.DOWNSTREAM)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self.push_frame(self._evt("user-stopped"), FrameDirection.DOWNSTREAM)
        elif isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if text:
                evt = self._evt("user-transcript", {"text": text})
                await self.push_frame(evt, FrameDirection.DOWNSTREAM)

        await self.push_frame(frame, direction)


# Valid Nova-3 language codes that the browser may request.
_VALID_STT_LANGUAGES: set[str] = {
    "multi",
    "ar",
    "be",
    "bn",
    "bs",
    "bg",
    "ca",
    "zh-HK",
    "zh",
    "zh-CN",
    "zh-Hans",
    "zh-TW",
    "zh-Hant",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "et",
    "fi",
    "nl-BE",
    "fr",
    "de",
    "de-CH",
    "el",
    "gu",
    "he",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "lv",
    "lt",
    "mk",
    "ms",
    "mr",
    "no",
    "fa",
    "pl",
    "pt",
    "ro",
    "ru",
    "sr",
    "sk",
    "sl",
    "es",
    "sv",
    "tl",
    "ta",
    "te",
    "th",
    "tr",
    "uk",
    "ur",
    "vi",
}


class LanguageSwitcher(FrameProcessor):
    """Listens for language-selection messages from the browser and reconfigures STT.

    When the demo page sends ``{type: 'voxtera-language', language: 'ro'}``,
    this processor pushes an ``STTUpdateSettingsFrame`` upstream to change
    the Deepgram STT language on the fly.
    """

    def __init__(self, stt: FrameProcessor) -> None:
        super().__init__()
        self._stt = stt
        self._current_language: str = "multi"

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("type") == "voxtera-language":
                lang = msg.get("language", "multi")
                if lang not in _VALID_STT_LANGUAGES:
                    logger.warning("[lang-switch] invalid language code {!r}, ignoring", lang)
                else:
                    if lang != self._current_language:
                        self._current_language = lang
                        logger.info("[lang-switch] switching STT language to {!r}", lang)
                        await self.push_frame(
                            STTUpdateSettingsFrame(settings={"language": lang}, service=self._stt),
                            FrameDirection.UPSTREAM,
                        )

        await self.push_frame(frame, direction)


class DemoEventBroadcaster(FrameProcessor):
    """Sends pipeline events to the browser via Daily's data channel.

    Pushes DailyOutputTransportMessageFrame with a JSON envelope so the
    demo page can render a live transcript and status badge.
    Only useful when transport_mode == "daily".
    """

    def __init__(self) -> None:
        super().__init__()
        self._user_stopped_at: float | None = None
        self._llm_started_at: float | None = None
        self._llm_chunks: list[str] = []

    def _evt(self, event: str, data: dict | None = None) -> DailyOutputTransportMessageFrame:
        return DailyOutputTransportMessageFrame(
            message={"type": "voxtera-event", "event": event, "ts": time.time(), "data": data or {}}
        )

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._user_stopped_at = time.monotonic()

        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_started_at = time.monotonic()
            self._llm_chunks = []
            await self.push_frame(self._evt("bot-thinking"), FrameDirection.DOWNSTREAM)

        elif isinstance(frame, LLMTextFrame):
            chunk = frame.text or ""
            if chunk:
                self._llm_chunks.append(chunk)

        elif isinstance(frame, LLMFullResponseEndFrame):
            reply = "".join(self._llm_chunks).strip()
            think_ms = None
            if self._llm_started_at is not None:
                think_ms = round((time.monotonic() - self._llm_started_at) * 1000)
            if reply:
                await self.push_frame(
                    self._evt("bot-reply", {"text": reply, "think_ms": think_ms}),
                    FrameDirection.DOWNSTREAM,
                )

        elif isinstance(frame, BotStartedSpeakingFrame):
            latency_ms = None
            if self._user_stopped_at is not None:
                latency_ms = round((time.monotonic() - self._user_stopped_at) * 1000)
            await self.push_frame(
                self._evt("bot-speaking", {"latency_ms": latency_ms}),
                FrameDirection.DOWNSTREAM,
            )

        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self.push_frame(self._evt("bot-done-speaking"), FrameDirection.DOWNSTREAM)

        # Log TTS frames for debugging
        elif isinstance(frame, TTSStartedFrame):
            logger.info("[audio-debug] TTS started")

        elif isinstance(frame, TTSStoppedFrame):
            logger.info("[audio-debug] TTS stopped")

        await self.push_frame(frame, direction)


def _eject_stale_bots(settings: Settings) -> None:
    """Remove leftover bot participants from previous runs via the Daily REST API."""
    import json

    room_name = settings.daily_room_name
    api_key = settings.daily_api_key
    base = "https://api.daily.co/v1"
    try:
        req = Request(
            f"{base}/presence",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        participants = data.get(room_name, [])
        stale_ids = [p["id"] for p in participants]
        if not stale_ids:
            return
        logger.info("[daily] found {} stale participants, ejecting all", len(stale_ids))

        ereq = Request(
            f"{base}/rooms/{room_name}/eject",
            data=json.dumps({"ids": stale_ids}).encode(),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(ereq, timeout=5) as resp:
            result = json.loads(resp.read())
        ejected = result.get("ejectedIds", [])
        for pid in ejected:
            logger.info("[daily] ejected stale bot {}", pid)
    except Exception as exc:
        logger.debug("[daily] could not check for stale bots: {}", exc)


def configure_logging(level: str) -> None:
    """Configure loguru to write to stderr at the given level."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        filter=lambda record: "Invalid RTVI transport message" not in record["message"],
    )


def build_pipeline(settings: Settings) -> tuple[PipelineTask, PipelineRunner]:
    """Construct the Pipecat pipeline and return a runnable task + runner."""
    mic_enabled = settings.input_mode in ("voice", "hybrid")

    if settings.transport_mode not in {"local", "daily"}:
        raise RuntimeError("TRANSPORT_MODE must be either 'local' or 'daily'.")

    # In Pipecat 1.0 the transport's vad_* params are dead code. VAD must be
    # an explicit pipeline step (VADProcessor) that emits
    # VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame. The STT
    # service listens for those to know when to commit audio to the API.
    if settings.transport_mode == "daily":
        missing = [
            name
            for name, value in (
                ("DAILY_API_KEY", settings.daily_api_key),
                ("DAILY_DOMAIN", settings.daily_domain),
                ("DAILY_ROOM_NAME", settings.daily_room_name),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Daily transport requires these environment variables: " + ", ".join(missing)
            )

        room_url = f"https://{settings.daily_domain}/{settings.daily_room_name}"
        _eject_stale_bots(settings)
        transport = DailyTransport(
            room_url,
            None,
            settings.bot_name,
            DailyParams(
                api_key=settings.daily_api_key,
                audio_in_enabled=mic_enabled,
                audio_in_sample_rate=16000,
                audio_in_channels=1,
                audio_in_passthrough=True,
                audio_out_enabled=True,
                audio_out_sample_rate=24000,
                audio_out_channels=1,
                camera_out_enabled=False,
                microphone_out_enabled=True,
            ),
        )
        logger.info("[daily] transport enabled for room {}", room_url)
    else:
        transport = LocalAudioTransport(
            LocalAudioTransportParams(
                audio_in_enabled=mic_enabled,
                audio_in_sample_rate=16000,  # Silero VAD requires 8kHz or 16kHz
                audio_in_channels=1,
                audio_in_passthrough=True,
                audio_out_enabled=True,
                audio_out_sample_rate=24000,
                audio_out_channels=1,
            )
        )

    # The mic-side processors only matter when audio input is enabled. In
    # text-only mode we skip building them — keeps the pipeline lean and
    # avoids loading the Silero ONNX model unnecessarily.
    stt = None
    needs_vad = True
    if mic_enabled:
        stt, needs_vad = _build_stt(settings)

    vad_processor = None
    if mic_enabled and needs_vad:
        vad_processor = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=16000,
                params=VADParams(
                    stop_secs=settings.vad_stop_secs,
                    start_secs=settings.vad_start_secs,
                    min_volume=settings.vad_min_volume,
                    confidence=settings.vad_confidence,
                ),
            )
        )
    if mic_enabled and not needs_vad:
        logger.info("[vad] external VAD skipped (STT provides its own)")

    llm = AnthropicLLMService(
        api_key=settings.anthropic_api_key,
        settings=AnthropicLLMService.Settings(model=LLM_MODEL),
    )

    tts = OpenAITTSService(
        api_key=settings.openai_api_key,
        settings=OpenAITTSService.Settings(
            model=TTS_MODEL,
            voice=settings.default_tts_voice,
        ),
    )

    # Conversation context. The system prompt does the heavy lifting on the
    # multilingual requirement — see src/voxtera/prompts/system_prompt.py.
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    context = LLMContext(messages)
    context_aggregator = LLMContextAggregatorPair(context)

    # Build the pipeline list. In text-only mode the mic-side processors
    # (audio level monitor, VAD, STT) are skipped entirely.
    processors: list = [transport.input()]
    if mic_enabled:
        if settings.rnnoise_enabled:
            if RNNoise is None:
                logger.warning(
                    "[rnnoise] enabled but pyrnnoise is unavailable; running without denoiser"
                )
            else:
                processors.append(RNNoiseDenoiser(sample_rate=16000))
                logger.info("[rnnoise] enabled")

        processors.extend(
            [
                PlaybackLeakageGuard(allow_interruptions=settings.allow_interruptions),
                AudioLevelMonitor(),
            ]
        )
        if vad_processor:
            processors.append(vad_processor)
        processors.extend(
            [
                stt,
                TranscriptionNoiseFilter(stt=stt),
                BotActiveUserFrameSuppressor(allow_interruptions=settings.allow_interruptions),
            ]
        )
        if settings.transport_mode == "daily":
            processors.append(UserTranscriptBroadcaster())
    processors.append(context_aggregator.user())
    processors.append(LLMRunGuard())

    # RAG: optionally inject hotel knowledge before the LLM sees the context.
    if settings.rag_enabled:
        # Warm up the embedding model now so the first query doesn't
        # cold-start inside the 500ms retrieval timeout.
        from voxtera.rag.embeddings import embed_sync
        from voxtera.rag.injector import RAGContextInjector
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        embed_sync(["warmup"])

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = ChunksStore(db_path)
        store.init_schema()
        retriever = Retriever(store)
        rag_injector = RAGContextInjector(retriever, hotel_id=settings.hotel_id)
        processors.append(rag_injector)
        logger.info("[rag] enabled for hotel_id={!r}", settings.hotel_id)

    processors.extend(
        [
            llm,
            PipelineTracer("voxtera", hotel_id=settings.hotel_id if settings.rag_enabled else None),
        ]
    )
    if settings.transport_mode == "daily":
        processors.append(DemoEventBroadcaster())
        if stt:
            processors.append(LanguageSwitcher(stt=stt))
    processors.extend(
        [
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=settings.allow_interruptions,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
        cancel_on_idle_timeout=settings.pipeline_idle_timeout_secs is not None,
        idle_timeout_secs=settings.pipeline_idle_timeout_secs,
    )

    runner = PipelineRunner(handle_sigint=True)
    return task, runner


async def _keyboard_input_loop(task: PipelineTask, *, hotel_id: str | None = None) -> None:
    """Background task: read lines from stdin and inject as user messages.

    Runs alongside the audio pipeline. Each line typed becomes a user turn:
    the message is appended to the LLM context and an LLMRunFrame triggers
    generation, just as if the user had spoken it. The bot's reply still
    plays through the speakers (or headphones) so this works hands-free for
    listening while typing in a quiet environment.

    Sentinel words `quit`, `exit`, `bye` end the session cleanly.
    """
    logger.info("[keyboard] type to chat. Sentinels: quit | exit | bye")
    while True:
        try:
            # Run blocking input() on a worker thread so the event loop
            # stays responsive (mic / TTS / runner all keep working).
            line = await asyncio.to_thread(input, "")
        except (EOFError, KeyboardInterrupt):
            return
        line = line.strip()
        if not line:
            continue
        if line.lower() in {"quit", "exit", "bye"}:
            logger.info("[keyboard] exit requested")
            await task.queue_frame(EndFrame())
            return
        logger.info("[voxtera] you typed: {!r}", line)
        log_user_query(user_query=line, hotel_id=hotel_id)
        await task.queue_frames(
            [
                LLMMessagesAppendFrame([{"role": "user", "content": line}]),
                LLMRunFrame(),
            ]
        )


async def run_bot(settings: Settings) -> None:
    """Build and run the voice loop until interrupted."""
    task, runner = build_pipeline(settings)

    if settings.input_mode == "text":
        logger.info("Voxtera ready (text mode — mic disabled). Type to chat. Ctrl-C to quit.")
    elif settings.input_mode == "hybrid":
        logger.info("Voxtera ready (hybrid mode — speak or type). Ctrl-C to quit.")
    else:
        logger.info("Voxtera ready. Speak into your microphone. Press Ctrl-C to quit.")

    if settings.transport_mode == "daily":
        logger.info(
            "Daily room: https://{}/{}",
            settings.daily_domain,
            settings.daily_room_name,
        )

    # Speak a localized greeting at startup. Resolution order:
    #   1. GREETING_LANGUAGE env var (e.g. "fr") if explicit
    #   2. OS locale detection (when GREETING_LANGUAGE=auto)
    #   3. English fallback for unsupported codes
    # Uses TTSSpeakFrame to bypass the LLM (faster, no token cost). Claude
    # still detects the user's spoken language on the first turn and replies
    # in that language regardless of the greeting language.
    greeting_lang, greeting_text = resolve_greeting(settings.greeting_language)
    logger.info("Greeting language: {} (preference: {})", greeting_lang, settings.greeting_language)
    await task.queue_frames([TTSSpeakFrame(text=greeting_text)])

    # Start the keyboard listener in parallel with the audio pipeline when
    # the user has asked for text or hybrid input.
    keyboard_task: asyncio.Task | None = None
    if settings.input_mode in ("text", "hybrid"):
        keyboard_task = asyncio.create_task(
            _keyboard_input_loop(task, hotel_id=settings.hotel_id if settings.rag_enabled else None)
        )

    try:
        await runner.run(task)
    except Exception:
        # Anything that escapes the runner gets logged with full traceback so
        # we never silently swallow a service error.
        logger.exception("Runner raised an unhandled exception")
        raise
    finally:
        if keyboard_task is not None and not keyboard_task.done():
            keyboard_task.cancel()
        await task.queue_frame(EndFrame())


def main() -> int:
    """Entry point. Loads settings, configures logging, runs the loop."""
    # `.env` is honoured here, at the entry point, so `voxtera.config` itself
    # stays a pure function over `os.environ` (important for tests).
    load_dotenv()

    try:
        settings = load_settings()
    except RuntimeError as exc:
        # Logger isn't configured yet; go straight to stderr.
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1

    configure_logging(settings.log_level)
    logger.info("Voxtera starting up. Bot name: {}", settings.bot_name)
    stt_model = STT_MODEL_DEEPGRAM if settings.stt_provider == "deepgram" else STT_MODEL_WHISPER
    logger.info(
        "Models — LLM: {} | STT: {} ({}) | TTS: {}",
        LLM_MODEL,
        stt_model,
        settings.stt_provider,
        TTS_MODEL,
    )
    logger.info(
        "VAD: stop={}s start={}s min_volume={} confidence={} | "
        "RNNoise: {} | Interruptions: {} | Idle timeout: {} | TTS voice: {}",
        settings.vad_stop_secs,
        settings.vad_start_secs,
        settings.vad_min_volume,
        settings.vad_confidence,
        settings.rnnoise_enabled,
        settings.allow_interruptions,
        "disabled"
        if settings.pipeline_idle_timeout_secs is None
        else f"{settings.pipeline_idle_timeout_secs}s",
        settings.default_tts_voice,
    )

    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        logger.info("Bye.")
        return 0
    except Exception:
        logger.exception("Fatal error in voice loop")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
