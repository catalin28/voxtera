"""Registry of voice/sentence-quality knobs for the trace dashboard.

Every parameter that affects what the user hears or how cleanly they're
understood is described here as a :class:`Tunable` with metadata (group,
label, explanation, type, range, default, tier) and — for ``tier="live"``
knobs — an ``apply()`` callback that pushes the change into the running
pipeline without a restart.

Three tiers:

- ``live``: applies immediately. e.g. ``vad_stop_secs`` toggles the
  :class:`SileroVADAnalyzer` parameter directly on the running instance.
- ``next_restart``: editable in the UI but only takes effect after a process
  restart. e.g. ``rag_enabled`` (the RAG processor is/is-not in the pipeline
  graph; can't be added/removed at runtime).
- ``hardcoded``: read-only display for transparency. e.g. transcript-filter
  thresholds in :mod:`voxtera.audio` are baked-in for guardrail reasons —
  changing them belongs in code review, not a slider.

The registry is populated by :func:`register_pipeline_knobs` at the end of
``build_pipeline`` so the apply callbacks can close over the live processor
instances. Until that call happens, the registry returns the static metadata
only (UI still renders the knobs in read-only display).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from loguru import logger

from voxtera.config import Settings
from voxtera.trace import emit

Tier = Literal["live", "next_restart", "hardcoded"]
KnobType = Literal["float", "int", "bool", "str", "enum"]
Origin = Literal["default", "env", "live"]


@dataclass
class Tunable:
    """Static + dynamic metadata for a single knob.

    ``apply`` is ``None`` for non-live knobs. ``current`` is None until the
    pipeline build registers a live value.
    """

    name: str
    group: str
    label: str
    explanation: str
    type: KnobType
    default: Any
    tier: Tier
    pipeline_stage: str
    # Numeric knobs use min/max. Enums use choices.
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list[str] = field(default_factory=list)
    # Filled in by the pipeline at registration time.
    current: Any = None
    origin: Origin = "default"
    apply: Callable[[Any], None] | None = None
    # Free-form footnote shown beneath the value (e.g. file:line for hardcoded).
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view used by the trace snapshot API."""
        d = asdict(self)
        # ``apply`` is a callable — strip it from the wire payload.
        d.pop("apply", None)
        # Drop empty optional fields to keep payload compact.
        if not d.get("choices"):
            d.pop("choices")
        if d.get("min") is None:
            d.pop("min")
        if d.get("max") is None:
            d.pop("max")
        if d.get("step") is None:
            d.pop("step")
        if not d.get("note"):
            d.pop("note")
        return d


class TunablesRegistry:
    """Process-wide registry of :class:`Tunable` instances.

    Thread-safe so the HTTP tune handler and the pipeline build can mutate
    safely. Knobs are keyed by ``name``.
    """

    _instance: TunablesRegistry | None = None

    def __init__(self) -> None:
        self._knobs: dict[str, Tunable] = {}
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> TunablesRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register(self, knob: Tunable) -> None:
        """Insert or replace a knob in the registry."""
        with self._lock:
            self._knobs[knob.name] = knob

    def get(self, name: str) -> Tunable | None:
        with self._lock:
            return self._knobs.get(name)

    def all(self) -> list[Tunable]:
        with self._lock:
            return list(self._knobs.values())

    def snapshot(self) -> list[dict[str, Any]]:
        """JSON-serialisable list of every registered knob."""
        return [k.to_dict() for k in self.all()]

    def apply(self, name: str, value: Any) -> tuple[bool, str, Any, Any]:
        """Apply a live edit. Returns ``(applied, error_or_empty, old, new)``.

        Validation runs first; if the value is outside the declared range or
        not in the enum, the change is rejected without touching the
        processor. On success, the knob's ``apply`` callback is invoked, the
        ``current``/``origin`` fields are updated, and a ``knob`` trace event
        is emitted so dashboards see the change.
        """
        knob = self.get(name)
        if knob is None:
            return (False, "unknown_knob", None, None)
        if knob.tier != "live":
            return (False, "not_live_tunable", knob.current, value)
        if knob.apply is None:
            return (False, "no_apply_handler", knob.current, value)

        coerced, err = _validate(knob, value)
        if err:
            return (False, err, knob.current, value)

        old = knob.current
        try:
            knob.apply(coerced)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[tunables] apply failed for {}: {}", name, exc)
            return (False, f"apply_failed:{exc}", old, coerced)

        knob.current = coerced
        knob.origin = "live"
        emit(
            "knob",
            source="tune",
            data={
                "knob": name,
                "old": old,
                "new": coerced,
                "origin": "live",
            },
        )
        logger.info("[tunables] {} {} -> {}", name, old, coerced)
        return (True, "", old, coerced)


