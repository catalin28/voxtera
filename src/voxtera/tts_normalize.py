"""TTS number normalization — a safety-net for digit/time pronunciation.

The primary fix for bad number pronunciation is the system prompt rule that
asks Claude to write numbers, times, dates and prices out in words (see
``prompts/system_prompt.md``). This module is the *second* layer: a
deterministic guard that rewrites any digit time-string that still reaches the
TTS, so the model slipping up once does not produce mangled audio.

The bug it targets shows up on the streaming TTS voices Voxtera uses
(ElevenLabs Flash v2.5 in production, also Google Chirp 3 HD): a clock time
whose minutes carry a leading zero ("12:00", "12:03", "9:05") is read with the
zero dropped — "twelve three" instead of "twelve oh three". Times with no zero
("12:45") are read correctly, so we only touch the zero cases and leave
everything else untouched to avoid mangling prices or ordinary decimals.

The normalizer is attached to every TTS provider (see ``attach_number_normalizer``
calls in ``tts.py``), so it follows whichever voice the runtime selects.

How it plugs in
---------------
:func:`normalize_numbers_for_tts` is a pure function (easy to unit-test).
:func:`attach_number_normalizer` wires it onto a Pipecat TTS service via
``add_text_transformer`` — Pipecat applies text transforms to the audio path
ONLY: the original ``AggregatedTextFrame`` is pushed downstream *before* the
transform runs (see ``TTSService._push_tts_frames``), so transcripts and the
assistant context keep the original "12:00" while the spoken audio gets the
expanded form.

The streaming wrinkle
---------------------
With ``TextAggregationMode.TOKEN`` (which Voxtera uses for low latency) the TTS
aggregator yields each LLM token immediately, so "12:00" can arrive split as
"12", ":", "00" and the transform would never see it whole. To prevent that we
swap in :class:`NumberSafeTokenAggregator`, which holds back a trailing fragment
that is still "growing" into a number until the next token (or stream flush)
arrives. It behaves identically to the stock aggregator for all non-numeric
text, so the latency impact is limited to numeric runs (≤ one token).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable

from loguru import logger
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

# A clock time whose minutes start with a zero: "12:00", "9:05", "12.03".
# - lookbehind/lookahead keep us off longer digit runs (IP-like "1.0.03",
#   version "12.0", phone digits) so we only fire on a clean HH:MM / HH.MM.
# - minutes are restricted to ``0\d`` (00–09): exactly the dropped-zero bug.
#   "12:45" and the price "12.50" have no leading zero and are left alone.
# - the trailing ``(?!\d)`` blocks only a following digit (so we don't grab
#   part of "12.000" or "12:00:30"); a sentence-ending "." after the time
#   ("Breakfast at 12:00.") is fine and must still match.
_LEADING_ZERO_TIME = re.compile(r"(?<![\d.,:])(\d{1,2})[:.](0\d)(?!\d)")

# A trailing run that could still be growing into a number across tokens:
# a digit followed by any digits/time-separators, anchored at the end of the
# buffer. "12", "12:", "12:0", "12:00" all match; "12:00." (sentence end) does
# not, so a completed time followed by punctuation is released promptly.
_TRAILING_NUMERIC = re.compile(r"\d[\d:.]*$")


def _expand_leading_zero_time(match: re.Match[str]) -> str:
    """Rewrite "12:03" → "12 0 3": hour kept as a number, minutes split into
    single digits so the leading zero is always voiced. Language-neutral —
    each digit is read in whatever language the TTS voice is speaking."""
    hour, minutes = match.group(1), match.group(2)
    spaced_minutes = " ".join(minutes)  # "03" → "0 3", "00" → "0 0"
    return f"{hour} {spaced_minutes}"


def normalize_numbers_for_tts(text: str) -> str:
    """Expand leading-zero clock times into a TTS-safe spoken form.

    Pure and side-effect-free. Returns ``text`` unchanged when there is nothing
    to fix (the common case), so it is cheap to call on every chunk.

    Examples:
        >>> normalize_numbers_for_tts("Breakfast is at 12:00.")
        'Breakfast is at 12 0 0.'
        >>> normalize_numbers_for_tts("Your taxi is booked for 8:05.")
        'Your taxi is booked for 8 0 5.'
        >>> normalize_numbers_for_tts("The spa closes at 12:45.")  # no zero → untouched
        'The spa closes at 12:45.'
    """
    if ":" not in text and "." not in text:
        return text
    return _LEADING_ZERO_TIME.sub(_expand_leading_zero_time, text)


class NumberSafeTokenAggregator(SimpleTextAggregator):
    """TOKEN-mode aggregator that never splits a numeric run across chunks.

    Drop-in replacement for the stock :class:`SimpleTextAggregator` when a TTS
    service streams tokens. For non-numeric text it yields immediately, exactly
    like the base class. When the buffer ends in a fragment that could still be
    growing into a number (a digit, or a digit + ``:``/``.``) it holds that
    fragment back until the next token completes it, so the number normalizer
    downstream always sees e.g. "12:00" whole rather than "12" / ":" / "00".

    SENTENCE mode is delegated unchanged to the base class.
    """

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        if self._aggregation_type != AggregationType.TOKEN:
            async for agg in super().aggregate(text):
                yield agg
            return

        if not text:
            return
        self._text += text
        match = _TRAILING_NUMERIC.search(self._text)
        if match is None:
            # Nothing number-like at the tail — release the whole buffer.
            ready, self._text = self._text, ""
        elif match.start() == 0:
            # The entire buffer is one growing numeric run — hold all of it.
            ready = ""
        else:
            # Release everything up to the trailing numeric fragment; keep it.
            ready, self._text = self._text[: match.start()], self._text[match.start() :]
        if ready:
            yield Aggregation(text=ready, type=AggregationType.TOKEN)

    async def flush(self) -> Aggregation | None:
        # End of stream: emit any held-back trailing number.
        if self._aggregation_type == AggregationType.TOKEN:
            if self._text:
                ready, self._text = self._text, ""
                return Aggregation(text=ready, type=AggregationType.TOKEN)
            return None
        return await super().flush()


async def _transform(text: str, aggregation_type: AggregationType | str) -> str:
    return normalize_numbers_for_tts(text)


def attach_number_normalizer(tts: object, *, enabled: bool = True) -> None:
    """Wire the number normalizer onto a Pipecat TTS service.

    Registers the audio-only text transform and, for token-streaming services,
    swaps in :class:`NumberSafeTokenAggregator` so split tokens don't defeat it.
    No-op (with a debug log) when ``enabled`` is False, so it can be turned off
    in production via TTS_NUMBER_NORMALIZE without a code change. Safe to call
    on any TTS service; unknown internals are tolerated.
    """
    if not enabled:
        logger.debug("[tts-normalize] disabled via settings — not attaching")
        return

    add_transformer: Callable[..., None] | None = getattr(tts, "add_text_transformer", None)
    if not callable(add_transformer):
        logger.warning(
            "[tts-normalize] {} has no add_text_transformer — number normalizer not attached",
            type(tts).__name__,
        )
        return
    add_transformer(_transform, "*")

    # Keep numeric runs intact when the service streams tokens.
    mode = getattr(tts, "_text_aggregation_mode", None)
    if mode == AggregationType.TOKEN or getattr(mode, "value", None) == "token":
        try:
            tts._text_aggregator = NumberSafeTokenAggregator(aggregation_type=mode)  # noqa: SLF001
            swapped = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tts-normalize] could not swap token aggregator: {}", exc)
            swapped = False
    else:
        swapped = False
    logger.info(
        "[tts-normalize] attached to {} (token-safe aggregator={})",
        type(tts).__name__,
        swapped,
    )
