"""Per-language Whisper confidence thresholds.

Loads thresholds from a JSON file and exposes per-language lookup. The lookup
chain is:

1. Resolve the detected language to a canonical ISO 639-1 short code.
2. Look up the canonical code in the loaded JSON config.
3. If not found, fall back to the JSON's ``"default"`` entry.
4. If the JSON itself is missing/malformed (or has no ``"default"``), fall
   back to the hardcoded :data:`_HARDCODED_FALLBACK` so the bot keeps working
   even if the config file is missing or broken.

The thresholds are used by :class:`voxtera.stt._MultilingualWhisperSTT` to
drop low-confidence transcriptions before they reach the LLM. Two signals
are tracked per Whisper segment: ``avg_logprob`` (per-token log-probability
average — Whisper's own confidence score) and ``no_speech_prob`` (probability
that the segment is non-speech audio).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

# Hard-coded last-resort defaults. Used only when the JSON file is missing,
# malformed, or doesn't include a "default" entry. Lenient by design — better
# to let an unknown language through than to silence the bot at the worst
# moment of a demo.
_HARDCODED_AVG_LOGPROB_MIN: float = -1.0
_HARDCODED_NO_SPEECH_PROB_MAX: float = 0.7


@dataclass(frozen=True)
class LangThresholds:
    """Confidence thresholds for a single language."""

    avg_logprob_min: float
    no_speech_prob_max: float


# Whisper returns ``language`` as either a full English name (``"english"``,
# ``"romanian"``) or a BCP-47/ISO 639-1 short code (``"en"``, ``"ro"``)
# depending on the response format. This map normalises both forms to a
# canonical ISO 639-1 two-letter code so the JSON config can use a single
# stable key per language.
_CANONICAL_LANG: dict[str, str] = {
    # Full English names (lowercase) — what Whisper typically returns
    # in verbose_json for the "language" field.
    "afrikaans": "af",
    "arabic": "ar",
    "azerbaijani": "az",
    "bulgarian": "bg",
    "catalan": "ca",
    "chinese": "zh",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "finnish": "fi",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "icelandic": "is",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "latvian": "lv",
    "lithuanian": "lt",
    "malay": "ms",
    "norwegian": "no",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "romanian": "ro",
    "russian": "ru",
    "serbian": "sr",
    "slovak": "sk",
    "slovenian": "sl",
    "spanish": "es",
    "swedish": "sv",
    "thai": "th",
    "turkish": "tr",
    "ukrainian": "uk",
    "urdu": "ur",
    "vietnamese": "vi",
    # ISO 639-1 short codes (already canonical) — listed explicitly so
    # `_to_canonical("en")` works without reaching the prefix-split path.
    "af": "af",
    "ar": "ar",
    "az": "az",
    "bg": "bg",
    "ca": "ca",
    "zh": "zh",
    "hr": "hr",
    "cs": "cs",
    "da": "da",
    "nl": "nl",
    "en": "en",
    "et": "et",
    "fi": "fi",
    "fr": "fr",
    "de": "de",
    "el": "el",
    "he": "he",
    "hi": "hi",
    "hu": "hu",
    "is": "is",
    "id": "id",
    "it": "it",
    "ja": "ja",
    "ko": "ko",
    "lv": "lv",
    "lt": "lt",
    "ms": "ms",
    "no": "no",
    "fa": "fa",
    "pl": "pl",
    "pt": "pt",
    "ro": "ro",
    "ru": "ru",
    "sr": "sr",
    "sk": "sk",
    "sl": "sl",
    "es": "es",
    "sv": "sv",
    "th": "th",
    "tr": "tr",
    "uk": "uk",
    "ur": "ur",
    "vi": "vi",
}


def _to_canonical(lang: str | None) -> str | None:
    """Resolve any Whisper language representation to ISO 639-1 short code.

    Accepts full English names (``"english"``), short codes (``"en"``), and
    BCP-47-style strings (``"en-US"``, ``"en_GB"``). Returns ``None`` for
    unknown or empty inputs.
    """
    if not lang:
        return None
    raw = str(lang).strip().lower()
    if not raw:
        return None
    direct = _CANONICAL_LANG.get(raw)
    if direct is not None:
        return direct
    # Try just the primary subtag (e.g. "en" from "en-US" or "en_GB").
    primary = raw.split("-", 1)[0].split("_", 1)[0]
    return _CANONICAL_LANG.get(primary)


class STTThresholds:
    """Per-language confidence thresholds for Whisper transcriptions.

    Use :meth:`load` to construct from a JSON file path. The constructor is
    public mostly for tests; production code should use :meth:`load`.
    """

    def __init__(
        self,
        per_language: Mapping[str, LangThresholds],
        default: LangThresholds,
        path: Path | None = None,
    ) -> None:
        self._per_language: dict[str, LangThresholds] = dict(per_language)
        self._default: LangThresholds = default
        self._path: Path | None = path
        # Track unknown languages we've already warned about so the log
        # doesn't fill up if the user code-switches into many languages.
        self._unknown_seen: set[str] = set()

    @classmethod
    def hardcoded_fallback(cls) -> LangThresholds:
        """Return the hardcoded last-resort default. Useful for tests."""
        return LangThresholds(
            avg_logprob_min=_HARDCODED_AVG_LOGPROB_MIN,
            no_speech_prob_max=_HARDCODED_NO_SPEECH_PROB_MAX,
        )

    @classmethod
    def load(cls, path: str | Path | None) -> STTThresholds:
        """Load thresholds from a JSON file.

        Falls back to the hardcoded defaults (and an empty per-language map)
        if the path is ``None``, the file is missing, or the JSON is
        malformed. The bot continues to work in all of those cases — the
        filter just becomes uniformly lenient.
        """
        fallback = cls.hardcoded_fallback()

        if path is None:
            logger.warning(
                "[stt-thresholds] no path configured; using hardcoded defaults "
                "(avg_logprob_min={}, no_speech_prob_max={})",
                fallback.avg_logprob_min,
                fallback.no_speech_prob_max,
            )
            return cls(per_language={}, default=fallback, path=None)

        p = Path(path).expanduser()
        if not p.exists():
            logger.warning(
                "[stt-thresholds] config file {!s} not found; using hardcoded defaults", p
            )
            return cls(per_language={}, default=fallback, path=p)

        try:
            raw_text = p.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "[stt-thresholds] failed to parse {!s}: {}; using hardcoded defaults",
                p,
                exc,
            )
            return cls(per_language={}, default=fallback, path=p)

        if not isinstance(data, dict):
            logger.error(
                "[stt-thresholds] {!s} must be a JSON object at the top level; "
                "using hardcoded defaults",
                p,
            )
            return cls(per_language={}, default=fallback, path=p)

        # The "default" entry is special — it's the fallback for languages
        # not explicitly listed in the JSON.
        default = cls._parse_entry(data.get("default"), origin=f"{p}#default") or fallback
        if data.get("default") is None:
            logger.warning(
                "[stt-thresholds] no 'default' entry in {!s}; "
                "using hardcoded fallback for unknown languages",
                p,
            )

        per_language: dict[str, LangThresholds] = {}
        for key, value in data.items():
            if key == "default":
                continue
            canonical = _to_canonical(key)
            if canonical is None:
                logger.warning(
                    "[stt-thresholds] unrecognised language key {!r} in {!s}; "
                    "ignored. Use ISO 639-1 short codes (en, fr, ro, …) or full "
                    "English names (english, french, romanian, …).",
                    key,
                    p,
                )
                continue
            parsed = cls._parse_entry(value, origin=f"{p}#{key}")
            if parsed is not None:
                per_language[canonical] = parsed

        logger.info(
            "[stt-thresholds] loaded {} per-language entries from {!s} "
            "(default: avg_logprob_min={}, no_speech_prob_max={})",
            len(per_language),
            p,
            default.avg_logprob_min,
            default.no_speech_prob_max,
        )
        return cls(per_language=per_language, default=default, path=p)

    @staticmethod
    def _parse_entry(value: object, *, origin: str) -> LangThresholds | None:
        """Parse a single entry. Returns None on malformed input (with a log)."""
        if value is None:
            return None
        if not isinstance(value, dict):
            logger.warning(
                "[stt-thresholds] entry at {} must be a JSON object; ignored",
                origin,
            )
            return None
        try:
            return LangThresholds(
                avg_logprob_min=float(value["avg_logprob_min"]),
                no_speech_prob_max=float(value["no_speech_prob_max"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "[stt-thresholds] invalid entry at {}: {}; ignored. "
                "Required keys: avg_logprob_min (float), no_speech_prob_max (float).",
                origin,
                exc,
            )
            return None

    def for_language(self, lang: str | None) -> LangThresholds:
        """Return thresholds for ``lang``, falling back to the default entry.

        Logs once per unknown language to flag misconfiguration without
        spamming the log when a user code-switches.
        """
        canonical = _to_canonical(lang)
        if canonical is not None and canonical in self._per_language:
            return self._per_language[canonical]
        if canonical is not None and canonical not in self._unknown_seen:
            self._unknown_seen.add(canonical)
            logger.info(
                "[stt-thresholds] no per-language entry for {!r}, using default "
                "(avg_logprob_min={}, no_speech_prob_max={})",
                canonical,
                self._default.avg_logprob_min,
                self._default.no_speech_prob_max,
            )
        return self._default

    def reload(self) -> None:
        """Reload thresholds from the original path. No-op if no path is set."""
        if self._path is None:
            logger.warning("[stt-thresholds] reload called but no path is configured; ignored")
            return
        new = STTThresholds.load(self._path)
        self._per_language = new._per_language
        self._default = new._default
        self._unknown_seen.clear()
        logger.info("[stt-thresholds] reloaded from {!s}", self._path)

    @property
    def path(self) -> Path | None:
        """Path the thresholds were loaded from, or None if hardcoded only."""
        return self._path

    @property
    def default(self) -> LangThresholds:
        """The default thresholds used when a language isn't explicitly listed."""
        return self._default

    def configured_languages(self) -> list[str]:
        """Return the list of language codes that have explicit entries."""
        return sorted(self._per_language)