def update_current(name: str, value: Any) -> None:
    """Mirror a runtime switch into the registry without invoking apply.

    Used by message-handler paths (STTRouter, TTSRouter, ModelSwitcher) after
    they have already performed the switch via Pipecat frames or gate flips.
    The apply callback would re-do the switch redundantly; this just keeps
    ``knob.current`` in sync so the dashboard's session_providers panel and
    the /knobs snapshot reflect the live state instead of the boot-time
    defaults.

    Safe to call before the registry has been populated (no-op if the knob
    isn't registered yet).
    """
    try:
        knob = TunablesRegistry.instance().get(name)
        if knob is None:
            return
        knob.current = value
        knob.origin = "live"
    except Exception:  # noqa: BLE001
        logger.exception("[tunables] failed to mirror {!r}.current", name)


def _validate(knob: Tunable, value: Any) -> tuple[Any, str]:
    """Coerce + range/choice check. Returns ``(coerced_value, error_or_empty)``."""
    try:
        if knob.type == "float":
            v = float(value)
            if knob.min is not None and v < knob.min:
                return (None, f"below_min:{knob.min}")
            if knob.max is not None and v > knob.max:
                return (None, f"above_max:{knob.max}")
            return (v, "")
        if knob.type == "int":
            v = int(value)
            if knob.min is not None and v < knob.min:
                return (None, f"below_min:{int(knob.min)}")
            if knob.max is not None and v > knob.max:
                return (None, f"above_max:{int(knob.max)}")
            return (v, "")
        if knob.type == "bool":
            if isinstance(value, bool):
                return (value, "")
            if isinstance(value, str) and value.lower() in {"true", "false", "1", "0", "yes", "no"}:
                return (value.lower() in {"true", "1", "yes"}, "")
            return (None, "not_a_bool")
        if knob.type == "enum":
            v = str(value)
            if v not in knob.choices:
                return (None, f"not_in_choices:{','.join(knob.choices)}")
            return (v, "")
        if knob.type == "str":
            return (str(value), "")
    except (TypeError, ValueError) as exc:
        return (None, f"coercion_failed:{exc}")
    return (None, "unsupported_type")


# ---------------------------------------------------------------------------
# Static metadata. Registered at import time with no apply handlers; the
# pipeline build later replaces them with live versions that close over the
# running processor instances.
# ---------------------------------------------------------------------------

