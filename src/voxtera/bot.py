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
    VAD_STOP_SECS       seconds of silence before VAD ends a turn (0.8 default)
    LOG_LEVEL           DEBUG | INFO | WARNING | ERROR
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
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
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voxtera.config import Settings, load_settings
from voxtera.prompts import SYSTEM_PROMPT, resolve_greeting

# Default models. Change here (or factor to env vars) if you want to tune.
LLM_MODEL = "claude-haiku-4-5-20251001"  # fast; swap to claude-sonnet-4-5 for quality
STT_MODEL = "whisper-1"  # OpenAI Whisper API
TTS_MODEL = "tts-1"  # tts-1 is faster; tts-1-hd is higher quality


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

    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label
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


def configure_logging(level: str) -> None:
    """Configure loguru to write to stderr at the given level."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
    )


def build_pipeline(settings: Settings) -> tuple[PipelineTask, PipelineRunner]:
    """Construct the Pipecat pipeline and return a runnable task + runner."""
    mic_enabled = settings.input_mode in ("voice", "hybrid")

    # In Pipecat 1.0 the transport's vad_* params are dead code. VAD must be
    # an explicit pipeline step (VADProcessor) that emits
    # VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame. The STT
    # service listens for those to know when to commit audio to the API.
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
    vad_processor = (
        VADProcessor(
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
        if mic_enabled
        else None
    )

    stt = (
        OpenAISTTService(
            api_key=settings.openai_api_key,
            settings=OpenAISTTService.Settings(model=STT_MODEL),
        )
        if mic_enabled
        else None
    )

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
        processors.extend([AudioLevelMonitor(), vad_processor, stt])
    processors.append(context_aggregator.user())

    # RAG: optionally inject hotel knowledge before the LLM sees the context.
    if settings.rag_enabled:
        from voxtera.rag.injector import RAGContextInjector
        from voxtera.rag.retriever import Retriever
        from voxtera.rag.store import ChunksStore

        default_db = str(Path.home() / ".voxtera" / "voxtera.db")
        db_path = Path(os.environ.get("VOXTERA_DB_PATH", default_db))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = ChunksStore(db_path)
        store.init_schema()
        retriever = Retriever(store, api_key=settings.openai_api_key)
        rag_injector = RAGContextInjector(retriever, hotel_id=settings.hotel_id)
        processors.append(rag_injector)
        logger.info("[rag] enabled for hotel_id={!r}", settings.hotel_id)

    processors.extend(
        [
            llm,
            tts,
            PipelineTracer("voxtera"),
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
    )

    runner = PipelineRunner(handle_sigint=True)
    return task, runner


async def _keyboard_input_loop(task: PipelineTask) -> None:
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
        keyboard_task = asyncio.create_task(_keyboard_input_loop(task))

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
    logger.info("Models — LLM: {} | STT: {} | TTS: {}", LLM_MODEL, STT_MODEL, TTS_MODEL)
    logger.info(
        "VAD: stop={}s start={}s min_volume={} confidence={} | TTS voice: {}",
        settings.vad_stop_secs,
        settings.vad_start_secs,
        settings.vad_min_volume,
        settings.vad_confidence,
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
