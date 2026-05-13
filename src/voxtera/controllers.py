"""Runtime controllers driven by browser app-messages.

Each ``FrameProcessor`` here listens for a specific
``DailyInputTransportMessageFrame`` envelope and reconfigures the live
pipeline without restarting it:

- :class:`LanguageSwitcher` — handles ``{type: 'voxtera-language', ...}`` and
  reconfigures the active STT branch via :class:`STTUpdateSettingsFrame`.
- :class:`AutoTTSLanguageSwitcher` — observes :class:`TranscriptionFrame`
  language detection and updates the active Google Chirp 3 HD TTS to the
  matching locale (preserving the voice character).
- :class:`ModelSwitcher` — handles ``voxtera-model`` and ``voxtera-voice``
  to swap the LLM model and TTS voice respectively.
- :class:`GreetingController` — fires the startup greeting on
  ``voxtera-ready``, with a 3 s debounce so a duplicate ready message can't
  produce two overlapping greetings.
- :class:`LLMRunGuard` — drops orphan / rapid-fire :class:`LLMRunFrame`
  events that aren't tied to a recent user turn.
"""

from __future__ import annotations

import os
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    LLMUpdateSettingsFrame,
    STTUpdateSettingsFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.openai.tts import OpenAITTSService

try:
    from pipecat.transports.daily.transport import DailyInputTransportMessageFrame
except Exception:  # daily-python not available on Windows
    DailyInputTransportMessageFrame = None  # type: ignore[assignment,misc]

from voxtera.routing import STTRouter, TTSRouter
from voxtera.stt import _VALID_STT_LANGUAGES, _google_languages_for_selection
from voxtera.tts import (
    TTS_GOOGLE_DEFAULT_VOICE,
    _voices_for_tts_provider,
)
from voxtera.tunables import update_current as _update_knob_current

# Default LLM. Override via LLM_MODEL_OVERRIDE env var (set by serve.py from
# the browser's model selection at spawn time).
LLM_MODEL: str = os.environ.get("LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001")

_ANTHROPIC_LLM_MODELS: frozenset[str] = frozenset(
    {
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    }
)
_OPENAI_LLM_MODELS: frozenset[str] = frozenset({"gpt-4o-mini"})
_VALID_LLM_MODELS: frozenset[str] = _ANTHROPIC_LLM_MODELS | _OPENAI_LLM_MODELS


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

        # The previous bot turn has ended — either naturally
        # (BotStoppedSpeakingFrame) or because the user barged in
        # (InterruptionFrame). In both cases the refractory window has
        # already served its purpose (suppressing rapid run storms during a
        # single bot turn) and must be cleared so the *next* user turn isn't
        # silently dropped. Without this reset, an interruption that arrives
        # within `min_run_interval_secs` of the last run leaves the bot
        # permanently stuck: the user's append is recorded but the
        # LLMRunFrame is dropped, so the LLM never fires.
        if isinstance(frame, BotStoppedSpeakingFrame | InterruptionFrame):
            if self._last_run_sent_at is not None:
                logger.debug(
                    "[llm-run-guard] clearing refractory window on {}",
                    type(frame).__name__,
                )
                self._last_run_sent_at = None
            await self.push_frame(frame, direction)
            return

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

            # Refractory window: suppress rapid run storms from noisy VAD churn
            # *within a single bot turn*. The window is cleared when the bot
            # turn ends (see BotStoppedSpeakingFrame / InterruptionFrame branch
            # above) so post-interruption runs are not blocked.
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