_STATIC_KNOBS: list[Tunable] = [
    # ============================== Mic ===================================
    Tunable(
        name="audio_in_sample_rate",
        group="mic",
        label="Mic sample rate",
        explanation=(
            "Sample rate of microphone capture, in Hz. Pinned to 16 kHz "
            "because Silero VAD's ONNX model is trained at 8 kHz or 16 kHz "
            "only — any other rate would silently produce wrong VAD events."
        ),
        type="int",
        default=16000,
        tier="hardcoded",
        pipeline_stage="transport_in",
        note="src/voxtera/pipeline.py — DailyParams / LocalAudioTransportParams",
    ),
    # ============================== VAD ===================================
    Tunable(
        name="vad_stop_secs",
        group="vad",
        label="Silence window — stop_secs",
        explanation=(
            "How many seconds of silence VAD waits before declaring the user "
            "is done speaking. Lower = snappier but cuts off mid-sentence "
            "pauses. Higher = waits too long. 0.2 was too aggressive (caused "
            "VAD chatter, multiple Whisper API calls per turn, +1.5–2 s "
            "overhead). 0.5 is the sweet spot from 2026-05-06 latency tuning."
        ),
        type="float",
        default=0.5,
        tier="live",
        pipeline_stage="vad",
        min=0.1,
        max=2.0,
        step=0.05,
    ),
    Tunable(
        name="vad_start_secs",
        group="vad",
        label="Speech onset window — start_secs",
        explanation=(
            "Seconds of voiced audio required before VAD declares speech has "
            "started. Lower = faster onset detection but more false starts on "
            "transient noise. Higher = slower but cleaner."
        ),
        type="float",
        default=0.2,
        tier="live",
        pipeline_stage="vad",
        min=0.05,
        max=1.0,
        step=0.05,
    ),
    Tunable(
        name="vad_min_volume",
        group="vad",
        label="Minimum RMS for speech",
        explanation=(
            "RMS energy floor below which audio is treated as silence, "
            "regardless of Silero's neural prediction. Lower = picks up "
            "whispers but more false positives in noisy rooms. Higher = "
            "rejects soft speech. Built-in laptop mics typically peak around "
            "0.05–0.07 RMS for normal speech, so 0.02 is a generous floor."
        ),
        type="float",
        default=0.02,
        tier="live",
        pipeline_stage="vad",
        min=0.005,
        max=0.2,
        step=0.005,
    ),
    Tunable(
        name="vad_confidence",
        group="vad",
        label="Silero confidence threshold",
        explanation=(
            "Silero's neural confidence threshold for speech vs. non-speech. "
            "Pipecat's default for headset mics is 0.7. Built-in laptop mics "
            "typically need lower; 0.5 catches normal speech without false "
            "positives on keyboard clicks."
        ),
        type="float",
        default=0.5,
        tier="live",
        pipeline_stage="vad",
        min=0.1,
        max=0.95,
        step=0.05,
    ),
    # ============================== Denoise ===============================
    Tunable(
        name="rnnoise_enabled",
        group="denoise",
        label="RNNoise denoiser",
        explanation=(
            "Optional pre-VAD neural denoiser. Helps in noisy demo "
            "environments (cafés, fans, traffic) but can make speech sound "
            "metallic if over-applied. The denoiser blends 35% original "
            "signal back in (dry mix) to keep consonants intelligible."
        ),
        type="bool",
        default=False,
        tier="live",
        pipeline_stage="rnnoise",
    ),
    Tunable(
        name="rnnoise_dry_mix",
        group="denoise",
        label="RNNoise dry mix (read-only)",
        explanation=(
            "Fraction of original signal blended back into the denoised "
            "audio to soften metallic artifacts. 0.35 keeps consonants "
            "audible while still cutting noise meaningfully. Hardcoded; "
            "edit src/voxtera/audio.py::RNNoiseDenoiser._dry_mix to change."
        ),
        type="float",
        default=0.35,
        tier="hardcoded",
        pipeline_stage="rnnoise",
        note="src/voxtera/audio.py::RNNoiseDenoiser._dry_mix",
    ),
    Tunable(
        name="rnnoise_suppression_guard_ratio",
        group="denoise",
        label="RNNoise suppression guard (read-only)",
        explanation=(
            "If the denoiser crushes output below this ratio of input RMS on "
            "a likely-speech frame, the original audio is passed through "
            "unchanged. Prevents dropped words on clean speech in quiet rooms."
        ),
        type="float",
        default=0.18,
        tier="hardcoded",
        pipeline_stage="rnnoise",
        note="src/voxtera/audio.py::RNNoiseDenoiser._suppression_guard_ratio",
    ),
    # ============================== Echo / barge-in =======================
    Tunable(
        name="allow_interruptions",
        group="echo",
        label="Allow user to interrupt bot",
        explanation=(
            "When enabled, near-field user speech opens a barge-in gate that "
            "lets the user cut the bot off mid-reply. When disabled, mic "
            "audio is gated to silence whenever the bot is speaking, "
            "preventing speaker echo from triggering false interruptions. "
            "Default is OFF for noisy / speaker-leak demo setups."
        ),
        type="bool",
        default=False,
        tier="live",
        pipeline_stage="leakage_guard",
    ),
    Tunable(
        name="leakage_open_ratio",
        group="echo",
        label="Leakage gate open ratio (read-only)",
        explanation=(
            "How much louder than ambient noise floor incoming audio must be "
            "before the barge-in gate opens. 3.5x means real near-field speech "
            "opens it but speaker leakage stays below threshold."
        ),
        type="float",
        default=3.5,
        tier="hardcoded",
        pipeline_stage="leakage_guard",
        note="src/voxtera/audio.py::PlaybackLeakageGuard._open_ratio",
    ),
    Tunable(
        name="leakage_min_open_rms",
        group="echo",
        label="Leakage gate absolute floor (read-only)",
        explanation=(
            "Absolute RMS floor for the barge-in gate, used in addition to "
            "the relative ratio. 0.045 keeps real speech opening the gate "
            "in environments with very low ambient noise."
        ),
        type="float",
        default=0.045,
        tier="hardcoded",
        pipeline_stage="leakage_guard",
        note="src/voxtera/audio.py::PlaybackLeakageGuard._min_open_rms",
    ),
    Tunable(
        name="leakage_required_open_frames",
        group="echo",
        label="Leakage gate hold-open frames (read-only)",
        explanation=(
            "How many consecutive over-threshold audio frames are required "
            "before the gate opens. 8 frames at 20 ms each = 160 ms of "
            "sustained sound, which rejects single transient bursts."
        ),
        type="int",
        default=8,
        tier="hardcoded",
        pipeline_stage="leakage_guard",
        note="src/voxtera/audio.py::PlaybackLeakageGuard._required_open_frames",
    ),
    Tunable(
        name="leakage_post_tts_cooldown_secs",
        group="echo",
        label="Post-TTS cooldown (read-only)",
        explanation=(
            "After the bot stops speaking, mic audio is still gated for "
            "this many seconds to absorb the WebRTC echo tail. 0.25 s is "
            "tuned to the typical browser playback latency."
        ),
        type="float",
        default=0.25,
        tier="hardcoded",
        pipeline_stage="leakage_guard",
        note="src/voxtera/audio.py::PlaybackLeakageGuard._post_tts_cooldown_secs",
    ),
    # ============================== STT ===================================
    Tunable(
        name="stt_provider",
        group="stt",
        label="STT provider",
        explanation=(
            "Which speech-to-text engine is currently active. Whisper "
            "supports 99 languages with auto-detection (recommended for "
            "tourism). Deepgram nova-3 supports 14. Google supports a broad "
            "set with explicit language codes."
        ),
        type="enum",
        default="whisper",
        tier="live",
        pipeline_stage="stt",
        choices=["whisper", "deepgram", "google"],
    ),
    Tunable(
        name="stt_prompt_enabled",
        group="stt",
        label="STT prompt hint",
        explanation=(
            "When enabled, a domain-specific prompt is sent to Whisper to "
            "improve recognition of hotel-specific vocabulary. WARNING: "
            "biases Whisper toward the prompt's language — disable for "
            "multilingual deployments. Off by default for that reason."
        ),
        type="bool",
        default=False,
        tier="next_restart",
        pipeline_stage="stt",
    ),
    # ============================== LLM ===================================
    Tunable(
        name="llm_model",
        group="llm",
        label="LLM model",
        explanation=(
            "Which Anthropic model the LLM service is using. Haiku 4.5 is "
            "fastest (low latency, recommended for voice). Sonnet 4.6 is "
            "higher quality (better answers, slower). Opus 4.7 is highest "
            "quality but noticeably slower."
        ),
        type="enum",
        default="claude-haiku-4-5-20251001",
        tier="live",
        pipeline_stage="llm",
        choices=[
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        ],
    ),
    # ============================== TTS ===================================
    Tunable(
        name="tts_provider",
        group="tts",
        label="TTS provider",
        explanation=(
            "Which text-to-speech engine is currently active. OpenAI tts-1 "
            "is faster and supports the most voices. Google Chirp 3 HD has "
            "75+ language coverage and per-locale Chirp voices."
        ),
        type="enum",
        default="openai",
        tier="live",
        pipeline_stage="tts",
        choices=["openai", "google"],
    ),
    Tunable(
        name="tts_voice",
        group="tts",
        label="TTS voice",
        explanation=(
            "Voice character for the active TTS provider. OpenAI: alloy, "
            "ash, coral, echo, fable, nova, onyx, sage, shimmer, verse. "
            "Google Chirp 3 HD: per-locale voices like en-US-Chirp3-HD-Charon."
        ),
        type="str",
        default="nova",
        tier="live",
        pipeline_stage="tts",
    ),
    # ============================== Audio out =============================
    Tunable(
        name="audio_out_sample_rate",
        group="audio_out",
        label="Audio out sample rate",
        explanation=(
            "Output sample rate. 48 kHz in Daily WebRTC mode (browser native), "
            "24 kHz in local mode. Pipecat resamples 24 kHz TTS audio up to "
            "48 kHz before Daily — without this resampling step, 24 kHz audio "
            "tagged as 48 kHz plays at chipmunk speed (observed Brave/Safari)."
        ),
        type="int",
        default=48000,
        tier="hardcoded",
        pipeline_stage="transport_out",
        note="src/voxtera/pipeline.py — DailyParams.audio_out_sample_rate",
    ),
    # ============================== Flow ==================================
    Tunable(
        name="rag_enabled",
        group="flow",
        label="RAG context injection",
        explanation=(
            "When enabled, hotel-specific knowledge chunks are retrieved and "
            "injected into the LLM context before each turn. Adds ~50–500 ms "
            "to LLM TTFT depending on retrieval cache state. Cannot be "
            "toggled at runtime — the RAG processor is wired into the "
            "pipeline graph at build time."
        ),
        type="bool",
        default=False,
        tier="next_restart",
        pipeline_stage="rag",
    ),
    Tunable(
        name="pipeline_idle_timeout_secs",
        group="flow",
        label="Idle timeout",
        explanation=(
            "Seconds of pipeline inactivity before the bot exits. None = "
            "disabled (default). Useful in always-on demo mode where users "
            "leave the page open without speaking."
        ),
        type="float",
        default=0,
        tier="next_restart",
        pipeline_stage="flow",
        min=0,
        max=3600,
    ),
]


