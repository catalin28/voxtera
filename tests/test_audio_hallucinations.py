"""Unit tests for the multilingual silence-hallucination detector in audio.py.

The detector is the cheap pre-filter that drops STT output before it
becomes a turn. We test the *pure* helper ``_is_known_whisper_hallucination``
so the test suite stays fast (no Pipecat frames, no async).
"""

from __future__ import annotations

import re

import pytest

from voxtera.audio import _is_known_whisper_hallucination


def _norm(text: str) -> str:
    """Match the normalization the production caller applies."""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


@pytest.mark.parametrize(
    "utterance",
    [
        # Turkish — the bug from PSTN logs.
        "Altyazı M.K.",
        "Altyazı: Çeviri:",
        "Altyazılar A.B.",
        # English subtitle credits.
        "Subtitles by the Amara.org community",
        "Closed captions by xyz",
        # French.
        "Sous-titres par ABC",
        "Sous-titrage Société Radio-Canada",
        # Spanish / Portuguese.
        "Subtítulos por XYZ",
        "Legendas por ABC",
        # German.
        "Untertitel von ABC",
        # Italian.
        "Sottotitoli a cura di XYZ",
        # Russian.
        "Субтитры от ABC",
        "Перевод от ABC",
    ],
)
def test_subtitle_credit_patterns_are_dropped(utterance: str) -> None:
    """All known subtitle/caption/translation credit lines are silence
    hallucinations — Whisper learned them from subtitled training video."""
    assert _is_known_whisper_hallucination(_norm(utterance)) is True


@pytest.mark.parametrize(
    "utterance",
    [
        # A guest asking about subtitles in a movie room — different shape,
        # contains other content. The current heuristic still flags this if
        # the token appears at all in a short utterance, which is fine for
        # the hotel-concierge domain (this question is exceedingly rare and
        # the guest will rephrase). We assert real guest questions WITHOUT
        # the subtitle tokens are not dropped.
        "Do you have a spa?",
        "What time is breakfast?",
        "Can I book a table for two?",
        "Tugra restaurant menu please",
        "Merhaba, hamam fiyatları nedir?",
        # Single-word greetings — these are handled by OTHER guards
        # (semantic relevance, language consistency); the credit detector
        # alone must not flag them.
        "Hello",
        "Merhaba",
        "Bonjour",
    ],
)
def test_real_guest_utterances_pass(utterance: str) -> None:
    assert _is_known_whisper_hallucination(_norm(utterance)) is False


def test_long_utterance_with_subtitle_token_is_not_dropped() -> None:
    """The credit detector only fires on SHORT utterances (≤20 tokens).
    A long, on-topic guest question that happens to use 'subtitles' once
    must not be dropped — only the silence-credit pattern is the target.
    """
    # 21+ tokens, on-topic — must pass.
    long_utterance = (
        "I would like to know whether the rooms have a television that supports "
        "english subtitles or maybe other settings I should ask about please"
    )
    assert len(long_utterance.split()) > 20
    assert _is_known_whisper_hallucination(_norm(long_utterance)) is False
