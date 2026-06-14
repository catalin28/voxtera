"""Fuzzy venue-name resolver — recover from STT mis-hearing foreign proper
nouns ("Tuğra" → "Tura"/"Tula") so the menu lookup can still find the venue.

Cases are taken from the real failing WhatsApp trace (2026-06-13) plus guard
cases that must stay untouched. No network, no model — pure string logic over
the curated venue list in config/stt_vocabulary.json.
"""

from __future__ import annotations

import pytest

from voxtera.call_center.entity_resolver import canonicalize_venues

H = "kempinski_ciragan"


@pytest.mark.parametrize(
    "utterance, expected_fragment",
    [
        # Real mis-hearings from the trace.
        ("I want to know what is the menu for the restaurant Tura?", "Tuğra"),
        ("No, I want the menu for Tula.", "Tuğra"),
        ("Do you have the menu for Tugra?", "Tuğra"),
        # Other venues + phonetic variants.
        ("Tell me about Ruya", "Ruya İstanbul"),
        ("show me the gazibo menu", "Gazebo"),
        ("is le fumua open tonight", "Le Fumoir"),
        ("a table at belini please", "Bellini"),
    ],
)
def test_misheard_venue_is_canonicalized(utterance: str, expected_fragment: str) -> None:
    out, subs = canonicalize_venues(utterance, H)
    assert expected_fragment in out, f"{utterance!r} -> {out!r}"
    assert subs and subs[0].canonical.startswith(expected_fragment.split()[0])
    assert subs[0].score >= 0.80


@pytest.mark.parametrize(
    "utterance",
    [
        "what time is breakfast",
        "I want a table for two please",
        "is the spa open",
        "can I get the menu",
        "you hit the menu for two",  # garbled, but no confident venue match
        "tell me about your restaurants",
        "do you have room service",
    ],
)
def test_ordinary_words_are_left_untouched(utterance: str) -> None:
    out, subs = canonicalize_venues(utterance, H)
    assert out == utterance
    assert subs == []


def test_exact_canonical_name_is_noop() -> None:
    out, subs = canonicalize_venues("book a table at Tuğra", H)
    assert out == "book a table at Tuğra"
    assert subs == []


def test_no_hotel_is_noop() -> None:
    assert canonicalize_venues("menu for Tula", None) == ("menu for Tula", [])


def test_unknown_hotel_is_noop() -> None:
    out, subs = canonicalize_venues("menu for Tula", "no_such_hotel")
    assert out == "menu for Tula"
    assert subs == []


def test_empty_utterance_is_noop() -> None:
    assert canonicalize_venues("", H) == ("", [])


def test_substitution_metadata_is_populated() -> None:
    _out, subs = canonicalize_venues("the menu for Tula please", H)
    assert len(subs) == 1
    s = subs[0]
    assert s.canonical == "Tuğra"
    assert "tula" in s.heard.lower()
    assert 0.80 <= s.score <= 1.0
