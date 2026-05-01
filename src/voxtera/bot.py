"""Voxtera local voice loop (VOX-6) — entry point.

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

Implementation lives in sibling modules; this file only wires them up:

    bot.py          — entry point (this file)
    pipeline.py     — :func:`build_pipeline`
    stt.py          — STT services, builders, language maps
    tts.py          — TTS builders, voice catalogs
    audio.py        — mic-side processors (denoise, leakage, monitor, filter)
    routing.py      — STT/TTS gates and routers
    controllers.py  — language / model / greeting / run-guard switchers
    observability.py — pipeline tracer + browser event broadcasters
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TTSSpeakFrame,
)

from voxtera.config import Settings, load_settings
from voxtera.controllers import LLM_MODEL
from voxtera.conversation_logger import log_user_query
from voxtera.pipeline import build_pipeline
from voxtera.prompts import resolve_greeting
from voxtera.stt import STT_MODEL_DEEPGRAM, STT_MODEL_GOOGLE, STT_MODEL_WHISPER
from voxtera.tts import TTS_MODEL


def configure_logging(level: str) -> None:
    """Configure loguru to write to stderr at the given level."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}",
        filter=lambda record: "Invalid RTVI transport message" not in record["message"],
    )


async def _keyboard_input_loop(task, *, hotel_id: str | None = None) -> None:
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
    # Build the actions runtime up front (if the feature is enabled) so the
    # same `sink`, `store`, and `directory` are shared by the pipeline AND
    # the long-running button listener task.
    action_runtime = None
    if settings.actions_enabled:
        from voxtera.actions import build_action_runtime

        try:
            action_runtime = build_action_runtime(settings.hotel_id)
        except Exception as e:
            logger.error("[actions] runtime build failed: {} — disabling actions", e)
            action_runtime = None

    task, runner = build_pipeline(settings, action_runtime=action_runtime)

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

    if settings.transport_mode != "daily":
        greeting_lang, greeting_text = resolve_greeting(settings.greeting_language)
        logger.info(
            "Greeting language: {} (preference: {})",
            greeting_lang,
            settings.greeting_language,
        )
        await task.queue_frames([TTSSpeakFrame(text=greeting_text)])

    keyboard_task: asyncio.Task | None = None
    if settings.input_mode in ("text", "hybrid"):
        keyboard_task = asyncio.create_task(
            _keyboard_input_loop(task, hotel_id=settings.hotel_id if settings.rag_enabled else None)
        )

    # Long-running listener for staff button taps in Telegram. Runs alongside
    # the voice pipeline so a tap fires a handler instantly without blocking
    # the audio loop. Uses the same store the pipeline writes tickets to.
    listener_task: asyncio.Task | None = None
    if action_runtime is not None:
        listener_task = asyncio.create_task(action_runtime.listener.run())
        logger.info("[actions] Telegram button listener started")

    try:
        await runner.run(task)
    except Exception:
        logger.exception("Runner raised an unhandled exception")
        raise
    finally:
        if keyboard_task is not None and not keyboard_task.done():
            keyboard_task.cancel()
        if action_runtime is not None and listener_task is not None:
            action_runtime.listener.stop()
            try:
                await asyncio.wait_for(listener_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                listener_task.cancel()
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
    stt_model = {
        "deepgram": STT_MODEL_DEEPGRAM,
        "google": STT_MODEL_GOOGLE,
    }.get(settings.stt_provider, STT_MODEL_WHISPER)
    logger.info(
        "Models — LLM: {} | STT: {} ({}) | TTS: {}",
        LLM_MODEL,
        stt_model,
        settings.stt_provider,
        TTS_MODEL,
    )
    logger.info(
        "VAD: stop={}s start={}s min_volume={} confidence={} | "
        "RNNoise: {} | Interruptions: {} | Idle timeout: {} | TTS voice: {} | Actions: {}",
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
        "enabled" if settings.actions_enabled else "disabled",
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
