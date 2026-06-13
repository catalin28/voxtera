"""Tests for the TTS number-pronunciation safety-net (voxtera.tts_normalize)."""

from __future__ import annotations

import pytest
from pipecat.utils.text.base_text_aggregator import AggregationType

from voxtera.tts_normalize import (
    NumberSafeTokenAggregator,
    normalize_numbers_for_tts,
)

# --- pure function: the leading-zero clock-time cases that get mangled --------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # The reported bug: a zero in the minutes gets dropped by the voice.
        ("Breakfast is at 12:00.", "Breakfast is at 12 0 0."),
        ("Your taxi is booked for 12:03.", "Your taxi is booked for 12 0 3."),
        ("Checkout closes at 9:05.", "Checkout closes at 9 0 5."),
        ("It opens at 08:00 sharp.", "It opens at 08 0 0 sharp."),
        # Period form (guests type "12.00" too).
        ("Dinner at 12.00 tonight.", "Dinner at 12 0 0 tonight."),
        # Multiple times in one sentence.
        ("Open 9:00 to 5:09.", "Open 9 0 0 to 5 0 9."),
    ],
)
def test_leading_zero_times_are_expanded(raw: str, expected: str) -> None:
    assert normalize_numbers_for_tts(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # No leading zero in minutes — Chirp reads these correctly, leave alone.
        "The spa closes at 12:45.",
        "Lunch is at 1:30.",
        # No digits at all — untouched, cheap path.
        "Of course, right this way.",
        # A price/decimal without a leading-zero fraction stays intact.
        "That is 12.50 euros.",
        # Plain counting numbers are fine spoken as-is.
        "There are 4 restaurants nearby.",
        # Bare year — not a HH:MM time, must not be rewritten.
        "Built in 2019.",
    ],
)
def test_non_target_text_is_unchanged(raw: str) -> None:
    assert normalize_numbers_for_tts(raw) == raw


def test_idempotent() -> None:
    once = normalize_numbers_for_tts("See you at 8:05.")
    assert normalize_numbers_for_tts(once) == once


# --- token aggregator: numbers must not be split across streamed tokens -------


async def _drain(agg: NumberSafeTokenAggregator, tokens: list[str]) -> str:
    """Feed tokens through the aggregator, normalize each emitted chunk the way
    the TTS transform does, and return the concatenated spoken text."""
    out: list[str] = []
    for tok in tokens:
        async for chunk in agg.aggregate(tok):
            out.append(normalize_numbers_for_tts(chunk.text))
    tail = await agg.flush()
    if tail is not None:
        out.append(normalize_numbers_for_tts(tail.text))
    return "".join(out)


async def test_split_time_token_is_kept_whole() -> None:
    agg = NumberSafeTokenAggregator(aggregation_type=AggregationType.TOKEN)
    # "12:00" arrives split across three deltas — the killer case.
    spoken = await _drain(agg, ["Breakfast at ", "12", ":", "00", " sharp."])
    assert "12 0 0" in spoken
    assert "12:00" not in spoken


async def test_time_at_end_is_flushed() -> None:
    agg = NumberSafeTokenAggregator(aggregation_type=AggregationType.TOKEN)
    # Number is the very last thing in the stream — must be released on flush.
    spoken = await _drain(agg, ["Your room is ready at ", "9", ":", "05"])
    assert spoken.endswith("9 0 5")


async def test_non_numeric_stream_passes_through_unbuffered() -> None:
    agg = NumberSafeTokenAggregator(aggregation_type=AggregationType.TOKEN)
    tokens = ["Of ", "course, ", "right ", "this ", "way."]
    spoken = await _drain(agg, tokens)
    assert spoken == "Of course, right this way."


async def test_no_held_text_after_full_drain() -> None:
    agg = NumberSafeTokenAggregator(aggregation_type=AggregationType.TOKEN)
    await _drain(agg, ["Taxi at ", "12", ":", "03", "."])
    # Buffer fully released — nothing stuck for the next utterance.
    assert agg._text == ""  # noqa: SLF001