class BrowserTextInputController(FrameProcessor):
    """Handles typed chat messages from the browser Daily data channel.

    Consumes app-messages shaped like:

        {"type": "voxtera-user-text", "text": "..."}

    and converts them into a normal user turn by appending the user message
    to LLM context and triggering a single :class:`LLMRunFrame`.
    """

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("type") == "voxtera-user-text":
                text = msg.get("text")
                if not isinstance(text, str):
                    logger.warning("[text-input] ignored non-string payload")
                else:
                    text = text.strip()
                    if not text:
                        logger.warning("[text-input] ignored empty typed message")
                    else:
                        logger.info("[text-input] received typed user message")
                        await self.push_frame(
                            LLMMessagesAppendFrame([{"role": "user", "content": text}]),
                            FrameDirection.DOWNSTREAM,
                        )
                        await self.push_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

        await self.push_frame(frame, direction)


# Languages each STT provider can actually transcribe. Sending a language
# code outside this set used to silently produce garbage transcriptions
# (e.g. Deepgram nova-3 receiving 'ro' would return Portuguese-shaped junk
# like "Boa, boa, boa..."). The :class:`LanguageSwitcher` now validates
# against these sets and falls back to a safe value with a clear warning.
_DEEPGRAM_NOVA3_LANGUAGES: frozenset[str] = frozenset(
    {
        # Nova-3 single-language codes. "multi" auto-detects across the
        # nova-3 multilingual set (en/es/fr/de/it/nl/pt/hi/ru/ja).
        "multi",
        "en",
        "es",
        "fr",
        "de",
        "hi",
        "ja",
        "ko",
        "pt",
        "ru",
        "it",
        "nl",
        "sv",
        "tr",
        "uk",
    }
)
# Whisper has wide language coverage; we accept the full STT language list
# here. The remaining hallucination risk is handled by
# :class:`TranscriptionNoiseFilter` in voxtera.audio.
_WHISPER_LANGUAGES: frozenset[str] = frozenset({"multi", "auto"})  # plus everything else
# Google STT covers a broad multilingual set. We treat any code present in
# :data:`_GOOGLE_LANGUAGE_MAP` as supported, plus "multi" for auto-detect.


# A single map keyed by provider, used by :meth:`_supports_language`.
def _provider_supports_language(provider: str, lang: str) -> bool:
    if lang == "multi":
        return True
    if provider == "deepgram":
        return lang in _DEEPGRAM_NOVA3_LANGUAGES
    if provider == "google":
        from voxtera.stt import _GOOGLE_LANGUAGE_MAP

        return lang in _GOOGLE_LANGUAGE_MAP
    if provider == "whisper":
        # Whisper's language coverage is broader than the demo dropdown.
        return lang in _VALID_STT_LANGUAGES
    return False


class LanguageSwitcher(FrameProcessor):
    """Listens for language-selection messages from the browser and reconfigures STT.

    When the demo page sends ``{type: 'voxtera-language', language: 'ro'}``,
    this processor pushes an ``STTUpdateSettingsFrame`` upstream to change
    the active STT language on the fly.

    If the requested language isn't supported by the active provider (e.g.
    Romanian on Deepgram nova-3), the request is logged loudly and the
    provider is left on its previous setting — better to fail visibly than
    return silent garbage.

    Provider-switch messages (``voxtera-stt``) are handled by
    :class:`STTRouter`, not here.
    """

    def __init__(
        self,
        stt: FrameProcessor | None = None,
        provider: str | None = None,
        *,
        router: STTRouter | None = None,
    ) -> None:
        super().__init__()
        if router is None and (stt is None or provider is None):
            raise ValueError("LanguageSwitcher needs either router or (stt, provider)")
        self._stt = stt
        self._provider = provider
        self._router = router
        self._current_language: str = "multi"

    def _resolve(self) -> tuple[FrameProcessor, str]:
        if self._router is not None:
            return self._router.active_stt, self._router.active_provider
        return self._stt, self._provider  # type: ignore[return-value]

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
                        active_stt, active_provider = self._resolve()
                        # Provider-specific language allowlist. Refuse the
                        # change rather than push a code the provider can't
                        # actually transcribe — that produces silent garbage,
                        # not an error.
                        if not _provider_supports_language(active_provider, lang):
                            logger.warning(
                                "[lang-switch] provider {!r} does NOT support language {!r}; "
                                "ignoring change. Pick a different STT provider for this "
                                "language (e.g. Google handles {!r} natively).",
                                active_provider,
                                lang,
                                lang,
                            )
                            await self.push_frame(frame, direction)
                            return

                        self._current_language = lang
                        logger.info(
                            "[lang-switch] switching STT language to {!r} (provider={})",
                            lang,
                            active_provider,
                        )
                        delta = None
                        if active_provider == "google":
                            from pipecat.services.google.stt import GoogleSTTService

                            delta = GoogleSTTService.Settings(
                                languages=_google_languages_for_selection(lang)
                            )
                        elif active_provider == "deepgram":
                            from pipecat.services.deepgram.stt import DeepgramSTTService

                            delta = DeepgramSTTService.Settings(language=lang)
                        else:
                            next_settings = {"language": lang}
                            await self.push_frame(
                                STTUpdateSettingsFrame(settings=next_settings, service=active_stt),
                                FrameDirection.UPSTREAM,
                            )
                            await self.push_frame(frame, direction)
                            return

                        await self.push_frame(
                            STTUpdateSettingsFrame(delta=delta, service=active_stt),
                            FrameDirection.UPSTREAM,
                        )

        await self.push_frame(frame, direction)


