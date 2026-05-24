"""Runtime controllers driven by browser app-messages.

Each ``FrameProcessor`` here listens for a specific
``DailyInputTransportMessageFrame`` envelope and reconfigures the live
pipeline without restarting it:

- :class:`LanguageSwitcher` — handles ``{type: 'voxtera-language', ...}`` and
  reconfigures the active STT branch via :class:`STTUpdateSettingsFrame`.
- :class:`AutoTTSLanguageSwitcher` — observes :class:`TranscriptionFrame`
  language detection and updates the active Google Chirp 3 HD TTS to the
  matching locale (preserving the voice character).
- :class:`InstantAckFiller` — observes :class:`TranscriptionFrame` and plays
  a short, language-matched backchannel the instant the guest stops speaking,
  masking the LLM/TTS latency gap with natural speech instead of silence.
- :class:`InterruptionResumer` — observes barge-ins; when the guest cuts the
  bot off early, injects a context note so the answering LLM can decide
  whether to return to the unfinished answer or drop it.
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
import random
import time

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
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

from voxtera.prompts.fillers import FILLERS
from voxtera.prompts.greetings import DEFAULT_LANGUAGE, daypart_for_timezone
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
    if provider == "gladia":
        # Gladia Solaria-1 advertises 99+ languages with native
        # auto-detection. Treat the same broad set as Whisper as
        # supported — anything we expose in the UI dropdown.
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
                        elif active_provider == "gladia":
                            # Pipecat's GladiaSTTService._update_settings stores
                            # a settings delta but does NOT reapply it to the
                            # live websocket (see upstream TODO in
                            # pipecat/services/gladia/stt.py), so the standard
                            # STTUpdateSettingsFrame path is a no-op. Instead,
                            # call the lazy-connect subclass's helper to close
                            # and reopen the session with new language_config.
                            reconfigure = getattr(active_stt, "reconfigure_languages", None)
                            if reconfigure is None:
                                logger.warning(
                                    "[lang-switch] gladia service missing "
                                    "reconfigure_languages — language change "
                                    "won't take effect on the live socket"
                                )
                            else:
                                await reconfigure([lang], code_switching=False)
                            await self.push_frame(frame, direction)
                            return
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
    * **ElevenLabs Flash v2.5**: same pattern as Cartesia — voices are
      multilingual, only the per-utterance ``language`` parameter is updated.
      Languages outside Flash v2.5's 32-language list are silently skipped.
      Note: this only works on the ``eleven_flash_v2_5`` and
      ``eleven_turbo_v2_5`` models — older models (v2, multilingual_v2)
      detect language from text and ignore the parameter.
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

    # ElevenLabs Flash v2.5 / Turbo v2.5 supported languages (ISO 639-1).
    # Source: Pipecat's language_to_elevenlabs_language() LANGUAGE_MAP in
    # pipecat.services.elevenlabs.tts (matches the 32 languages listed at
    # https://elevenlabs.io/docs/overview/capabilities/text-to-speech).
    # ElevenLabs voices are multilingual: the same voice ID produces audio
    # in any of these languages when ``language`` is set per-utterance.
    _ELEVENLABS_LANGUAGES: frozenset[str] = frozenset(
        {
            "ar",
            "bg",
            "cs",
            "da",
            "de",
            "el",
            "en",
            "es",
            "fi",
            "fil",
            "fr",
            "hi",
            "hr",
            "hu",
            "id",
            "it",
            "ja",
            "ko",
            "ms",
            "nl",
            "no",
            "pl",
            "pt",
            "ro",
            "ru",
            "sk",
            "sv",
            "ta",
            "tr",
            "uk",
            "vi",
            "zh",
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

    @classmethod
    def _elevenlabs_lang_for_bcp47(cls, bcp47: str):
        """Convert a BCP-47 code to a Pipecat ``Language`` enum supported
        by ElevenLabs Flash v2.5 / Turbo v2.5, or ``None`` if unsupported.

        Same pattern as :meth:`_cartesia_lang_for_bcp47`. Lazy import so
        the dep only loads when ElevenLabs is actually active.

        Special case: Filipino is ``Language.FIL`` (not ``FI``, which is
        Finnish), so we map "fil" before the generic upper-case lookup.
        """
        if not bcp47:
            return None
        code = bcp47.split("-")[0].lower()
        if code not in cls._ELEVENLABS_LANGUAGES:
            return None
        try:
            from pipecat.transcriptions.language import Language

            # Filipino has a 3-letter ISO 639-2 code in the Language enum.
            if code == "fil":
                return getattr(Language, "FIL", None)
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
            and self._tts_router.active_provider in ("google", "cartesia", "elevenlabs")
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
                    elif active_provider == "cartesia":
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
                    else:  # elevenlabs
                        eleven_lang = self._elevenlabs_lang_for_bcp47(bcp47)
                        if eleven_lang is None:
                            logger.info(
                                "[tts-lang] elevenlabs: {!r} not in Flash v2.5's "
                                "32-language list, keeping current TTS language",
                                bcp47,
                            )
                            # Update tracked lang anyway so we don't retry every
                            # frame for the same unsupported language.
                            self._current_lang = bcp47
                        else:
                            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

                            logger.info(
                                "[tts-lang] detected={!r} → ElevenLabs language={!r} "
                                "(voice unchanged)",
                                bcp47,
                                getattr(eleven_lang, "value", eleven_lang),
                            )
                            await self.push_frame(
                                TTSUpdateSettingsFrame(
                                    delta=ElevenLabsTTSService.Settings(language=eleven_lang),
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


class InstantAckFiller(FrameProcessor):
    """Plays a short acknowledgment the instant the guest finishes speaking.

    A voice turn has a hard latency floor: STT commit + end-of-turn detection
    + LLM TTFT + TTS TTFA is ≈ 2.5 s even on fast hardware. This processor
    does not make that faster — it *masks* it. The moment a final
    :class:`TranscriptionFrame` arrives it pushes a short pre-written
    backchannel (e.g. "One moment.", "Let me check that.") to the TTS as a
    :class:`TTSSpeakFrame`. That audio starts playing within ~100 ms while
    the LLM is still generating, so the guest hears a natural human-style
    acknowledgment instead of dead silence.

    The filler runs fully in PARALLEL with the LLM/RAG work and is short
    enough (~0.6-1.3 s) that it finishes around when the real answer is
    ready — it never delays the answer.

    Language: the filler is spoken in whatever language the STT detected for
    *this* utterance (``TranscriptionFrame.language`` — the same signal
    :class:`AutoTTSLanguageSwitcher` uses to switch the TTS). There is no
    separate detection step. A language with no curated pool plays *no*
    filler — a wrong-language acknowledgment is worse than silence.

    Placement: must sit AFTER :class:`AutoTTSLanguageSwitcher` (so the TTS is
    already on the guest's language when the filler's :class:`TTSSpeakFrame`
    reaches it) and BEFORE ``context_aggregator.user()`` (which consumes
    :class:`TranscriptionFrame`, so anything downstream never sees the
    trigger).

    Anti-annoyance measures:

    * Turns shorter than ``min_words`` (default 3) are skipped — a verbose
      "let me check that" in front of a one-word "yes" / "thanks" is both
      pointless and socially odd, and skipping them cuts filler frequency on
      rapid back-and-forth.
    * The same phrase is never played twice in a row for a given language
      (per-language recent-phrase memory over a varied pool), so the filler
      never sounds like a robotic tic.
    """

    def __init__(self, *, min_words: int = 3) -> None:
        super().__init__()
        self._min_words = min_words
        # Last phrase played per language — so we never repeat back-to-back.
        self._last_phrase: dict[str, str] = {}
        # Last language a filler was played in — fallback when a transcript
        # arrives with no language tag (rare, but the STT can return None).
        self._last_lang: str | None = None

    @staticmethod
    def _lang_code(lang_val: object) -> str | None:
        """Normalise any language representation to a 2-letter code.

        Reuses :meth:`AutoTTSLanguageSwitcher._resolve_lang` (which already
        handles Whisper full names, BCP-47 codes and ``Language`` enums) and
        strips the region subtag: ``Language.RO`` / ``"romanian"`` /
        ``"ro-RO"`` all collapse to ``"ro"``.
        """
        bcp47 = AutoTTSLanguageSwitcher._resolve_lang(lang_val)
        if not bcp47:
            return None
        return bcp47.split("-")[0].lower()

    def _pick(self, lang_code: str) -> str | None:
        """Pick a filler for ``lang_code``, never repeating the last one."""
        pool = FILLERS.get(lang_code)
        if not pool:
            return None
        last = self._last_phrase.get(lang_code)
        choices = [p for p in pool if p != last] or list(pool)
        phrase = random.choice(choices)
        self._last_phrase[lang_code] = phrase
        return phrase

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            text = (getattr(frame, "text", "") or "").strip()
            # Skip trivially short turns — see the class docstring.
            if len(text.split()) >= self._min_words:
                lang_code = self._lang_code(getattr(frame, "language", None)) or self._last_lang
                if lang_code:
                    phrase = self._pick(lang_code)
                    if phrase:
                        self._last_lang = lang_code
                        logger.info("[filler] lang={!r} → {!r}", lang_code, phrase)
                        # Push the filler downstream FIRST so it races ahead
                        # toward the TTS while the transcript below still has
                        # to traverse the context aggregator + LLM.
                        await self.push_frame(TTSSpeakFrame(text=phrase), FrameDirection.DOWNSTREAM)
                    else:
                        logger.debug(
                            "[filler] no curated pool for language {!r} — staying silent",
                            lang_code,
                        )

        await self.push_frame(frame, direction)


# Out-of-band note injected into the LLM context after a barge-in truncates a
# reply. Phrased explicitly as a system-style directive (NOT as guest speech)
# so the model treats it as stage direction rather than something the guest
# said. The additive-vs-dismissal classification is left to the answering LLM,
# which does it in the guest's own language — no keyword tables, no extra call.
_RESUME_NOTE = (
    "[System note — not spoken by the guest: your previous reply was cut off "
    "mid-sentence when the guest began speaking. Judge the guest's most recent "
    "message: if it ADDS a request (e.g. 'and also...', 'what about...'), "
    "answer the new request first, then briefly finish the cut-off point "
    "without repeating what the guest already heard; if it DISMISSES or "
    "REPLACES the topic (e.g. 'no', 'stop', 'actually...'), drop the cut-off "
    "point and answer only the new message. Keep the whole reply brief.]"
)


class InterruptionResumer(FrameProcessor):
    """Lets the bot return to an answer the guest cut off mid-sentence.

    When ``allow_interruptions`` is on, a guest can barge in over the bot.
    Pipecat handles the barge-in correctly — TTS stops, the in-flight LLM
    generation is cancelled — but the *partial* reply is then simply
    abandoned. If the guest's interruption was additive ("...and tell me about
    the spa too") rather than a dismissal ("no, stop"), the bot drops the first
    topic and never returns to it, leaving the guest with half an answer.

    This processor does not classify the interruption itself. It supplies the
    *signal* the answering LLM needs to classify it:

    1. It watches :class:`BotStartedSpeakingFrame` to time the reply, and
       :class:`InterruptionFrame` to catch a barge-in landing while the bot is
       still speaking.
    2. If the barge-in lands within ``_resume_window_secs`` of the reply
       starting — early enough that meaningful content was left unsaid — it
       arms a one-shot flag. A barge-in past that window is treated as a
       near-complete answer and ignored: resuming a nearly-finished thought
       ("as I was saying...") sounds worse than dropping it.
    3. On the next :class:`TranscriptionFrame` (the interrupting utterance) it
       injects :data:`_RESUME_NOTE` into the LLM context via
       :class:`LLMMessagesAppendFrame`. The note tells the model its previous
       reply was truncated and instructs it to judge — in the guest's own
       language — whether the new utterance *adds* a request or *replaces* it.

    The additive-vs-replacement classification is therefore performed by the
    same LLM that produces the answer: no extra round trip, no per-language
    keyword tables, ~0 added latency.

    Placement: must sit downstream of STT (to see :class:`TranscriptionFrame`)
    and upstream of ``context_aggregator.user()`` (so the injected note lands
    in context before the LLM run) — i.e. right next to :class:`InstantAckFiller`.

    Live-tunable: ``_enabled`` and ``_resume_window_secs`` are mutated in place
    by the ``interruption_resume_enabled`` / ``interruption_resume_window_secs``
    knobs (see :func:`voxtera.tunables.register_pipeline_knobs`).
    """

    def __init__(self, *, enabled: bool = True, resume_window_secs: float = 5.0) -> None:
        super().__init__()
        # Live-tunable: flipped/retuned by the interruption_resume_* knobs.
        self._enabled = enabled
        self._resume_window_secs = resume_window_secs
        # Monotonic timestamp of the most recent BotStartedSpeakingFrame, or
        # None when the bot isn't speaking. Used to measure how far into a
        # reply a barge-in landed.
        self._bot_started_at: float | None = None
        # One-shot: set when a barge-in truncates a reply early enough to be
        # worth resuming; consumed by the next TranscriptionFrame.
        self._pending_resume = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_started_at = time.monotonic()

        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Reply finished cleanly — nothing was truncated. Clear state so a
            # stale flag can't fire on an unrelated later turn.
            self._bot_started_at = None
            self._pending_resume = False

        elif isinstance(frame, InterruptionFrame):
            # A barge-in. Only meaningful if the bot was actually speaking (a
            # start time is recorded) and the feature is enabled.
            if self._enabled and self._bot_started_at is not None:
                elapsed = time.monotonic() - self._bot_started_at
                if elapsed <= self._resume_window_secs:
                    self._pending_resume = True
                    logger.info(
                        "[interruption-resumer] barge-in {:.1f}s into reply "
                        "(window {:.1f}s) — arming resume note",
                        elapsed,
                        self._resume_window_secs,
                    )
                else:
                    logger.debug(
                        "[interruption-resumer] barge-in {:.1f}s into reply "
                        "exceeds window {:.1f}s — reply near-complete, not resuming",
                        elapsed,
                        self._resume_window_secs,
                    )
            self._bot_started_at = None

        elif (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
            and self._pending_resume
        ):
            # The interrupting utterance has been transcribed. Inject the
            # out-of-band note BEFORE letting the transcript continue, so the
            # note lands in context ahead of the user turn that triggers the
            # LLM run. One-shot: cleared here so a multi-segment utterance
            # doesn't inject the note more than once.
            self._pending_resume = False
            await self.push_frame(
                LLMMessagesAppendFrame([{"role": "user", "content": _RESUME_NOTE}]),
                FrameDirection.DOWNSTREAM,
            )
            logger.info("[interruption-resumer] injected resume note into context")

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
                        elif active_provider == "elevenlabs":
                            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

                            # ElevenLabs treats voice as a URL field, so changing
                            # it forces a websocket reconnect under the hood (see
                            # ElevenLabsTTSSettings.URL_FIELDS). That's handled
                            # by pipecat — we just push the new value.
                            delta = ElevenLabsTTSService.Settings(voice=voice)
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

    Greeting selection (resolved lazily, when voxtera-ready fires, so the
    language and timezone messages may arrive in any order):

    * Language — the browser sends ``voxtera-language`` before
      ``voxtera-ready``. The selected language picks the row in
      ``greetings_map`` / ``timed_greetings``. ``"multi"`` (auto-detect) or a
      language with no hardcoded greeting keeps the boot ``default_language``.
    * Time of day — the browser sends ``voxtera-timezone`` with its IANA
      timezone (e.g. ``"Europe/Paris"``). When present, the matching
      morning/afternoon/evening variant from ``timed_greetings`` is spoken.
      With no timezone (phone line, Telegram, an older widget) the
      time-neutral greeting from ``greetings_map`` is used instead. The
      server clock is never used — it is UTC and says nothing about the guest.

    If no ``greetings_map`` is supplied the controller plays the static
    ``greeting_text`` it was constructed with — the original behaviour, kept
    for callers that don't need per-language / per-time greetings.
    """

    def __init__(
        self,
        greeting_text: str,
        *,
        debounce_secs: float = 3.0,
        greetings_map: dict[str, str] | None = None,
        timed_greetings: dict[str, dict[str, str]] | None = None,
        default_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        super().__init__()
        # Static fallback greeting — used when no greetings_map is supplied, or
        # as a last resort if a language somehow isn't in the catalog.
        self._greeting_text = greeting_text
        # Per-language time-neutral catalog (lang -> text). None means "static
        # greeting, ignore language/time" (preserves the original behaviour).
        self._greetings_map = greetings_map
        # Per-language time-of-day catalog (lang -> {morning/afternoon/evening}).
        self._timed_greetings = timed_greetings or {}
        # Guest state, learned from browser app-messages before voxtera-ready.
        self._language = default_language
        self._timezone: str | None = None
        # Debounce window: if voxtera-ready fires twice within this many
        # seconds, don't queue a second greeting.
        self._debounce_secs = debounce_secs
        self._last_greeting_at: float | None = None

    def _resolve_greeting_text(self) -> str:
        """Pick the greeting to speak from the current language + timezone."""
        # No catalog: static greeting (preserves the original contract).
        if not self._greetings_map:
            return self._greeting_text

        lang = self._language if self._language in self._greetings_map else DEFAULT_LANGUAGE

        # Time-aware variant when the browser reported the guest's timezone.
        daypart = daypart_for_timezone(self._timezone)
        if daypart:
            timed = self._timed_greetings.get(lang)
            if timed and daypart in timed:
                return timed[daypart]

        # Time-neutral fallback (unknown/absent timezone, or no timed variant).
        return self._greetings_map.get(lang, self._greeting_text)

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, DailyInputTransportMessageFrame):
            msg = frame.message
            logger.info("[greeting] received DailyInputTransportMessageFrame: {}", msg)
            if isinstance(msg, dict):
                msg_type = msg.get("type")
                # The demo page sends, before voxtera-ready:
                #   voxtera-stt → voxtera-language → voxtera-timezone →
                #   voxtera-model → voxtera-tts-provider → voxtera-voice.
                # Language and timezone are just stored here; the greeting is
                # resolved lazily at voxtera-ready, so arrival order is moot.
                if msg_type == "voxtera-language":
                    lang = msg.get("language", "")
                    # "multi" = auto-detect; no preference, keep the default.
                    if lang and lang != "multi":
                        if self._greetings_map and lang not in self._greetings_map:
                            logger.info(
                                "[greeting] no hardcoded greeting for language={!r}, "
                                "keeping {!r}",
                                lang,
                                self._language,
                            )
                        else:
                            self._language = lang
                            logger.info("[greeting] guest language set to {!r}", lang)
                elif msg_type == "voxtera-timezone":
                    tz = msg.get("tz")
                    if tz:
                        self._timezone = tz
                        logger.info("[greeting] guest timezone set to {!r}", tz)
                elif msg_type == "voxtera-ready":
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
                        text = self._resolve_greeting_text()
                        logger.info(
                            "[greeting] user ready — playing startup greeting "
                            "(language={!r} timezone={!r} daypart={!r} {} chars)",
                            self._language,
                            self._timezone,
                            daypart_for_timezone(self._timezone),
                            len(text),
                        )
                        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM)

        await self.push_frame(frame, direction)
