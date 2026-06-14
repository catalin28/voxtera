"""Incremental [OFFER:<id>] tag handling for streamed voice TTS.

split_streamable lets the voice call path stream render deltas to TTS
sentence-by-sentence while never voicing a half-formed hidden offer tag.
"""

from __future__ import annotations

from voxtera.whatsapp.offer_tags import split_streamable


def test_plain_text_streams_through_whole() -> None:
    assert split_streamable("Good evening, how can I help?") == (
        "Good evening, how can I help?",
        "",
    )


def test_complete_tag_is_stripped_inline() -> None:
    emit, hold = split_streamable("Here is the menu. [OFFER:tugra_menu]")
    assert "[OFFER" not in emit
    assert "Here is the menu." in emit
    assert hold == ""


def test_forming_tag_is_held_back() -> None:
    # The tag is only partially streamed so far — must not be spoken yet.
    emit, hold = split_streamable("Lovely choice. [OFFER:tug")
    assert emit == "Lovely choice. "
    assert hold == "[OFFER:tug"


def test_bare_open_bracket_prefix_is_held() -> None:
    emit, hold = split_streamable("See the spa [")
    assert emit == "See the spa "
    assert hold == "["


def test_tag_reassembled_across_two_chunks() -> None:
    # Simulate streaming: chunk 1 leaves a forming tag held; chunk 2 closes it.
    emit1, hold1 = split_streamable("The pool is lovely. [OFFER:")
    assert emit1 == "The pool is lovely. "
    assert hold1 == "[OFFER:"
    emit2, hold2 = split_streamable(hold1 + "pool_area]")
    assert emit2 == ""  # the whole reassembled tag is stripped, nothing to speak
    assert hold2 == ""


def test_benign_bracket_not_an_offer_streams_through() -> None:
    # A "[" that can't become "[OFFER:" should not stall streaming.
    emit, hold = split_streamable("Room 12 [note] is ready")
    assert emit == "Room 12 [note] is ready"
    assert hold == ""
