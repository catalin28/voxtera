"""Tests for the markdown-aware chunker."""

from __future__ import annotations

from voxtera.rag.chunker import _token_len, chunk_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _word_block(n_words: int) -> str:
    """Return a string of *n_words* simple words."""
    return " ".join(f"word{i}" for i in range(n_words))


def _sentence_block(n_sentences: int) -> str:
    """Return *n_sentences* distinct sentences."""
    return " ".join(f"Sentence number {i} is here." for i in range(n_sentences))


# ---------------------------------------------------------------------------
# Empty / trivial input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_string(self) -> None:
        assert chunk_text("") == []

    def test_whitespace_only(self) -> None:
        assert chunk_text("   \n\n  ") == []


class TestShortInput:
    def test_shorter_than_target(self) -> None:
        text = "Hello world."
        result = chunk_text(text)
        assert len(result) == 1
        assert result[0].text == text
        assert result[0].token_count == _token_len(text)

    def test_single_word(self) -> None:
        result = chunk_text("Bonjour")
        assert len(result) == 1
        assert result[0].text == "Bonjour"


# ---------------------------------------------------------------------------
# Multi-chunk splitting
# ---------------------------------------------------------------------------


class TestMultipleChunks:
    def test_no_chunk_exceeds_max_tokens(self) -> None:
        text = _word_block(400)
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) > 1
        for c in chunks:
            assert c.token_count <= 300, f"Chunk has {c.token_count} tokens (max 300)"

    def test_deterministic(self) -> None:
        text = _word_block(500)
        run1 = chunk_text(text)
        run2 = chunk_text(text)
        assert run1 == run2


# ---------------------------------------------------------------------------
# Overlap (sentence-boundary)
# ---------------------------------------------------------------------------


class TestOverlap:
    def test_adjacent_chunks_share_overlap(self) -> None:
        text = _sentence_block(60)
        chunks = chunk_text(text, target_tokens=100, max_tokens=200, overlap_tokens=20)
        assert len(chunks) >= 3
        for i in range(1, len(chunks)):
            prev_tail_words = chunks[i - 1].text.split()[-10:]
            cur_head_words = chunks[i].text.split()[:30]
            shared = set(prev_tail_words) & set(cur_head_words)
            assert len(shared) > 0, f"Chunks {i - 1} and {i} have no overlapping words"

    def test_no_overlap_on_first_chunk(self) -> None:
        text = _sentence_block(60)
        chunks = chunk_text(text, target_tokens=100, max_tokens=200, overlap_tokens=20)
        assert chunks[0].text.startswith("Sentence number 0")

    def test_overlap_ends_at_sentence_boundary(self) -> None:
        """Overlap should consist of complete sentences, not mid-word cuts."""
        text = (
            "The hotel has a pool. The spa opens at nine. "
            "Breakfast starts at seven. Lunch is served at noon. "
            "Dinner begins at six. The bar closes at midnight. "
            "Room service is available. The gym is on floor two. "
            "The lobby has free wifi. Check-out is at eleven."
        )
        chunks = chunk_text(text, target_tokens=20, max_tokens=60, overlap_tokens=15)
        if len(chunks) >= 2:
            # Overlap (start of chunk 2) should begin with an uppercase letter,
            # indicating a sentence start.
            second = chunks[1].text.lstrip()
            assert second[
                0
            ].isupper(), f"Overlap doesn't start at sentence boundary: {second[:40]!r}"


# ---------------------------------------------------------------------------
# Markdown heading context propagation
# ---------------------------------------------------------------------------


