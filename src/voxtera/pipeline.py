"""Pipeline assembly.

:func:`build_pipeline` is the only public symbol — it wires every processor
described in the sibling modules into a Pipecat :class:`Pipeline` and returns
a runnable :class:`PipelineTask` plus its :class:`PipelineRunner`.

Two transports are supported:

- ``transport_mode='local'`` — single STT, single TTS, no parallel branches.
  Used for the CLI demo and tests.
- ``transport_mode='daily'`` — every STT/TTS provider with valid creds is
  built and runs as a parallel branch behind the routers in
  :mod:`voxtera.routing`. The browser flips the active branch via
  ``voxtera-stt`` / ``voxtera-tts-provider`` app-messages.

A small ``_eject_stale_bots`` helper is included to clean up any bot
participants left over in the Daily room from a previous crash.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voxtera.audio import (
    AudioLevelMonitor,
    BotActiveUserFrameSuppressor,
    PlaybackLeakageGuard,
    RNNoiseDenoiser,
    TranscriptionNoiseFilter,
)

try:
    from pyrnnoise import RNNoise as _RNNoise
except Exception:  # pragma: no cover - optional dependency at runtime
    _RNNoise = None
from voxtera.config import Settings
from voxtera.controllers import (
    LLM_MODEL,
    AutoTTSLanguageSwitcher,
    GreetingController,
    LanguageSwitcher,
    LLMRunGuard,
    ModelSwitcher,
)
from voxtera.observability import (
    DemoEventBroadcaster,
    PipelineTracer,
    UserTranscriptBroadcaster,
)
from voxtera.prompts import SYSTEM_PROMPT, resolve_greeting
from voxtera.routing import STTGate, STTRouter, TTSGate, TTSRouter
from voxtera.stt import _STT_BUILDERS, _build_stt
from voxtera.tts import _TTS_BUILDERS


def _eject_stale_bots(settings: Settings) -> None:
    """Remove leftover bot participants from previous runs via the Daily REST API.

    Only ejects participants whose ``userName`` matches the configured bot
    name. Human guests (e.g. ``userName='Guest'``) are never touched, so
    rebuilding the pipeline mid-session does not disconnect the user.
    """
    room_name = settings.daily_room_name
    api_key = settings.daily_api_key
    bot_name = settings.bot_name
    base = "https://api.daily.co/v1"
    try:
        req = Request(
            f"{base}/presence",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        participants = data.get(room_name, [])
        stale_ids = [p["id"] for p in participants if p.get("userName") == bot_name]
        if not stale_ids:
            return
        logger.info(
            "[daily] found {} stale bot participants ({}), ejecting",
            len(stale_ids),
            bot_name,
        )

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
    #
    # In Daily mode we build *all* STT providers whose credentials are
    # configured, run them in parallel branches gated by STTRouter, and let
    # the browser flip the active branch at runtime via voxtera-stt
    # messages. In local mode we keep the simple single-STT path because
    # the CLI has no UI to switch from.
    stt = None
    needs_vad = True
    stt_branches: dict[str, dict] = {}
    stt_router: STTRouter | None = None
    if mic_enabled:
        if settings.transport_mode == "daily":
            for name, builder in _STT_BUILDERS.items():
                try:
                    built = builder(settings)
                except Exception as exc:  # noqa: BLE001 — log and skip the branch
                    logger.warning("[stt-router] failed to build {}: {}", name, exc)
                    built = None
                if built is not None:
                    stt_branches[name] = {
                        "stt": built,
                        "input_gate": STTGate(kind="input", label=name),
                        "output_gate": STTGate(kind="output", label=name),
                        "noise_filter": TranscriptionNoiseFilter(stt=built),
                    }
            if not stt_branches:
                raise RuntimeError(
                    "No STT provider could be built. Set at least one of "
                    "OPENAI_API_KEY, DEEPGRAM_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS."
                )
            initial_provider = (
                settings.stt_provider
                if settings.stt_provider in stt_branches
                else next(iter(stt_branches))
            )
            if initial_provider != settings.stt_provider:
                logger.warning(
                    "[stt-router] requested provider {!r} not available — " "starting with {!r}",
                    settings.stt_provider,
                    initial_provider,
                )
            stt_router = STTRouter(stt_branches, initial=initial_provider)
            stt = stt_branches[initial_provider]["stt"]
            # Always run Silero VAD: at least one branch (Whisper or Google)
            # needs it, and Deepgram tolerates redundant VAD events.
            needs_vad = True
        else:
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

    # Build TTS providers. In Daily mode we build every provider with valid
    # credentials and run them as gated parallel branches like STT, so the
    # browser can flip between OpenAI tts-1 and Google Chirp 3 HD instantly.
    # In local mode we keep a single TTS instance.
    tts_branches: dict[str, dict] = {}
    tts_router: TTSRouter | None = None
    if settings.transport_mode == "daily":
        for name, builder in _TTS_BUILDERS.items():
            try:
                built = builder(settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[tts-router] failed to build {}: {}", name, exc)
                built = None
            if built is not None:
                tts_branches[name] = {
                    "tts": built,
                    "input_gate": TTSGate(kind="input", label=name),
                    "output_gate": TTSGate(kind="output", label=name),
                }
        if not tts_branches:
            raise RuntimeError(
                "No TTS provider could be built. Set OPENAI_API_KEY or "
                "GOOGLE_APPLICATION_CREDENTIALS."
            )
        initial_tts_provider = (
            settings.tts_provider
            if settings.tts_provider in tts_branches
            else next(iter(tts_branches))
        )
        if initial_tts_provider != settings.tts_provider:
            logger.warning(
                "[tts-router] requested provider {!r} not available — " "starting with {!r}",
                settings.tts_provider,
                initial_tts_provider,
            )
        tts_router = TTSRouter(tts_branches, initial=initial_tts_provider)
        tts = tts_branches[initial_tts_provider]["tts"]
    else:
        # Local mode: build the single configured provider only.
        builder = _TTS_BUILDERS.get(settings.tts_provider)
        if builder is None:
            raise RuntimeError(
                f"Unknown TTS_PROVIDER={settings.tts_provider!r}. " "Use one of: openai, google."
            )
        built = builder(settings)
        if built is None:
            raise RuntimeError(
                f"TTS_PROVIDER={settings.tts_provider!r} is missing required credentials."
            )
        tts = built

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
            if _RNNoise is None:
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
        if stt_router is not None:
            # Daily mode: parallel STT branches gated by the router. Each
            # branch is [input_gate, stt, noise_filter, output_gate]; only
            # the active branch's gates let frames through.
            processors.append(stt_router)
            processors.append(
                ParallelPipeline(
                    *[
                        [
                            branch["input_gate"],
                            branch["stt"],
                            branch["noise_filter"],
                            branch["output_gate"],
                        ]
                        for branch in stt_branches.values()
                    ]
                )
            )
            processors.append(
                BotActiveUserFrameSuppressor(allow_interruptions=settings.allow_interruptions)
            )
        else:
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
            if stt_router is not None:
                processors.append(LanguageSwitcher(router=stt_router))
            else:
                processors.append(LanguageSwitcher(stt=stt, provider=settings.stt_provider))
        processors.append(
            ModelSwitcher(
                llm=llm,
                tts=tts,
                initial_llm_model=LLM_MODEL,
                initial_tts_voice=settings.default_tts_voice,
                tts_router=tts_router,
            )
        )
        if tts_router is not None:
            # Automatically update Google TTS language when STT detects the
            # guest has switched language. Chirp 3 HD is multilingual so the
            # same voice character works across locales.
            processors.append(AutoTTSLanguageSwitcher(tts_router=tts_router))
        # Greeting controller: defers the startup greeting until the browser
        # sends {type: 'voxtera-ready'} after the transcript page is visible,
        # so the bot never speaks into an empty room.
        greeting_lang, greeting_text = resolve_greeting(settings.greeting_language)
        logger.info(
            "[greeting] language={} (preference={}) — awaiting voxtera-ready from browser",
            greeting_lang,
            settings.greeting_language,
        )
        processors.append(GreetingController(greeting_text=greeting_text))
    if tts_router is not None:
        # Daily mode: parallel TTS branches gated by the router.
        processors.append(tts_router)
        processors.append(
            ParallelPipeline(
                *[
                    [
                        branch["input_gate"],
                        branch["tts"],
                        branch["output_gate"],
                    ]
                    for branch in tts_branches.values()
                ]
            )
        )
    else:
        processors.append(tts)
    processors.extend(
        [
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