class AutoTTSLanguageSwitcher(FrameProcessor):
    """Automatically updates TTS language when STT detects a language change.

    Observes :class:`TranscriptionFrame`, maps the detected language to a
    BCP-47 code, and pushes a :class:`TTSUpdateSettingsFrame` downstream so
    the active TTS service produces audio in the guest's language.

    Provider-specific behavior:

    * **Google Chirp 3 HD**: voice IDs are locale-prefixed
      (``en-US-Chirp3-HD-Charon``), so the locale prefix is rewritten while
      preserving the voice character (e.g. → ``ro-RO-Chirp3-HD-Charon``
      for Romanian).
    * **Cartesia Sonic-3**: voices are inherently multilingual — the same
      voice ID produces audio in any of Sonic-3's 42 supported languages
      when the ``language`` parameter is updated. Only the language is
      changed; the voice ID stays the same. Languages outside the
      42-language list are silently skipped (TTS keeps producing in the
      previously-set language) and logged at info level.
    * **OpenAI tts-1**: not handled here. tts-1 detects language natively
      from the input text and doesn't accept an explicit language parameter.

    When the TTS provider switches via ``voxtera-tts-provider``, the
    tracked language is reset so the first transcription after the switch
    always re-applies the correct locale to the new service.
    """

    # Cartesia Sonic-3 supported languages (ISO 639-1). Source: Cartesia
    # docs (https://docs.cartesia.ai/build-with-cartesia/tts-models/latest).
    # Sonic-3 voices are multilingual: the same voice ID produces audio in
    # any of these languages when the ``language`` parameter is set.
    _CARTESIA_LANGUAGES: frozenset[str] = frozenset(
        {
            "en",
            "fr",
            "de",
            "es",
            "pt",
            "zh",
            "ja",
            "hi",
            "it",
            "ko",
            "nl",
            "pl",
            "ru",
            "sv",
            "tr",
            "tl",
            "bg",
            "ro",
            "ar",
            "cs",
            "el",
            "fi",
            "hr",
            "ms",
            "sk",
            "da",
            "ta",
            "uk",
            "hu",
            "no",
            "vi",
            "bn",
            "th",
            "he",
            "ka",
            "id",
            "te",
            "gu",
            "kn",
            "ml",
            "mr",
            "pa",
        }
    )

    # Whisper full names, BCP-47 short codes, and BCP-47 full codes all map
    # to the canonical BCP-47 code expected by Google TTS.
    _LANG_MAP: dict[str, str] = {
        # Whisper full language names (lowercase)
        "afrikaans": "af-ZA",
        "arabic": "ar-XA",
        "bulgarian": "bg-BG",
        "catalan": "ca-ES",
        "chinese": "zh-CN",
        "croatian": "hr-HR",
        "czech": "cs-CZ",
        "danish": "da-DK",
        "dutch": "nl-NL",
        "english": "en-US",
        "estonian": "et-EE",
        "finnish": "fi-FI",
        "french": "fr-FR",
        "german": "de-DE",
        "greek": "el-GR",
        "gujarati": "gu-IN",
        "hindi": "hi-IN",
        "hungarian": "hu-HU",
        "icelandic": "is-IS",
        "indonesian": "id-ID",
        "italian": "it-IT",
        "japanese": "ja-JP",
        "kannada": "kn-IN",
        "korean": "ko-KR",
        "latvian": "lv-LV",
        "lithuanian": "lt-LT",
        "malay": "ms-MY",
        "marathi": "mr-IN",
        "norwegian": "nb-NO",
        "persian": "fa-IR",
        "polish": "pl-PL",
        "portuguese": "pt-PT",
        "romanian": "ro-RO",
        "russian": "ru-RU",
        "serbian": "sr-RS",
        "slovak": "sk-SK",
        "slovenian": "sl-SI",
        "spanish": "es-ES",
        "swedish": "sv-SE",
        "tagalog": "fil-PH",
        "tamil": "ta-IN",
        "telugu": "te-IN",
        "thai": "th-TH",
        "turkish": "tr-TR",
        "ukrainian": "uk-UA",
        "urdu": "ur-IN",
        "vietnamese": "vi-VN",
        # BCP-47 short codes (Deepgram / Google STT output)
        "af": "af-ZA",
        "ar": "ar-XA",
        "bg": "bg-BG",
        "ca": "ca-ES",
        "zh": "zh-CN",
        "hr": "hr-HR",
        "cs": "cs-CZ",
        "da": "da-DK",
        "nl": "nl-NL",
        "en": "en-US",
        "et": "et-EE",
        "fi": "fi-FI",
        "fr": "fr-FR",
        "de": "de-DE",
        "el": "el-GR",
        "gu": "gu-IN",
        "hi": "hi-IN",
        "hu": "hu-HU",
        "id": "id-ID",
        "it": "it-IT",
        "ja": "ja-JP",
        "kn": "kn-IN",
        "ko": "ko-KR",
        "lv": "lv-LV",
        "lt": "lt-LT",
        "ms": "ms-MY",
        "mr": "mr-IN",
        "no": "nb-NO",
        "fa": "fa-IR",
        "pl": "pl-PL",
        "pt": "pt-PT",
        "ro": "ro-RO",
        "ru": "ru-RU",
        "sr": "sr-RS",
        "sk": "sk-SK",
        "sl": "sl-SI",
        "es": "es-ES",
        "sv": "sv-SE",
        "ta": "ta-IN",
        "te": "te-IN",
        "th": "th-TH",
        "tr": "tr-TR",
        "uk": "uk-UA",
        "ur": "ur-IN",
        "vi": "vi-VN",
    }

    def __init__(self, tts_router: TTSRouter) -> None:
        super().__init__()
        self._tts_router = tts_router
        # BCP-47 code currently set on the active TTS service. None means
        # "unknown / needs to be applied on next transcription". Shared
        # across Google and Cartesia because both providers receive the
        # same BCP-47 normalization upstream — only the downstream
        # Settings shape differs.
        self._current_lang: str | None = None

    @classmethod
    def _cartesia_lang_for_bcp47(cls, bcp47: str):
        """Convert a BCP-47 code to a Pipecat ``Language`` enum supported
        by Cartesia Sonic-3, or ``None`` if Sonic-3 doesn't support it.

        Returns a Pipecat ``Language`` member (e.g. ``Language.RO``).
        Done lazily via ``getattr`` so the import only fires when Cartesia
        is actually active.
        """
        if not bcp47:
            return None
        code = bcp47.split("-")[0].lower()
        if code not in cls._CARTESIA_LANGUAGES:
            return None
        try:
            from pipecat.transcriptions.language import Language

            return getattr(Language, code.upper(), None)
        except ImportError:
            return None

    @staticmethod
    def _resolve_lang(lang_val: object) -> str | None:
        """Map any language representation to a BCP-47 code, or None if unknown."""
        if lang_val is None:
            return None
        # Language enum → get the .value string
        raw = getattr(lang_val, "value", None) or str(lang_val)
        raw = raw.strip().lower()
        if not raw:
            return None
        # Direct lookup (full name or short code)
        result = AutoTTSLanguageSwitcher._LANG_MAP.get(raw)
        if result:
            return result
        # Try just the primary language subtag (e.g. "en" from "en-US")
        primary = raw.split("-")[0].split("_")[0]
        return AutoTTSLanguageSwitcher._LANG_MAP.get(primary)

    @staticmethod
    def _chirp3_voice_for_lang(current_voice: str, new_lang: str) -> str:
        """Reconstruct a Chirp 3 HD voice ID for the new locale.

        e.g. ``("en-US-Chirp3-HD-Charon", "ro-RO")`` → ``"ro-RO-Chirp3-HD-Charon"``
        For non-Chirp3-HD voice names the original value is returned unchanged.
        """
        if "Chirp3-HD-" in current_voice:
            character = current_voice.split("Chirp3-HD-")[-1]
            return f"{new_lang}-Chirp3-HD-{character}"
        return current_voice

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Reset tracked language when the TTS provider changes so the first
        # transcription after switching back to Google re-applies the locale.
        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict) and msg.get("type") == "voxtera-tts-provider":
                self._current_lang = None

        elif (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
            and self._tts_router.active_provider in ("google", "cartesia")
        ):
            bcp47 = self._resolve_lang(getattr(frame, "language", None))
            if bcp47 and bcp47 != self._current_lang:
                active_provider = self._tts_router.active_provider
                try:
                    active_tts = self._tts_router.active_tts
                    if active_provider == "google":
                        from pipecat.services.google.tts import GoogleTTSService

                        # Read the current voice name from the live TTS settings.
                        settings_obj = getattr(active_tts, "_settings", None)
                        current_voice = (
                            getattr(settings_obj, "voice", None) or TTS_GOOGLE_DEFAULT_VOICE
                        )
                        new_voice = self._chirp3_voice_for_lang(current_voice, bcp47)
                        logger.info(
                            "[tts-lang] detected={!r} → Google TTS language={!r} voice={!r}",
                            bcp47,
                            bcp47,
                            new_voice,
                        )
                        await self.push_frame(
                            TTSUpdateSettingsFrame(
                                delta=GoogleTTSService.Settings(voice=new_voice, language=bcp47),
                                service=active_tts,
                            ),
                            FrameDirection.DOWNSTREAM,
                        )
                        self._current_lang = bcp47
                    else:  # cartesia
                        cartesia_lang = self._cartesia_lang_for_bcp47(bcp47)
                        if cartesia_lang is None:
                            logger.info(
                                "[tts-lang] cartesia: {!r} not in Sonic-3's 42-language "
                                "list, keeping current TTS language",
                                bcp47,
                            )
                            # Update tracked lang anyway so we don't retry every
                            # frame for the same unsupported language.
                            self._current_lang = bcp47
                        else:
                            from pipecat.services.cartesia.tts import CartesiaTTSService

                            logger.info(
                                "[tts-lang] detected={!r} → Cartesia language={!r} "
                                "(voice unchanged)",
                                bcp47,
                                getattr(cartesia_lang, "value", cartesia_lang),
                            )
                            await self.push_frame(
                                TTSUpdateSettingsFrame(
                                    delta=CartesiaTTSService.Settings(language=cartesia_lang),
                                    service=active_tts,
                                ),
                                FrameDirection.DOWNSTREAM,
                            )
                            self._current_lang = bcp47
                except Exception as exc:
                    logger.warning(
                        "[tts-lang] failed to update {} TTS language: {}",
                        active_provider,
                        exc,
                    )

        await self.push_frame(frame, direction)