class TestHeadingContext:
    def test_heading_starts_new_chunk(self) -> None:
        text = (
            "Some intro paragraph with enough words to matter.\n\n"
            "# Main Heading\n\n"
            "Content under main heading.\n\n"
            "## Sub Heading\n\n"
            "Content under sub heading."
        )
        chunks = chunk_text(text, target_tokens=20, max_tokens=80, overlap_tokens=0)
        heading_chunks = [c for c in chunks if "# Main Heading" in c.text]
        assert len(heading_chunks) >= 1, "No chunk contains '# Main Heading'"

    def test_heading_not_split_mid_line(self) -> None:
        heading = "# Welcome to the Grand Hotel Barcelona Experience"
        text = heading + "\n\nSome body text here."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        found = any(heading in c.text for c in chunks)
        assert found, "Heading was split across chunks"

    def test_heading_context_prefix_appears(self) -> None:
        """Chunks under a heading should contain the ancestor heading path."""
        text = (
            "# Hotel Info\n\n"
            "## Restaurant\n\n"
            "The restaurant serves Mediterranean cuisine. "
            "Fresh ingredients are sourced from local farms. "
            "The chef has twenty years of experience cooking."
        )
        chunks = chunk_text(text, target_tokens=30, max_tokens=80, overlap_tokens=0)
        # Find a chunk that contains "Mediterranean" — it should also
        # have the heading context path.
        body_chunks = [c for c in chunks if "Mediterranean" in c.text]
        assert body_chunks, "No chunk contains 'Mediterranean'"
        ctx_chunk = body_chunks[0]
        assert (
            "Hotel Info" in ctx_chunk.text
        ), f"Missing ancestor heading 'Hotel Info' in: {ctx_chunk.text!r}"
        assert (
            "Restaurant" in ctx_chunk.text
        ), f"Missing heading 'Restaurant' in: {ctx_chunk.text!r}"

    def test_heading_hierarchy_resets_on_same_level(self) -> None:
        """When a heading at level 2 appears, previous level-2 is replaced."""
        text = (
            "# Hotel\n\n"
            "## Pool\n\n"
            "Olympic size pool.\n\n"
            "## Spa\n\n"
            "Full-service spa with sauna."
        )
        chunks = chunk_text(text, target_tokens=30, max_tokens=80, overlap_tokens=0)
        spa_chunks = [c for c in chunks if "sauna" in c.text]
        assert spa_chunks
        spa_chunk = spa_chunks[0]
        # Should have Hotel > Spa context, NOT Hotel > Pool > Spa.
        assert "Spa" in spa_chunk.text
        assert (
            "Pool" not in spa_chunk.text
        ), f"Stale heading 'Pool' found in spa chunk: {spa_chunk.text!r}"

    def test_previous_section_gets_correct_heading_context(self) -> None:
        """Content flushed when a new heading appears must keep the OLD context."""
        text = (
            "# Hotel\n\n"
            "## Restaurant\n\n"
            "Italian cuisine with fresh pasta, wood-fired pizza, "
            "and regional wines served daily.\n\n"
            "## Spa\n\n"
            "Deep tissue massage."
        )
        chunks = chunk_text(text, target_tokens=30, max_tokens=80, overlap_tokens=0)
        rest_chunks = [c for c in chunks if "Italian" in c.text]
        assert rest_chunks, "No chunk contains 'Italian'"
        rest_chunk = rest_chunks[0]
        assert (
            "Restaurant" in rest_chunk.text
        ), f"Missing 'Restaurant' in restaurant chunk: {rest_chunk.text!r}"
        assert (
            "Spa" not in rest_chunk.text
        ), f"Spa heading leaked into restaurant chunk: {rest_chunk.text!r}"


# ---------------------------------------------------------------------------
# Robust sentence splitting
# ---------------------------------------------------------------------------


class TestSentenceSplitting:
    def test_abbreviation_not_split(self) -> None:
        """Dr., Mr., etc. should not cause a false sentence break."""
        text = "Dr. Smith arrived at the hotel. He checked in immediately."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "Dr. Smith" in chunks[0].text

    def test_decimal_not_split(self) -> None:
        """Numbers like 4.5 should not cause a sentence break."""
        text = "The hotel has a 4.5 star rating. Guests love it."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "4.5 star" in chunks[0].text

    def test_real_sentence_boundary_still_works(self) -> None:
        """Actual sentence boundaries should still be detected."""
        text = (
            "The pool is heated. The gym is on the second floor. "
            "Breakfast is complimentary. Check-out is at noon."
        )
        # With very small target, should produce multiple chunks.
        chunks = chunk_text(text, target_tokens=10, max_tokens=30, overlap_tokens=0)
        assert len(chunks) >= 2

    # --- Multilingual abbreviation tests ---

    def test_spanish_abbreviation_not_split(self) -> None:
        """Sra. (Señora) should not cause a false sentence break."""
        text = "La Sra. García llegó al hotel. Ella reservó una suite."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "Sra. García" in chunks[0].text

    def test_french_abbreviation_not_split(self) -> None:
        """Mme. (Madame) should not cause a false sentence break."""
        text = "Mme. Dupont est arrivée à l'hôtel. Elle a réservé une chambre."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "Mme. Dupont" in chunks[0].text

    def test_german_abbreviation_not_split(self) -> None:
        """Nr. (Nummer) and ca. should not cause false breaks."""
        text = "Zimmer Nr. Dreizehn ist verfügbar. Ca. Zehn Gäste kommen heute."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "Nr. Dreizehn" in chunks[0].text

    def test_single_initial_not_split(self) -> None:
        """Single-letter initials (J. K. Rowling) should not split."""
        text = "J. K. Rowling stayed at the hotel. She enjoyed the spa."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "J. K. Rowling" in chunks[0].text

    def test_short_acronym_not_split(self) -> None:
        """Short all-caps acronyms (U.S., E.U.) should not split."""
        text = "The hotel follows E.U. Standards are very high."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300)
        assert len(chunks) == 1
        assert "E.U." in chunks[0].text


