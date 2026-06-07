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
import signal
import sys
import threading
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TTSSpeakFrame,
)

from voxtera import call_record
from voxtera.config import Settings, load_settings
from voxtera.controllers import LLM_MODEL
from voxtera.conversation_logger import log_user_query
from voxtera.pipeline import build_pipeline
from voxtera.prompts import daypart_for_hour, resolve_greeting
from voxtera.stt import (
    STT_MODEL_DEEPGRAM,
    STT_MODEL_ELEVENLABS,
    STT_MODEL_GLADIA,
    STT_MODEL_GOOGLE,
    STT_MODEL_WHISPER,
)
from voxtera.trace import TraceForwarder
from voxtera.trace_server import TuneServer, resolve_port
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
        # Typed turns have no STT language detection — record without one.
        call_record.record_user_turn(text=line)
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

    # Begin the per-call record: metadata header + turn-by-turn transcript,
    # written to logs/calls/<session_id>/record.json. The pipeline processors
    # populate it as the call runs (see voxtera.observability hooks); the
    # finally block below calls finalize() to stamp the end time.
    call_record.init_call(
        enabled=settings.call_recording_enabled,
        hotel_id=settings.hotel_id,
        bot_name=settings.bot_name,
        transport_mode=settings.transport_mode,
        stt_provider=settings.stt_provider,
        tts_provider=settings.tts_provider,
        llm_model=LLM_MODEL,
    )

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
        # Local mode has no browser to report the guest's timezone — but the
        # machine running the bot IS the listener, so its own clock is the
        # right clock. Greet with the matching time of day.
        local_daypart = daypart_for_hour(datetime.now().hour)
        greeting_lang, greeting_text = resolve_greeting(
            settings.greeting_language,
            daypart=local_daypart,
            hotel_name=action_runtime.hotel_config.hotel_name if action_runtime else None,
        )
        logger.info(
            "Greeting language: {} (preference: {}) daypart: {}",
            greeting_lang,
            settings.greeting_language,
            local_daypart,
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

    # Trace plane: drain the in-process TraceBus into HTTP POSTs to serve.py
    # (no-op when VOXTERA_LAUNCHER_URL is unset), and expose a localhost-only
    # tune endpoint so serve.py can forward live-tune commands from the
    # /trace.html dashboard.
    import os as _os

    from voxtera import launcher_client as _launcher_client

    trace_forwarder = TraceForwarder(
        launcher_url=_launcher_client.LAUNCHER_URL,
        session_id=_launcher_client.SESSION_ID,
    )
    await trace_forwarder.start()

    tune_server: TuneServer | None = None
    trace_enabled = _os.environ.get("VOXTERA_TRACE_ENABLED", "true").lower() not in (
        "0",
        "false",
        "no",
    )
    if trace_enabled:
        tune_server = TuneServer(
            port=resolve_port(),
            session_id=_launcher_client.SESSION_ID,
        )
        try:
            await tune_server.start()

            # Register a speak callback so the launcher's watchdog can
            # announce session timeout via the bot's TTS before disconnecting.
            def _speak_via_pipeline(text: str) -> None:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.ensure_future(
                        task.queue_frames([InterruptionFrame(), TTSSpeakFrame(text=text)])
                    )
                )

            tune_server.register_speak_callback(_speak_via_pipeline)

        except OSError as exc:
            # Port already in use (another bot still running, or VOXTERA_BOT_PORT
            # collision). Log and continue — the bot still runs, just no live
            # tuning.
            logger.warning("[trace-server] could not bind: {} — live tuning disabled", exc)
            tune_server = None

    # Install a SIGTERM handler so the launcher's terminate() triggers a
    # graceful shutdown through the finally block (flush audio + finalize)
    # rather than an abrupt process death that loses the recording.
    loop = asyncio.get_running_loop()
    _sigterm_received = False
    _shutdown_timer: threading.Timer | None = None

    def _force_flush_and_exit() -> None:
        import asyncio as _aio
        import os as _os2

        try:
            _loop = _aio.new_event_loop()
            _loop.run_until_complete(call_record.flush_audio())
            _loop.run_until_complete(call_record.flush_raw_input())
            _loop.run_until_complete(call_record.flush_stage_recorders())
            _loop.close()
            call_record.finalize()
            logger.info("[shutdown] force flush completed — exiting")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[shutdown] force flush failed: {}", exc)
        _os2._exit(0)

    def _handle_sigterm() -> None:
        nonlocal _sigterm_received, _shutdown_timer
        if _sigterm_received:
            # Second SIGTERM — force flush and exit immediately.
            logger.warning("[shutdown] second SIGTERM — forcing flush + exit")
            if _shutdown_timer:
                _shutdown_timer.cancel()
            threading.Thread(target=_force_flush_and_exit, daemon=True).start()
            return
        _sigterm_received = True
        logger.info("[shutdown] SIGTERM received — requesting graceful stop (5s watchdog)")
        asyncio.ensure_future(task.queue_frame(EndFrame()))
        # Watchdog: if pipeline doesn't shut down in 5s, force flush and exit.
        # EndFrame often gets stuck in LLM/TTS processors.
        _shutdown_timer = threading.Timer(5.0, _force_flush_and_exit)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)

    try:
        await runner.run(task)
    except Exception:
        logger.exception("Runner raised an unhandled exception")
        raise
    finally:
        # Cancel the watchdog — we made it to the finally block normally.
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
        if keyboard_task is not None and not keyboard_task.done():
            keyboard_task.cancel()
        if action_runtime is not None and listener_task is not None:
            action_runtime.listener.stop()
            try:
                await asyncio.wait_for(listener_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                listener_task.cancel()
        if tune_server is not None:
            try:
                await tune_server.stop()
            except Exception:  # noqa: BLE001
                logger.exception("[trace-server] stop failed")
        try:
            await trace_forwarder.stop()
        except Exception:  # noqa: BLE001
            logger.exception("[trace] forwarder stop failed")
        # Try to drain the pipeline gracefully via EndFrame so the audio
        # recorder flushes through the normal path.
        try:
            await task.queue_frame(EndFrame())
        except Exception:  # noqa: BLE001
            logger.debug("[shutdown] EndFrame could not be queued")
        # Force-flush the audio buffer if it hasn't been written yet.
        # stop_recording() is idempotent: if EndFrame already triggered the
        # flush through the pipeline, this is a harmless no-op.
        try:
            await call_record.flush_audio()
        except Exception:  # noqa: BLE001
            logger.warning("[shutdown] flush_audio failed")
        # Flush raw browser input recording.
        try:
            await call_record.flush_raw_input()
        except Exception:  # noqa: BLE001
            logger.warning("[shutdown] flush_raw_input failed")
        # Flush per-stage diagnostic recordings (no-op unless STAGE_AUDIO_DEBUG).
        try:
            await call_record.flush_stage_recorders()
        except Exception:  # noqa: BLE001
            logger.warning("[shutdown] flush_stage_recorders failed")
        # Always stamp the end time and write the final record.json.
        call_record.finalize()


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
        "gladia": STT_MODEL_GLADIA,
        "elevenlabs": STT_MODEL_ELEVENLABS,
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