def _populate_static() -> None:
    """Register the static metadata (no apply handlers)."""
    reg = TunablesRegistry.instance()
    for k in _STATIC_KNOBS:
        reg.register(k)


# Populate on import so the snapshot endpoint works even before the pipeline
# build wires the live apply handlers.
_populate_static()


# ---------------------------------------------------------------------------
# Live registration: called from build_pipeline after every processor exists.
# Each registered knob replaces the static one with a live ``current`` value
# and an ``apply()`` that closes over the live processor instance.
# ---------------------------------------------------------------------------


def register_pipeline_knobs(
    *,
    settings: Settings,
    vad_processor: Any | None,
    leakage_guard: Any | None,
    user_frame_suppressor: Any | None,
    rnnoise_denoiser: Any | None,
    stt_router: Any | None,
    tts_router: Any | None,
    llm: Any | None,
    tts: Any | None,
) -> None:
    """Register live apply handlers for every v1 live-tunable knob.

    Called once from :func:`voxtera.pipeline.build_pipeline` at the end of
    pipeline construction. Each knob's apply closes over the actual processor
    instance so live edits land on the running pipeline.

    Any argument that's ``None`` (because the corresponding processor wasn't
    built — e.g. ``rnnoise_denoiser`` when ``RNNOISE_ENABLED=false``) leaves
    that knob in display-only mode (no apply handler).
    """
    reg = TunablesRegistry.instance()

    # ---- VAD knobs -----------------------------------------------------
    if vad_processor is not None:
        # Pipecat's VADProcessor wraps a ``vad_analyzer`` (Silero). Silero's
        # ``params`` dataclass holds stop_secs/start_secs/min_volume/confidence.
        # Mutating the live instance is the supported way to retune at runtime.
        analyzer = getattr(vad_processor, "_vad_analyzer", None) or getattr(
            vad_processor, "vad_analyzer", None
        )

        def _apply_vad_param(name: str, value: Any) -> None:
            if analyzer is None:
                raise RuntimeError("vad_analyzer not accessible on VADProcessor")
            params = getattr(analyzer, "_params", None) or getattr(analyzer, "params", None)
            if params is None:
                raise RuntimeError("VAD analyzer has no params attribute")
            setattr(params, name, value)

        for env_name, knob_attr, env_value in [
            ("vad_stop_secs", "stop_secs", settings.vad_stop_secs),
            ("vad_start_secs", "start_secs", settings.vad_start_secs),
            ("vad_min_volume", "min_volume", settings.vad_min_volume),
            ("vad_confidence", "confidence", settings.vad_confidence),
        ]:
            knob = reg.get(env_name)
            if knob is None:
                continue
            knob.current = env_value
            knob.origin = "env" if env_value != knob.default else "default"
            # Bind the closure with default arg to avoid late-binding bug.
            knob.apply = lambda v, _a=knob_attr: _apply_vad_param(_a, v)
            reg.register(knob)

    # ---- Allow interruptions ------------------------------------------
    knob = reg.get("allow_interruptions")
    if knob is not None:
        knob.current = settings.allow_interruptions
        knob.origin = "env" if settings.allow_interruptions != knob.default else "default"

        def _apply_allow_interruptions(value: bool) -> None:
            if leakage_guard is not None:
                leakage_guard._allow_interruptions = bool(value)
            if user_frame_suppressor is not None:
                user_frame_suppressor._allow_interruptions = bool(value)

        knob.apply = _apply_allow_interruptions
        reg.register(knob)

    # ---- RNNoise toggle ------------------------------------------------
    knob = reg.get("rnnoise_enabled")
    if knob is not None:
        knob.current = settings.rnnoise_enabled
        knob.origin = "env" if settings.rnnoise_enabled != knob.default else "default"

        def _apply_rnnoise(value: bool) -> None:
            if rnnoise_denoiser is None:
                # Denoiser wasn't built at startup. Toggling on requires a
                # pipeline rebuild — flag this clearly to the operator.
                raise RuntimeError(
                    "rnnoise was not built at startup; restart with "
                    "RNNOISE_ENABLED=true to enable it"
                )
            rnnoise_denoiser._disabled = not bool(value)

        knob.apply = _apply_rnnoise
        reg.register(knob)

    # ---- STT provider --------------------------------------------------
    knob = reg.get("stt_provider")
    if knob is not None and stt_router is not None:
        knob.current = settings.stt_provider
        knob.origin = "env" if settings.stt_provider != knob.default else "default"
        # Constrain choices to actually-built branches.
        try:
            built = list(stt_router._branches.keys())  # noqa: SLF001
            if built:
                knob.choices = built
        except Exception:  # noqa: BLE001
            pass

        def _apply_stt_provider(value: str) -> None:
            stt_router.set_active(value)

        knob.apply = _apply_stt_provider
        reg.register(knob)

    # ---- TTS provider --------------------------------------------------
    knob = reg.get("tts_provider")
    if knob is not None and tts_router is not None:
        knob.current = settings.tts_provider
        knob.origin = "env" if settings.tts_provider != knob.default else "default"
        try:
            built = list(tts_router._branches.keys())  # noqa: SLF001
            if built:
                knob.choices = built
        except Exception:  # noqa: BLE001
            pass

        def _apply_tts_provider(value: str) -> None:
            tts_router.set_active(value)

        knob.apply = _apply_tts_provider
        reg.register(knob)

    # ---- TTS voice -----------------------------------------------------
    knob = reg.get("tts_voice")
    if knob is not None:
        knob.current = settings.default_tts_voice
        knob.origin = "env" if settings.default_tts_voice != knob.default else "default"

        # The apply handler emits the same TTSUpdateSettingsFrame that
        # ModelSwitcher would on a voxtera-voice app-message. We can't push
        # a frame from a sync HTTP handler; instead we stash the desired
        # voice and let a scheduler frame processor pick it up. For v1, do
        # the simplest thing: mutate the active TTS service's settings
        # directly, mirroring what ModelSwitcher does for the
        # voxtera-voice path.
        def _apply_tts_voice(value: str) -> None:
            active = getattr(tts_router, "active_tts", None) if tts_router is not None else tts
            if active is None:
                raise RuntimeError("no active TTS service")
            settings_obj = getattr(active, "_settings", None)
            if settings_obj is None:
                raise RuntimeError("active TTS has no _settings attribute")
            settings_obj.voice = value

        knob.apply = _apply_tts_voice
        reg.register(knob)

    # ---- LLM model -----------------------------------------------------
    knob = reg.get("llm_model")
    if knob is not None and llm is not None:
        from voxtera.controllers import LLM_MODEL

        knob.current = LLM_MODEL
        knob.origin = "default"

        def _apply_llm_model(value: str) -> None:
            settings_obj = getattr(llm, "_settings", None)
            if settings_obj is None:
                raise RuntimeError("LLM has no _settings attribute")
            settings_obj.model = value

        knob.apply = _apply_llm_model
        reg.register(knob)

    # ---- Read-only / next-restart current values -----------------------
    # Populate ``current`` for read-only knobs so the UI shows real values.
    _populate_readonly_current(settings)

    logger.info(
        "[tunables] registered {} knobs ({} live)",
        len(reg.all()),
        sum(1 for k in reg.all() if k.tier == "live" and k.apply is not None),
    )


def _populate_readonly_current(settings: Settings) -> None:
    """Set ``current`` on read-only / next-restart knobs from Settings."""
    reg = TunablesRegistry.instance()
    mapping = {
        "audio_in_sample_rate": 16000,
        "audio_out_sample_rate": 48000 if settings.transport_mode == "daily" else 24000,
        "stt_prompt_enabled": settings.stt_prompt_enabled,
        "rag_enabled": settings.rag_enabled,
        "pipeline_idle_timeout_secs": settings.pipeline_idle_timeout_secs or 0,
    }
    for name, value in mapping.items():
        knob = reg.get(name)
        if knob is None:
            continue
        knob.current = value
        knob.origin = "env" if value != knob.default else "default"
        reg.register(knob)