# ---------------------------------------------------------------------------
# List-aware splitting
# ---------------------------------------------------------------------------


class TestListAwareSplitting:
    def test_list_items_not_split_mid_item(self) -> None:
        """Each list item should be treated as an atomic unit."""
        text = (
            "## Amenities\n\n"
            "- Olympic-size swimming pool\n"
            "- Full-service spa and wellness center\n"
            "- State-of-the-art fitness gym\n"
            "- Business center with meeting rooms\n"
            "- Rooftop bar with panoramic views\n"
            "- Kids club and playground area"
        )
        chunks = chunk_text(text, target_tokens=30, max_tokens=80, overlap_tokens=0)
        for c in chunks:
            # No chunk should contain a partial list marker (dash at end of chunk
            # without the item text).
            lines = c.text.strip().split("\n")
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("- "):
                    # Should have content after the dash.
                    assert len(stripped) > 2, f"Partial list item: {stripped!r}"

    def test_ordered_list_items_preserved(self) -> None:
        """Ordered lists (1., 2.) should also be split at item boundaries."""
        text = (
            "1. Check in at reception\n"
            "2. Receive your room key\n"
            "3. Take the elevator to your floor\n"
            "4. Enjoy your stay"
        )
        chunks = chunk_text(text, target_tokens=15, max_tokens=40, overlap_tokens=0)
        assert len(chunks) >= 1
        # Each chunk should contain at least one complete list item.
        for c in chunks:
            assert any(
                c.text.strip().find(f"{i}.") >= 0 for i in range(1, 5)
            ), f"No complete list item found in: {c.text!r}"


# ---------------------------------------------------------------------------
# Token count accuracy
# ---------------------------------------------------------------------------


class TestTokenCount:
    def test_token_count_matches_text(self) -> None:
        text = _word_block(300)
        for c in chunk_text(text, target_tokens=100, max_tokens=200):
            assert c.token_count == _token_len(c.text)


# ---------------------------------------------------------------------------
# Table-aware chunking
# ---------------------------------------------------------------------------


class TestTableAwareChunking:
    def test_small_table_stays_atomic(self) -> None:
        """A table that fits within max_tokens is returned as one chunk."""
        table = (
            "| Room   | Price |\n"
            "|--------|-------|\n"
            "| Single | $100  |\n"
            "| Double | $180  |"
        )
        chunks = chunk_text(table, target_tokens=200, max_tokens=300, overlap_tokens=0)
        assert len(chunks) == 1
        assert "Single" in chunks[0].text
        assert "Double" in chunks[0].text

    def test_large_table_splits_with_header(self) -> None:
        """A large table should be split row-by-row with header repeated."""
        header = "| Feature | Details |\n|---------|---------|"
        rows = "\n".join(f"| Feature {i} | Description of feature {i} |" for i in range(30))
        table = header + "\n" + rows
        chunks = chunk_text(
            table,
            target_tokens=40,
            max_tokens=80,
            overlap_tokens=0,
        )
        assert len(chunks) >= 2
        # Every chunk must contain the header.
        for c in chunks:
            assert (
                "Feature" in c.text and "Details" in c.text
            ), f"Missing header in table chunk: {c.text[:80]!r}"
            assert "---|" in c.text, f"Missing separator in table chunk: {c.text[:80]!r}"

    def test_table_rows_not_split_mid_row(self) -> None:
        """No chunk should contain a partial row (pipe without closing pipe)."""
        header = "| Col A | Col B | Col C |\n|-------|-------|-------|"
        rows = "\n".join(f"| Alpha {i} | Beta {i} | Gamma {i} |" for i in range(20))
        table = header + "\n" + rows
        chunks = chunk_text(
            table,
            target_tokens=30,
            max_tokens=80,
            overlap_tokens=0,
        )
        for c in chunks:
            for line in c.text.strip().split("\n"):
                stripped = line.strip()
                if stripped.startswith("|"):
                    assert stripped.endswith("|"), f"Partial row: {stripped!r}"