class ModelSwitcher(FrameProcessor):
    """Listens for model/voice selection messages from the browser.

    Handles two message types:
    - ``{type: 'voxtera-model', model: 'claude-sonnet-4-6'}``
      → pushes LLMUpdateSettingsFrame upstream to swap the Anthropic model.
    - ``{type: 'voxtera-voice', voice: '<voice-id>'}``
      → pushes TTSUpdateSettingsFrame downstream to swap the active TTS voice.
      The Settings type used depends on the currently active TTS provider
      (looked up via the optional :class:`TTSRouter`).
    """

    def __init__(
        self,
        llm: FrameProcessor,
        tts: FrameProcessor,
        *,
        initial_llm_model: str = LLM_MODEL,
        initial_tts_voice: str = "nova",
        tts_router: TTSRouter | None = None,
    ) -> None:
        super().__init__()
        self._llm = llm
        self._tts = tts
        self._tts_router = tts_router
        self._current_llm_model = initial_llm_model
        self._current_tts_voice = initial_tts_voice

    def _resolve_tts(self) -> tuple[FrameProcessor, str]:
        if self._tts_router is not None:
            return self._tts_router.active_tts, self._tts_router.active_provider
        # Fall back to introspection so non-Daily local mode keeps working.
        provider = "google" if self._tts.__class__.__name__.startswith("Google") else "openai"
        return self._tts, provider

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            if isinstance(msg, dict):
                if msg.get("type") == "voxtera-model":
                    model = msg.get("model", "")
                    if model not in _VALID_LLM_MODELS:
                        logger.warning("[model-switch] unknown LLM model {!r}, ignoring", model)
                    elif model != self._current_llm_model:
                        if (
                            model in _OPENAI_LLM_MODELS
                            or self._current_llm_model in _OPENAI_LLM_MODELS
                        ):
                            logger.warning(
                                "[model-switch] cross-provider switch {!r}→{!r} requires reconnect",
                                self._current_llm_model,
                                model,
                            )
                        else:
                            self._current_llm_model = model
                            logger.info("[model-switch] switching LLM model to {!r}", model)
                            await self.push_frame(
                                LLMUpdateSettingsFrame(
                                    delta=AnthropicLLMService.Settings(model=model),
                                    service=self._llm,
                                ),
                                FrameDirection.UPSTREAM,
                            )
                            # Mirror into the registry so the dashboard's
                            # session_providers panel reflects the live model.
                            _update_knob_current("llm_model", model)
                    await self.push_frame(frame, direction)
                    return
                elif msg.get("type") == "voxtera-voice":
                    voice = msg.get("voice", "")
                    active_tts, active_provider = self._resolve_tts()
                    valid_voices = _voices_for_tts_provider(active_provider)
                    if voice not in valid_voices:
                        logger.warning(
                            "[model-switch] voice {!r} not valid for provider {!r}, ignoring",
                            voice,
                            active_provider,
                        )
                    elif voice != self._current_tts_voice:
                        self._current_tts_voice = voice
                        logger.info(
                            "[model-switch] switching TTS voice to {!r} (provider={})",
                            voice,
                            active_provider,
                        )
                        if active_provider == "google":
                            from pipecat.services.google.tts import GoogleTTSService

                            delta = GoogleTTSService.Settings(voice=voice)
                        elif active_provider == "cartesia":
                            from pipecat.services.cartesia.tts import CartesiaTTSService

                            delta = CartesiaTTSService.Settings(voice=voice)
                        else:
                            delta = OpenAITTSService.Settings(voice=voice)
                        await self.push_frame(
                            TTSUpdateSettingsFrame(delta=delta, service=active_tts),
                            FrameDirection.DOWNSTREAM,
                        )
                        # Mirror into the registry so the dashboard's
                        # session_providers panel reflects the live voice.
                        _update_knob_current("tts_voice", voice)
                    await self.push_frame(frame, direction)
                    return

        await self.push_frame(frame, direction)


