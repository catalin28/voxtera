"""Tests for the PDF loader."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from voxtera.rag.loaders import LoadedDocument
from voxtera.rag.loaders.pdf import (
    _alpha_ratio,
    _image_coverage,
    _looks_garbled,
    _merge_text_and_ocr,
    _needs_ocr,
    _word_tokens,
    load_pdf,
)

# ---------------------------------------------------------------------------
# load_pdf — happy path
# ---------------------------------------------------------------------------


class TestLoadPdf:
    def test_contains_known_text(self, sample_pdf: Path) -> None:
        result = load_pdf(sample_pdf)
        assert "Grand Hotel Barcelona" in result.text

    def test_returns_loaded_document(self, sample_pdf: Path) -> None:
        assert isinstance(load_pdf(sample_pdf), LoadedDocument)

    def test_doc_id_is_stem(self, sample_pdf: Path) -> None:
        assert load_pdf(sample_pdf).doc_id == "sample"

    def test_page_count_in_metadata(self, sample_pdf: Path) -> None:
        assert load_pdf(sample_pdf).metadata["page_count"] == "2"

    def test_pages_joined_with_double_newline(self, sample_pdf: Path) -> None:
        assert "\n\n" in load_pdf(sample_pdf).text

    def test_both_pages_present(self, sample_pdf: Path) -> None:
        result = load_pdf(sample_pdf)
        assert "Grand Hotel Barcelona" in result.text
        assert "Restaurant Menu" in result.text

    def test_source_path_in_metadata(self, sample_pdf: Path) -> None:
        assert load_pdf(sample_pdf).metadata["source_path"] == str(sample_pdf)

    def test_ocr_flags_in_metadata(self, sample_pdf: Path) -> None:
        result = load_pdf(sample_pdf)
        assert result.metadata["ocr_available"] in ("True", "False")
        # All pages of sample_pdf are clean born-digital → no OCR needed.
        assert result.metadata["ocr_pages"] == "0"


# ---------------------------------------------------------------------------
# load_pdf — errors and edge cases
# ---------------------------------------------------------------------------


class TestLoadPdfErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="PDF not found"):
            load_pdf(tmp_path / "does_not_exist.pdf")

    def test_empty_pdf_returns_empty_text(self, empty_pdf: Path) -> None:
        result = load_pdf(empty_pdf)
        assert result.text == ""
        assert result.metadata["page_count"] == "1"


# ---------------------------------------------------------------------------
# _alpha_ratio
# ---------------------------------------------------------------------------


class TestAlphaRatio:
    def test_pure_letters_is_one(self) -> None:
        assert _alpha_ratio("Hello") == 1.0

    def test_empty_is_zero(self) -> None:
        assert _alpha_ratio("") == 0.0

    def test_pure_garbage_is_zero(self) -> None:
        assert _alpha_ratio("□□□ \x00 ■●▲") == 0.0

    def test_mixed_is_between(self) -> None:
        ratio = _alpha_ratio("Hello 123!")
        assert 0.0 < ratio < 1.0


# ---------------------------------------------------------------------------
# _looks_garbled
# ---------------------------------------------------------------------------


class TestLooksGarbled:
    def test_clean_prose_is_not_garbled(self) -> None:
        text = (
            "Welcome to the Grand Hotel Barcelona. Our hotel offers "
            "luxurious rooms with Mediterranean views all year round."
        )
        assert not _looks_garbled(text)

    def test_glyph_garbage_is_garbled(self) -> None:
        text = "□ □ □ ■ ● ▲ △ □ □ □ ■ ● ▲ △ □ □ □ ■ ● ▲ △ □ □ □"
        assert _looks_garbled(text)

    def test_short_text_is_not_judged(self) -> None:
        # Below the minimum char threshold we don't claim it's garbled —
        # other signals (text length) decide.
        assert not _looks_garbled("□ □ □")


# ---------------------------------------------------------------------------
# _needs_ocr — decision logic
# ---------------------------------------------------------------------------


def _make_text_page(text: str) -> fitz.Page:
    """Build a one-page in-memory PDF and return its page handle."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    if text:
        page.insert_text((72, 100), text, fontsize=11)
    return page