# ---------------------------------------------------------------------------
# FAQ / Q&A pair detection
# ---------------------------------------------------------------------------


class TestFAQPairDetection:
    def test_qa_pair_stays_together(self) -> None:
        """A Q: / A: pair should never be split across chunks."""
        text = (
            "Q: What time is checkout?\n\n"
            "A: Checkout is at 11 AM.\n\n"
            "Q: Is breakfast included?\n\n"
            "A: Yes, breakfast is complimentary for all guests."
        )
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        for c in chunks:
            if "Q:" in c.text:
                assert "A:" in c.text, f"Question without answer in chunk: {c.text!r}"

    def test_multiple_faq_pairs(self) -> None:
        """Each Q+A pair is an atomic unit; pairs can be in different chunks."""
        pairs = "\n\n".join(
            f"Q: Question number {i}?\n\nA: Answer to question {i} with details." for i in range(6)
        )
        chunks = chunk_text(pairs, target_tokens=30, max_tokens=80, overlap_tokens=0)
        # Every chunk that has a Q must have the matching A.
        for c in chunks:
            q_count = c.text.count("Q:")
            a_count = c.text.count("A:")
            assert (
                q_count == a_count
            ), f"Mismatched Q/A: {q_count} Q vs {a_count} A in: {c.text[:100]!r}"

    def test_question_answer_variant(self) -> None:
        """Question: / Answer: long-form patterns also work."""
        text = (
            "Question: Where is the pool?\n\n"
            "Answer: The pool is on the rooftop with a panoramic view."
        )
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        assert len(chunks) == 1
        assert "Question:" in chunks[0].text
        assert "Answer:" in chunks[0].text

    def test_faq_hyphen_separator(self) -> None:
        """Q- / A- separator style should also be detected as FAQ pair."""
        text = "Q- What time is breakfast?\n\n" "A- Breakfast is served from 7 to 10 AM."
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        assert len(chunks) == 1
        assert "Q-" in chunks[0].text
        assert "A-" in chunks[0].text


# ---------------------------------------------------------------------------
# Fenced code-block preservation
# ---------------------------------------------------------------------------


class TestFencedBlockPreservation:
    def test_fenced_block_stays_atomic(self) -> None:
        """A triple-backtick block should not be split."""
        text = (
            "Some intro text.\n\n"
            "```json\n"
            '{"room": "101", "type": "suite"}\n'
            "```\n\n"
            "Some outro text."
        )
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        # Find the chunk with the JSON — it should contain both ``` markers.
        json_chunks = [c for c in chunks if '"room"' in c.text]
        assert json_chunks, "No chunk contains the JSON block"
        assert "```" in json_chunks[0].text

    def test_fenced_block_with_blank_lines(self) -> None:
        """Blank lines inside a fenced block should not cause paragraph splits."""
        text = (
            "```\n" "line one\n" "\n" "line two after blank\n" "\n" "line three after blank\n" "```"
        )
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        assert len(chunks) == 1
        assert "line one" in chunks[0].text
        assert "line three" in chunks[0].text

    def test_heading_inside_fence_not_split(self) -> None:
        """Lines like '# comment' inside a code block should not trigger splits."""
        text = "```python\n" "# This is a comment\n" "def hello():\n" "    pass\n" "```"
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        assert len(chunks) == 1
        assert "# This is a comment" in chunks[0].text
        assert "def hello" in chunks[0].text

    def test_tilde_fence_preserved(self) -> None:
        """~~~ fences work the same as ``` fences."""
        text = "~~~\nsome code\n~~~"
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        assert len(chunks) == 1
        assert "some code" in chunks[0].text


