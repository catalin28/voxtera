"""Mic-side audio processors.

All ``FrameProcessor`` subclasses that touch mic audio or shape transcripts
on their way to the LLM:

- :class:`RNNoiseDenoiser` — optional pre-VAD denoiser (env knob
  ``RNNOISE_ENABLED``).
- :class:`PlaybackLeakageGuard` — silences mic energy while the bot is
  speaking so VAD doesn't barge in on the bot's own voice.
- :class:`BotActiveUserFrameSuppressor` — drops user-turn frames during bot
  speech in the strict (no-interruptions) configuration.
- :class:`AudioLevelMonitor` — quiet diagnostic; logs mic RMS at DEBUG only.
- :class:`TranscriptionNoiseFilter` — generic structural + semantic filter
  for STT output (no hardcoded phrase lists).
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher

import numpy as np
from loguru import logger

try:
    from pyrnnoise import RNNoise
except Exception:  # pragma: no cover - optional dependency at runtime
    RNNoise = None
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    STTUpdateSettingsFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voxtera.prompts import SYSTEM_PROMPT
from voxtera.trace import DropAccumulator as _DropAccumulator


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


# Known multilingual Whisper hallucinations on silent / unclear audio.
# These appear verbatim in Whisper's training data (YouTube/podcast outros)
# and Whisper "transcribes" them when there's nothing else to latch onto.
# Compared as exact, lower-cased, punctuation-stripped strings.
_WHISPER_HALLUCINATION_EXACT: frozenset[str] = frozenset(
    {
        # English
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
        # Romanian (observed in the wild — see VOX-* logs).
        "poți să vă abonați și să vă lăsați un like",
        "poti sa va abonati si sa va lasati un like",
        "abonațivă și lăsați un like",
        "abonati va si lasati un like",
        "mulțumesc pentru vizionare",
        "multumesc pentru vizionare",
        "să vă mulțumim pentru vizionare",
        "sa va multumim pentru vizionare",
        "vă mulțumim pentru vizionare",
        "va multumim pentru vizionare",
        "mulțumim pentru vizionare",
        "multumim pentru vizionare",
        # French
        "abonnez vous",
        "abonnez vous à ma chaîne",
        "merci d avoir regardé",
        "n oubliez pas de vous abonner",
        # Spanish
        "suscríbete a mi canal",
        "suscribete a mi canal",
        "dale like y suscríbete",
        "dale like y suscribete",
        "gracias por ver",
        # Italian
        "iscriviti al mio canale",
        "grazie per aver guardato",
        # German
        "abonniert meinen kanal",
        "danke fürs zuschauen",
        "danke fuers zuschauen",
        # Portuguese
        "se inscreva no canal",
        "obrigado por assistir",
        # Turkish
        "izlediğiniz için teşekkürler",
        "izlediginiz icin tesekkurler",
        "abone olmayı unutmayın",
        "abone olmayi unutmayin",
        "kanalıma abone olun",
        "kanalima abone olun",
        # Russian
        "спасибо за просмотр",
        "подписывайтесь на канал",
        "ставьте лайк",
        "не забудьте подписаться",
    }
)

# Multilingual keyword set for the "subscribe + like" YouTube-outro pattern.
# Whisper produces this in countless surface forms; rather than enumerate
# every variant, we scan the normalized transcription for ANY of these
# language-specific tokens. If two of them co-occur in a short utterance
# the message is almost certainly a YouTube hallucination, regardless of
# the exact wording.
_SUBSCRIBE_TOKENS: frozenset[str] = frozenset(
    {
        # English
        "subscribe",
        # Romanian
        "abonati",
        "abonați",
        "abonează",
        "aboneaza",
        "abonatie",
        "abonatii",
        # French
        "abonner",
        "abonnez",
        "abonnement",
        # Spanish
        "suscríbete",
        "suscribete",
        "suscribirse",
        "suscribir",
        # Italian
        "iscriviti",
        "iscrivetevi",
        # German
        "abonniert",
        "abonniere",
        # Portuguese
        "inscreva",
        "inscrever",
        # Turkish
        "abone",
        # Russian
        "подписывайтесь",
        "подпишитесь",
        "подписаться",
        "подписки",
    }
)
_LIKE_TOKENS: frozenset[str] = frozenset(
    {
        # English
        "like",
        # Romanian
        "lăsați",
        "lasati",
        "apreciați",
        "apreciati",
        # French
        "aimer",
        "pouce",
        # Spanish
        "manita",
        # Turkish
        "beğen",
        "begen",
        "beğenin",
        "begenin",
        # Russian
        "лайк",
        "лайки",
        # German / Italian / Portuguese borrow English "like" too.
    }
)


# Known multilingual "see more videos on our website/channel" hallucination.
# Like the subscribe+like pattern, STT models emit this video-platform
# call-to-action verbatim on silent / unclear audio (observed in the wild:
# "Voir plus de vidéos sur le site web de l'Université d'Ottawa"). It is
# caught by the co-occurrence of THREE token classes — a viewing verb /
# "more", a "video" noun, and a site/channel word. All three are required
# so genuine guest questions that mention only one ("is there a video?",
# "your website?", "I want to watch videos") are never dropped.
_VIDEO_VIEW_TOKENS: frozenset[str] = frozenset(
    {
        # English
        "see",
        "watch",
        "more",
        # French
        "voir",
        "regarder",
        "plus",
        # Romanian
        "vedeti",
        "vedeți",
        "vizionare",
        "vizionati",
        "vizionați",
        "urmariti",
        "urmăriți",
        "mai",
        "multe",
        # Spanish
        "ver",
        "mira",
        "más",
        "mas",
        # German
        "sehen",
        "weitere",
        "mehr",
        # Italian
        "guarda",
        "guardare",
        "altri",
        "altro",
        # Portuguese
        "veja",
        "assista",
        "mais",
        # Turkish
        "izle",
        "izleyin",
        "izlemek",
        "daha",
        "fazla",
        # Russian
        "смотрите",
        "смотреть",
        "больше",
        "ещё",
        "еще",
    }
)
_VIDEO_NOUN_TOKENS: frozenset[str] = frozenset(
    {
        "video",
        "videos",
        "vidéo",
        "vidéos",
        "vídeo",
        "vídeos",
        "videoclip",
        "videoclipuri",
        "videolar",
        "видео",
    }
)
_VIDEO_SITE_TOKENS: frozenset[str] = frozenset(
    {
        # site / website
        "site",
        "website",
        "web",
        "sitio",
        "webseite",
        "sitesi",
        "siteul",
        "siteului",
        "siteuri",
        "сайт",
        "сайте",
        "сайта",
        # channel
        "channel",
        "chaîne",
        "chaine",
        "canal",
        "canale",
        "kanal",
        "canalul",
        "kanalı",
        "kanalima",
        "канал",
        "канале",
        "канала",
    }
)


def _is_known_whisper_hallucination(normalized: str) -> bool:
    """Return True if the normalized text matches a known multilingual hallucination.

    `normalized` should already be lower-cased and punctuation-stripped.

    Three checks:
    1. Exact match against :data:`_WHISPER_HALLUCINATION_EXACT`.
    2. Co-occurrence of a "subscribe" token and a "like" token in a short
       utterance — catches every YouTube-outro hallucination an STT model
       produces, in any language, without needing to enumerate variants.
    3. Co-occurrence of a viewing verb / "more", a "video" noun and a
       site/channel token — catches the "see more videos on our website"
       hallucination family. All three classes are required to avoid
       dropping genuine guest speech.
    """
    if normalized in _WHISPER_HALLUCINATION_EXACT:
        return True
    tokens = set(normalized.split())
    if not tokens:
        return False
    short = len(tokens) <= 20
    # "thank you for watching" pattern in any language — a hotel guest would
    # never say this. Requires BOTH a thanks token AND a viewing token.
    thanks_tokens = {
        "mulțumim",
        "multumim",
        "mulțumesc",
        "multumesc",
        "mulțumiri",
        "thanks",
        "thank",
        "gracias",
        "merci",
        "danke",
        "grazie",
        "obrigado",
        "спасибо",
        "teşekkürler",
        "tesekkurler",
    }
    for_watching_tokens = {
        "vizionare",
        "watching",
        "regardé",
        "zuschauen",
        "guardato",
        "assistir",
        "просмотр",
    }
    if short and (tokens & thanks_tokens) and (tokens & for_watching_tokens):
        return True
    # YouTube-outro "subscribe + like" pattern.
    if short and (tokens & _SUBSCRIBE_TOKENS) and (tokens & _LIKE_TOKENS):
        return True
    # "See more videos on our website / channel" pattern.
    return bool(
        short
        and (tokens & _VIDEO_VIEW_TOKENS)
        and (tokens & _VIDEO_NOUN_TOKENS)
        and (tokens & _VIDEO_SITE_TOKENS)
    )


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
        self._domain_vec_ready = False
        self._last_text = ""
        self._last_text_at = 0.0
        # Language consistency tracking: detect suspicious switches on short
        # utterances which are almost always Whisper hallucinations.
        self._prev_language: str | None = None
        self._consistent_lang_turns: int = 0

    def _ensure_domain_vec(self) -> None:
        """Lazily compute the domain embedding on first use."""
        if self._domain_vec_ready:
            return
        self._domain_vec_ready = True
        try:
            from voxtera.rag.embeddings import embed_sync

            self._embed_sync = embed_sync
            vec = np.asarray(self._embed_sync([SYSTEM_PROMPT])[0], dtype=np.float32)
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
        self._ensure_domain_vec()
        if self._domain_vec is None or self._embed_sync is None:
            return None
        try:
            v = np.asarray(self._embed_sync([text])[0], dtype=np.float32)
            return _cosine_similarity(v, self._domain_vec)
        except Exception:
            return None

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, STTUpdateSettingsFrame):
            # User explicitly switched language — reset language tracking so
            # the very next utterance in the new language is not blocked by
            # the consistency guard.
            self._prev_language = None
            self._consistent_lang_turns = 0
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            if not text:
                return

            logger.debug("[stt-filter] raw transcription: {!r}", text)

            # Whisper has well-known hallucinations from YouTube/podcast training
            # data that appear on silent/unclear audio. Drop them unconditionally.
            #
            # We use TWO checks: an exact-match set for common single phrases,
            # and a substring/keyword scan for the universal "subscribe and
            # like" YouTube-outro pattern, which Whisper produces in every
            # language it knows (Romanian "abonați", French "abonner",
            # Spanish "suscríbete", etc.).
            normalized = re.sub(r"[^\w\s]", "", text.lower()).strip()
            if _is_known_whisper_hallucination(normalized):
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
                # Short hallucinations (1-2 words) with low relevance — classic
                # Whisper artifacts from noise bursts ("SHINY!", "Love that.").
                # 3-4 token phrases can be valid user questions ("Can you hear me?").
                if sim < 0.25 and token_count <= 2:
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
                    if token_count_lang <= 2 and self._consistent_lang_turns >= 3:
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
        # Monotonic timestamps for when each flag was set, used by the
        # watchdog below. None when the flag is False.
        self._bot_speaking_set_at: float | None = None
        self._bot_thinking_set_at: float | None = None
        self._cooldown_until = 0.0
        self._barge_in_open = False
        self._barge_in_frames = 0

        # Adaptive baseline for ambient/noise leakage level.
        self._noise_floor = 0.005
        self._noise_floor_alpha = 0.02

        # Auto-tuned gate settings. These defaults are tuned to minimize
        # word-truncation at the start of follow-up utterances (the user
        # speaking immediately after the bot stops). Previous settings
        # (0.25s cooldown, 8 frames, 0.045 RMS) lost ~200ms of audio at
        # the start of each utterance because:
        #  * cooldown forced audio through the barge-in gate path
        #  * the gate required ~160ms of high-RMS frames to open
        #  * the RMS threshold was tuned for laptop mics (AirPods/headsets
        #    produce slightly lower normalized RMS)
        # Symptom: Whisper transcribes "What time does breakfast start?" as
        # "Does breakfast start?" or hallucinates "Time does break for Scott".
        # Lowered values trade a small TTS-echo-protection margin (only
        # relevant for speaker-based mics, not headphones/AirPods) for
        # clean transcripts on follow-up turns.
        self._open_ratio = 3.5
        self._min_open_rms = 0.025
        self._required_open_frames = 3  # ~60ms at 20ms frames
        self._post_tts_cooldown_secs = 0.10

        # Watchdog timeouts: how long a state flag can stay True without
        # receiving its corresponding end frame before we force-clear it.
        # A real bot utterance rarely exceeds 6s, so 20s on _bot_speaking
        # catches missed TTSStopped/BotStopped frames quickly while still
        # leaving margin for legitimately long replies (greetings with
        # ElevenLabs/Cartesia can approach 10s). _bot_thinking gets
        # more room because LLM responses can stream for a while before
        # the End frame fires.
        self._max_bot_speaking_secs = 20.0
        self._max_bot_thinking_secs = 20.0

        # Drop counter — feeds the dashboard's per-stage "X muted" indicator
        # so it's obvious when leakage suppression is eating user audio.
        # action="silenced" because we push zeroed frames rather than dropping.
        self._drops = _DropAccumulator(stage="leakage_guard", action="silenced")

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

        # All state-transition events are logged at INFO with the "[lg-state]"
        # prefix so they can be grepped from production logs to debug "deaf"
        # incidents. Format: "[lg-state] <FrameType> → <new state>".
        if isinstance(frame, BotStartedSpeakingFrame):
            # CRITICAL: only stamp the watchdog timestamp on a False→True
            # transition. Pipecat sometimes emits multiple BotStartedSpeaking
            # frames during a single bot turn (per-sentence aggregator). If we
            # re-stamp on every one, the watchdog timer keeps resetting and
            # never fires — leaving the guard stuck silencing user audio
            # indefinitely when the matching Stopped frame is lost.
            was_speaking = self._bot_speaking
            if not was_speaking:
                self._bot_speaking_set_at = now
            self._bot_speaking = True
            self._barge_in_open = False
            self._barge_in_frames = 0
            logger.info(
                "[lg-state] BotStartedSpeaking → speaking=True (was={})",
                was_speaking,
            )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStoppedSpeakingFrame):
            was_speaking = self._bot_speaking
            self._bot_speaking = False
            self._bot_speaking_set_at = None
            self._barge_in_open = False
            self._barge_in_frames = 0
            self._cooldown_until = now + self._post_tts_cooldown_secs
            logger.info(
                "[lg-state] BotStoppedSpeaking → speaking=False (was={})",
                was_speaking,
            )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            # Treat thinking as bot-active for leakage suppression; otherwise
            # background speech can cancel the in-flight reply and yield <empty>.
            # Same False→True guard as BotStartedSpeaking — protects the
            # watchdog timestamp from being reset by intermediate Start frames.
            was_thinking = self._bot_thinking
            if not was_thinking:
                self._bot_thinking_set_at = now
            self._bot_thinking = True
            self._barge_in_open = False
            self._barge_in_frames = 0
            logger.info(
                "[lg-state] LLMFullResponseStart → thinking=True (was={})",
                was_thinking,
            )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            was_thinking = self._bot_thinking
            self._bot_thinking = False
            self._bot_thinking_set_at = None
            logger.info(
                "[lg-state] LLMFullResponseEnd → thinking=False (was={})",
                was_thinking,
            )
            await self.push_frame(frame, direction)
            return

        # Some transports/services emit TTS lifecycle more reliably than bot
        # speaking frames. Handle both so state never gets stuck.
        if isinstance(frame, TTSStartedFrame):
            # Same False→True guard: per-sentence TTSStartedFrame is common
            # for multi-sentence replies, and re-stamping would defeat the
            # watchdog. Only stamp when actually transitioning to speaking.
            was_speaking = self._bot_speaking
            if not was_speaking:
                self._bot_speaking_set_at = now
            self._bot_speaking = True
            self._barge_in_open = False
            self._barge_in_frames = 0
            logger.info(
                "[lg-state] TTSStarted → speaking=True (was={})",
                was_speaking,
            )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TTSStoppedFrame):
            was_speaking = self._bot_speaking
            self._bot_speaking = False
            self._bot_speaking_set_at = None
            self._barge_in_open = False
            self._barge_in_frames = 0
            self._cooldown_until = now + self._post_tts_cooldown_secs
            logger.info(
                "[lg-state] TTSStopped → speaking=False (was={})",
                was_speaking,
            )
            await self.push_frame(frame, direction)
            return

        # Pipecat fires InterruptionFrame when a user barge-in cancels an
        # in-flight bot turn. The LLM stream and TTS may be cancelled
        # without their End frames ever firing, leaving _bot_thinking
        # (and sometimes _bot_speaking) stuck True — which silences ALL
        # subsequent user audio via the gate logic below. We force-reset
        # state on InterruptionFrame so the next user utterance is heard.
        if isinstance(frame, InterruptionFrame):
            was_speaking = self._bot_speaking
            was_thinking = self._bot_thinking
            self._bot_speaking = False
            self._bot_speaking_set_at = None
            self._bot_thinking = False
            self._bot_thinking_set_at = None
            self._barge_in_open = False
            self._barge_in_frames = 0
            self._cooldown_until = now + self._post_tts_cooldown_secs
            logger.info(
                "[lg-state] InterruptionFrame → speaking=False thinking=False "
                "(was speaking={}, thinking={})",
                was_speaking,
                was_thinking,
            )
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InputAudioRawFrame) and frame.audio:
            samples = np.frombuffer(frame.audio, dtype=np.int16)
            if not samples.size:
                await self.push_frame(frame, direction)
                return

            # Watchdog: clear flags that have been stuck True longer than
            # their max plausible duration. Protects against the "End frame
            # got lost" failure mode (LLM stream cancelled mid-flight, TTS
            # disconnected, transport drop without InterruptionFrame, etc.)
            # which would otherwise leave the guard silencing user audio
            # forever. Logged at WARNING level so a recurring fire is
            # visible in production logs.
            if self._bot_speaking and self._bot_speaking_set_at is not None:
                speaking_age = now - self._bot_speaking_set_at
                if speaking_age > self._max_bot_speaking_secs:
                    logger.warning(
                        "[leakage-guard] _bot_speaking watchdog fired after "
                        "{:.1f}s without TTSStopped/BotStopped — clearing",
                        speaking_age,
                    )
                    self._bot_speaking = False
                    self._bot_speaking_set_at = None
                    self._cooldown_until = now + self._post_tts_cooldown_secs
            if self._bot_thinking and self._bot_thinking_set_at is not None:
                thinking_age = now - self._bot_thinking_set_at
                if thinking_age > self._max_bot_thinking_secs:
                    logger.warning(
                        "[leakage-guard] _bot_thinking watchdog fired after "
                        "{:.1f}s without LLMFullResponseEnd — clearing",
                        thinking_age,
                    )
                    self._bot_thinking = False
                    self._bot_thinking_set_at = None

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

            # Gate audio to suppress TTS playback echo reaching VAD/STT.
            # Strict mode (no interruptions): always suppress.
            # Interruptions enabled: use the RMS barge-in gate so genuine
            # near-field speech opens the gate while speaker echo stays closed.
            open_threshold = max(self._noise_floor * self._open_ratio, self._min_open_rms)

            if not self._allow_interruptions:
                # Never open while bot is active.
                silent = np.zeros_like(samples, dtype=np.int16)
                output = self._clone_audio_frame(frame, silent.tobytes())
                self._drops.record("bot_active_strict")
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

            # Suppress TTS playback leakage before it reaches VAD.
            silent = np.zeros_like(samples, dtype=np.int16)
            output = self._clone_audio_frame(frame, silent.tobytes())
            # Reason taxonomy: distinguish the two "active" flag sources so
            # the dashboard tooltip tells us WHICH End frame is missing on
            # the next "deaf" incident. "bot_active" alone hid that detail.
            if self._bot_speaking and self._bot_thinking:
                reason = "bot_speaking_and_thinking"
            elif self._bot_speaking:
                reason = "bot_speaking"
            elif self._bot_thinking:
                reason = "bot_thinking"
            else:
                reason = "barge_in_closed"
            self._drops.record(reason)
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
        # Watchdog: timestamp when _bot_active was last set True and the
        # max duration we'll allow it to stay True without an end frame.
        self._bot_active_set_at: float | None = None
        self._max_bot_active_secs = 60.0  # covers LLM + TTS round-trip
        # Drop counter — action="dropped" because we return without pushing
        # (unlike leakage_guard which silences-then-pushes).
        self._drops = _DropAccumulator(stage="suppressor", action="dropped")

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.monotonic()

        # Watchdog: clear _bot_active if it's been stuck True longer than
        # max_bot_active_secs. Same failure mode as leakage_guard — the
        # End frame can go missing and this flag would otherwise stay True
        # forever, dropping every user-turn frame.
        if self._bot_active and self._bot_active_set_at is not None:
            active_age = now - self._bot_active_set_at
            if active_age > self._max_bot_active_secs:
                logger.warning(
                    "[barge-in] _bot_active watchdog fired after {:.1f}s "
                    "without an end frame — clearing",
                    active_age,
                )
                self._bot_active = False
                self._bot_active_set_at = None

        if isinstance(frame, LLMFullResponseStartFrame | BotStartedSpeakingFrame | TTSStartedFrame):
            if not self._bot_active:
                self._bot_active_set_at = now
            self._bot_active = True
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame | BotStoppedSpeakingFrame | TTSStoppedFrame):
            self._bot_active = False
            self._bot_active_set_at = None
            await self.push_frame(frame, direction)
            return

        # Interruption cancels the bot turn — the LLM/TTS End frames may not
        # fire, so explicitly clear _bot_active here. Without this, all
        # subsequent user-turn frames get dropped until the next clean
        # End frame happens to fire (the "deaf after interruption" bug).
        if isinstance(frame, InterruptionFrame):
            if self._bot_active:
                logger.info(
                    "[barge-in] InterruptionFrame → bot_active cleared "
                    "(user-turn frames will pass through again)"
                )
            self._bot_active = False
            self._bot_active_set_at = None
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
            self._drops.record(f"bot_active:{frame.__class__.__name__}")
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


# ---------------------------------------------------------------------- #
# Spectral pre-emphasis for Bluetooth devices                             #
# ---------------------------------------------------------------------- #


class HighShelfPreEmphasis(FrameProcessor):
    """Applies a high-shelf boost to compensate for Bluetooth narrowband roll-off.

    Bluetooth headsets (AirPods, Jabra, etc.) clamp high frequencies during
    quiet passages. A +5 dB shelf above 2 kHz restores energy that STT
    models need for consonant discrimination.

    The filter is enabled/disabled at runtime via :meth:`set_enabled` when
    the browser reports ``device_kind == "bluetooth"``.

    Performance: uses a biquad IIR filter (6 multiplies + 4 adds per sample).
    At 16 kHz / 20ms frames (320 samples), this adds <0.05ms per frame.
    Tracing logs cumulative stats every 250 frames (~5s).
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        gain_db: float = 5.0,
        frequency: float = 2000.0,
    ) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._gain_db = gain_db
        self._frequency = frequency
        self._enabled = False  # off until bluetooth detected

        # Biquad filter state (2 delay elements for mono)
        self._z1 = 0.0
        self._z2 = 0.0

        # Pre-compute coefficients
        self._b0 = 1.0
        self._b1 = 0.0
        self._b2 = 0.0
        self._a1 = 0.0
        self._a2 = 0.0
        self._compute_coefficients()

        # Tracing
        self._frame_count = 0
        self._total_process_us = 0.0
        self._max_process_us = 0.0
        self._trace_interval = 250  # log every 250 frames (~5s at 50fps)

    def _compute_coefficients(self) -> None:
        """Compute biquad high-shelf filter coefficients (Audio EQ Cookbook)."""
        amp = 10 ** (self._gain_db / 40.0)  # amplitude from dB
        w0 = 2.0 * np.pi * self._frequency / self._sample_rate
        cos_w0 = np.cos(w0)
        sin_w0 = np.sin(w0)
        alpha = sin_w0 / 2.0 * np.sqrt(2.0)  # Q = 0.707 (Butterworth)

        # High shelf coefficients
        ap1 = amp + 1.0
        am1 = amp - 1.0
        two_sqrt_a_alpha = 2.0 * np.sqrt(amp) * alpha

        b0 = amp * (ap1 + am1 * cos_w0 + two_sqrt_a_alpha)
        b1 = -2.0 * amp * (am1 + ap1 * cos_w0)
        b2 = amp * (ap1 + am1 * cos_w0 - two_sqrt_a_alpha)
        a0 = ap1 - am1 * cos_w0 + two_sqrt_a_alpha
        a1 = 2.0 * (am1 - ap1 * cos_w0)
        a2 = ap1 - am1 * cos_w0 - two_sqrt_a_alpha

        # Normalize
        self._b0 = b0 / a0
        self._b1 = b1 / a0
        self._b2 = b2 / a0
        self._a1 = a1 / a0
        self._a2 = a2 / a0

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the filter at runtime."""
        if enabled != self._enabled:
            self._enabled = enabled
            if enabled:
                # Reset filter state on enable to avoid transient
                self._z1 = 0.0
                self._z2 = 0.0
            logger.info(
                "[pre-emphasis] {} (high-shelf +{}dB @ {}Hz)",
                "ENABLED — bluetooth detected" if enabled else "disabled",
                self._gain_db,
                self._frequency,
            )

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, InputAudioRawFrame) or not self._enabled:
            await self.push_frame(frame, direction)
            return

        t0 = time.perf_counter_ns()

        # Convert to float32 for processing
        samples = np.frombuffer(frame.audio, dtype=np.int16).astype(np.float32)

        # Apply biquad filter (Direct Form II Transposed)
        output = np.empty_like(samples)
        z1, z2 = self._z1, self._z2
        b0, b1, b2 = self._b0, self._b1, self._b2
        a1, a2 = self._a1, self._a2

        for i in range(len(samples)):
            x = samples[i]
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            output[i] = y

        self._z1 = z1
        self._z2 = z2

        # Clip and convert back to int16
        output = np.clip(output, -32768.0, 32767.0).astype(np.int16)

        # Build output frame preserving metadata
        out_frame = InputAudioRawFrame(
            audio=output.tobytes(),
            sample_rate=frame.sample_rate,
            num_channels=frame.num_channels,
        )
        out_frame.pts = frame.pts
        out_frame.metadata = dict(frame.metadata) if frame.metadata else {}
        out_frame.transport_source = frame.transport_source
        out_frame.transport_destination = frame.transport_destination
        out_frame.broadcast_sibling_id = frame.broadcast_sibling_id

        elapsed_us = (time.perf_counter_ns() - t0) / 1000.0

        # Tracing
        self._frame_count += 1
        self._total_process_us += elapsed_us
        self._max_process_us = max(self._max_process_us, elapsed_us)

        if self._frame_count % self._trace_interval == 0:
            avg_us = self._total_process_us / self._frame_count
            logger.info(
                "[pre-emphasis] trace: {} frames processed | avg={:.1f}µs max={:.1f}µs per frame",
                self._frame_count,
                avg_us,
                self._max_process_us,
            )

        await self.push_frame(out_frame, direction)