class GreetingController(FrameProcessor):
    """Plays the startup greeting after the browser sends {type: 'voxtera-ready'}.

    In Daily mode the greeting is NOT queued at pipeline start. Instead this
    processor waits for the frontend to confirm the user is ready (emitted
    immediately after joining and the transcript page is shown) and then pushes
    TTSSpeakFrame downstream so the TTS service plays the greeting only when a
    participant is actually listening.

    Triggers on every voxtera-ready so reconnecting users receive a fresh
    greeting without requiring a backend restart — but a 3 s debounce
    prevents two rapid voxtera-ready events (browser reload, tab focus race)
    from queuing two overlapping greetings, which otherwise sounds like a
    heavy echo on the welcome message.

    Greeting language resolution:

    The boot-time ``greeting_text`` is used as the default. When the browser
    sends ``voxtera-language`` before ``voxtera-ready`` (which the demo
    page does in the Start handler), the greeting is re-resolved from the
    optional ``greetings_map`` so the user hears the welcome message in
    the language they selected. If the selected language isn't in the
    map (e.g. an exotic language we don't have a hardcoded greeting for),
    the bot falls back to the default ``greeting_text``.
    """

    def __init__(
        self,
        greeting_text: str,
        *,
        debounce_secs: float = 3.0,
        greetings_map: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._greeting_text = greeting_text
        # Per-language greeting catalog. Used to re-resolve the greeting
        # text when the browser sends voxtera-language before
        # voxtera-ready. None means "static greeting, ignore language
        # switches" (preserves the original behaviour for callers that
        # don't pass a map).
        self._greetings_map = greetings_map
        # Debounce window: if voxtera-ready fires twice within this many
        # seconds, don't queue a second greeting.
        self._debounce_secs = debounce_secs
        self._last_greeting_at: float | None = None

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            logger.info("[greeting] received DailyInputTransportMessageFrame: {}", msg)
            if isinstance(msg, dict):
                # Update greeting text based on the user's selected language
                # BEFORE voxtera-ready fires. The demo page sends these in
                # order: voxtera-stt → voxtera-language → voxtera-model →
                # voxtera-tts-provider → voxtera-voice → voxtera-ready.
                if msg.get("type") == "voxtera-language" and self._greetings_map:
                    lang = msg.get("language", "")
                    # "multi" = auto-detect; no preference, keep default.
                    if lang and lang != "multi":
                        new_text = self._greetings_map.get(lang)
                        if new_text and new_text != self._greeting_text:
                            logger.info("[greeting] updating greeting to language={!r}", lang)
                            self._greeting_text = new_text
                        elif not new_text:
                            logger.info(
                                "[greeting] no hardcoded greeting for language={!r}, "
                                "keeping current",
                                lang,
                            )
                elif msg.get("type") == "voxtera-ready":
                    now = time.monotonic()
                    if (
                        self._last_greeting_at is not None
                        and (now - self._last_greeting_at) < self._debounce_secs
                    ):
                        logger.warning(
                            "[greeting] dropped duplicate voxtera-ready "
                            "({:.2f}s after previous) to avoid overlapping greetings",
                            now - self._last_greeting_at,
                        )
                    else:
                        self._last_greeting_at = now
                        logger.info(
                            "[greeting] user ready — playing startup greeting " "({} chars)",
                            len(self._greeting_text),
                        )
                        await self.push_frame(
                            TTSSpeakFrame(text=self._greeting_text), FrameDirection.DOWNSTREAM
                        )

        await self.push_frame(frame, direction)