# ---------------------------------------------------------------------------
# Blockquote preservation
# ---------------------------------------------------------------------------


class TestBlockquotePreservation:
    def test_blockquote_stays_atomic(self) -> None:
        """A blockquote paragraph should be kept as one piece."""
        text = (
            "Introduction paragraph.\n\n"
            "> This is an important policy notice.\n"
            "> All guests must present valid ID.\n"
            "> No exceptions will be made.\n\n"
            "Following paragraph."
        )
        chunks = chunk_text(text, target_tokens=200, max_tokens=300, overlap_tokens=0)
        bq_chunks = [c for c in chunks if "policy notice" in c.text]
        assert bq_chunks
        assert "No exceptions" in bq_chunks[0].text, "Blockquote was split across chunks"

    def test_short_blockquote_not_split_by_sentences(self) -> None:
        """Blockquote should not be broken at sentence boundaries."""
        text = "> First sentence. Second sentence. Third sentence."
        chunks = chunk_text(text, target_tokens=10, max_tokens=80, overlap_tokens=0)
        # Even with small target, the blockquote should be one chunk.
        assert len(chunks) == 1


# ---------------------------------------------------------------------------
# Adaptive chunk sizing
# ---------------------------------------------------------------------------


class TestAdaptiveChunkSizing:
    def test_dense_content_produces_smaller_chunks(self) -> None:
        """Number-heavy content should produce more (smaller) chunks."""
        dense = " ".join(
            f"Room {i}: ${100 + i * 10}/night, {20 + i}m², sleeps {1 + i % 4}." for i in range(30)
        )
        normal_chunks = chunk_text(
            dense, target_tokens=100, max_tokens=200, overlap_tokens=0, adaptive=False
        )
        adaptive_chunks = chunk_text(
            dense, target_tokens=100, max_tokens=200, overlap_tokens=0, adaptive=True
        )
        # Adaptive should produce at least as many chunks (smaller target).
        assert len(adaptive_chunks) >= len(normal_chunks)

    def test_narrative_content_not_over_split(self) -> None:
        """Narrative content should not produce fewer chunks than normal."""
        narrative = (
            "The hotel is nestled in the heart of the old town, surrounded by "
            "cobblestone streets and centuries-old architecture. Guests often "
            "remark on the breathtaking views from the terrace, where the "
            "Mediterranean sea stretches endlessly to the horizon. The gardens "
            "are meticulously maintained, featuring native plants and fragrant "
            "herbs that fill the air with delightful aromas throughout the year."
        )
        normal_chunks = chunk_text(
            narrative, target_tokens=50, max_tokens=100, overlap_tokens=0, adaptive=False
        )
        adaptive_chunks = chunk_text(
            narrative, target_tokens=50, max_tokens=100, overlap_tokens=0, adaptive=True
        )
        # Narrative: adaptive target is >= original, so same or fewer chunks.
        assert len(adaptive_chunks) <= len(normal_chunks)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_exact_duplicates_removed(self) -> None:
        """Repeated identical paragraphs produce only one chunk."""
        paragraph = "The pool is open from 7 AM to 10 PM daily."
        text = "\n\n".join([paragraph] * 5)
        chunks = chunk_text(
            text, target_tokens=200, max_tokens=300, overlap_tokens=0, deduplicate=True
        )
        assert len(chunks) == 1
        assert "pool" in chunks[0].text

    def test_non_duplicates_preserved(self) -> None:
        """Distinct paragraphs are all kept."""
        text = (
            "The pool is open daily.\n\n" "The gym is on floor two.\n\n" "The spa offers massages."
        )
        chunks = chunk_text(
            text, target_tokens=200, max_tokens=300, overlap_tokens=0, deduplicate=True
        )
        all_text = " ".join(c.text for c in chunks)
        assert "pool" in all_text
        assert "gym" in all_text
        assert "spa" in all_text

    def test_whitespace_normalized_dedup(self) -> None:
        """Chunks that differ only in whitespace are treated as duplicates."""
        text = "Hello   world.\n\nHello world."
        chunks = chunk_text(
            text, target_tokens=200, max_tokens=300, overlap_tokens=0, deduplicate=True
        )
        assert len(chunks) == 1