class TestNeedsOcr:
    def test_blank_page_needs_ocr(self) -> None:
        assert _needs_ocr("", 0.0) is True

    def test_short_text_needs_ocr(self) -> None:
        assert _needs_ocr("Hi", 0.0) is True

    def test_long_clean_text_does_not_need_ocr(self) -> None:
        text = (
            "Welcome to the Grand Hotel Barcelona. Our hotel offers "
            "luxurious rooms with Mediterranean views all year round. "
            "Breakfast is served from seven in the morning until ten."
        )
        assert _needs_ocr(text, 0.0) is False

    def test_garbled_text_needs_ocr(self) -> None:
        garbage = "□ □ □ ■ ● ▲ △ □ □ □ ■ ● ▲ △ □ □ □ ■ ● ▲ △ □ □ □"
        assert _needs_ocr(garbage, 0.0) is True

    def test_image_heavy_with_sparse_text_needs_ocr(self) -> None:
        # Long enough not to trip the short-text rule, but well under the
        # image-heavy text limit — simulating a chart caption.
        sparse = "Annual revenue chart. See figure for details."
        assert _needs_ocr(sparse, 0.6) is True

    def test_image_heavy_with_dense_text_skips_ocr(self) -> None:
        # Image-heavy branch only fires when text is sparse; a long block of
        # prose should skip OCR even on a page covered with images.
        dense = (
            "This page contains a long descriptive paragraph with plenty of "
            "prose that is well over the image-heavy text limit threshold so "
            "the loader should not bother running OCR on it even though the "
            "page also embeds a large decorative banner image at the top of "
            "the layout for branding purposes only."
        )
        assert _needs_ocr(dense, 0.9) is False


# ---------------------------------------------------------------------------
# _image_coverage
# ---------------------------------------------------------------------------


class TestImageCoverage:
    def test_text_only_page_has_zero_coverage(self) -> None:
        page = _make_text_page("Hello world this is a normal page of text.")
        assert _image_coverage(page) == 0.0


# ---------------------------------------------------------------------------
# _merge_text_and_ocr
# ---------------------------------------------------------------------------


class TestMergeTextAndOcr:
    def test_empty_ocr_returns_text(self) -> None:
        assert _merge_text_and_ocr("real text", "") == "real text"

    def test_empty_text_returns_ocr(self) -> None:
        assert _merge_text_and_ocr("", "ocr text") == "ocr text"

    def test_redundant_ocr_is_dropped(self) -> None:
        # OCR rediscovered roughly the same words — trust the parser.
        text = "Welcome to the Grand Hotel Barcelona Mediterranean views"
        ocr = "Welcome Grand Hotel Barcelona views"
        assert _merge_text_and_ocr(text, ocr) == text

    def test_duplicate_garbled_ocr_is_dropped(self) -> None:
        # OCR re-recognized the same content, mis-spelled and duplicated —
        # raw alpha-count would have appended this and corrupted the chunk.
        text = "Conference Room A available for booking"
        ocr = "Confcrcncc Room A Conference Room A available booking room A"
        assert _merge_text_and_ocr(text, ocr) == text

    def test_substantially_more_ocr_is_appended(self) -> None:
        text = "Short header"
        ocr = (
            "A much longer caption embedded inside the image that the parser "
            "could not reach because the text lives in a raster figure."
        )
        merged = _merge_text_and_ocr(text, ocr)
        assert text in merged
        assert ocr in merged
        assert "\n\n" in merged

    def test_ocr_with_only_short_words_is_dropped(self) -> None:
        # No tokens of length >= 3 — nothing useful to add.
        assert _merge_text_and_ocr("real content here", "a an of to it") == "real content here"


class TestWordTokens:
    def test_lowercases_and_drops_punctuation(self) -> None:
        assert _word_tokens("Hello, World!") == {"hello", "world"}

    def test_drops_short_tokens(self) -> None:
        assert _word_tokens("a an of cat dog") == {"cat", "dog"}

    def test_empty_returns_empty_set(self) -> None:
        assert _word_tokens("") == set()


class TestOcrBudget:
    def test_default_budget_does_not_break_normal_load(self, sample_pdf: Path) -> None:
        # sample_pdf has 2 clean pages; default budget is large.
        result = load_pdf(sample_pdf)
        assert result.metadata["ocr_pages"] == "0"

    def test_zero_budget_skips_ocr_entirely(self, empty_pdf: Path) -> None:
        # An empty page would normally trigger OCR; budget=0 vetoes it.
        result = load_pdf(empty_pdf, ocr_page_budget=0)
        assert result.metadata["ocr_pages"] == "0"
