"""PDF loader with image-aware OCR fallback.

Strategy per page:

1. Always attempt **direct text extraction** via PyMuPDF (fast, accurate
   for born-digital PDFs).
2. Decide whether OCR is needed using cheap structural signals:
   * The extracted text is suspiciously short, or
   * The text looks garbled (very low alphabetic ratio), or
   * The page is mostly image content with little text coverage.
3. If OCR is warranted **and** Tesseract is installed, render the page to
   an image and run OCR with a multilingual language hint, then merge
   the OCR result with any direct text already extracted.

This avoids running OCR on every page (saving minutes on large born-digital
documents) while still capturing scanned pages, decorative-font menus, and
mixed pages where charts or images carry text the parser cannot reach.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import fitz  # pymupdf
from loguru import logger

from voxtera.rag.loaders import LoadedDocument

# ---------------------------------------------------------------------------
# Optional OCR imports — graceful degradation if Tesseract isn't installed.
# ---------------------------------------------------------------------------

_TESSERACT_AVAILABLE = False

try:
    import pytesseract
    from PIL import Image

    if shutil.which("tesseract") or shutil.which("tesseract.exe"):
        _TESSERACT_AVAILABLE = True
    else:
        logger.warning(
            "pytesseract is installed but the Tesseract binary was not found "
            "on PATH. PDF OCR will be disabled."
        )
except ImportError:
    logger.warning(
        "pytesseract / Pillow not installed. PDF OCR will be disabled."
    )

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Below this many non-whitespace chars a page is considered "almost empty" —
# OCR is warranted because the parser likely failed.
_MIN_TEXT_CHARS = 40

# Below this fraction of alphabetic characters, the extraction looks garbled
# (typical for broken font encodings producing boxes / null bytes / glyphs
# without a Unicode mapping).  Calibrated for Latin scripts.
_MIN_ALPHA_RATIO = 0.4

# When images cover at least this fraction of page area AND extracted text is
# sparse, OCR is warranted to recover text embedded in the image.
_IMAGE_COVERAGE_TRIGGER = 0.3

# When the page is image-heavy, "sparse text" means fewer than this many
# characters were extracted — likely a chart caption or label is missing.
_IMAGE_HEAVY_TEXT_LIMIT = 200

# Tesseract language pack hint — covers the languages Voxtera targets.
# Falls back to English-only if a pack is missing (handled at call site).
_OCR_LANG_FULL = "eng+spa+fra+deu+ita+por"
_OCR_LANG_FALLBACK = "eng"

# DPI used when rasterising a page for OCR.  300 is the standard sweet spot
# for Tesseract accuracy without exploding memory use.
_OCR_DPI = 300

# Safety cap: refuse to OCR more than this many pages from a single PDF so a
# misconfigured huge document cannot stall an ingest job for hours.  Callers
# can override via the ``ocr_page_budget`` argument to :func:`load_pdf`.
_DEFAULT_OCR_PAGE_BUDGET = 200

# When the OCR result adds *new* word tokens the parser missed, we keep it.
# This threshold is the minimum fraction of OCR tokens that must be unique
# (not present in the parsed text) for the OCR output to be appended.  Tuned
# to ignore mis-recognized duplicates of the same content.
_OCR_NEW_TOKEN_RATIO = 0.3


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------


def _alpha_ratio(text: str) -> float:
    """Fraction of characters in *text* that are alphabetic letters."""
    if not text:
        return 0.0
    return sum(1 for c in text if c.isalpha()) / len(text)


def _looks_garbled(text: str) -> bool:
    """Cheap check: did the extractor return mostly non-letter glyphs?"""
    stripped = text.strip()
    if len(stripped) < _MIN_TEXT_CHARS:
        # Too short to judge — let other signals decide.
        return False
    return _alpha_ratio(stripped) < _MIN_ALPHA_RATIO


def _image_coverage(page: fitz.Page) -> float:
    """Estimate the fraction of *page* area covered by raster images.

    Returns 0.0 for pages with no images.  Capped at 1.0 (overlapping images
    can otherwise sum past the page area).
    """
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0

    covered = 0.0
    for img in page.get_images(full=True):
        xref = img[0]
        for rect in page.get_image_rects(xref):
            covered += rect.width * rect.height
    return float(min(covered / page_area, 1.0))


def _needs_ocr(text: str, image_coverage: float) -> bool:
    """Decide whether OCR is worth running on a page.

    Takes the pre-computed *image_coverage* so callers don't pay the
    page-traversal cost twice.

    Triggers (any one is enough):
    * Almost no text was extracted.
    * Extracted text looks garbled.
    * Page is image-heavy and what little text exists is sparse.
    """
    if len(text) < _MIN_TEXT_CHARS:
        return True
    if _looks_garbled(text):
        return True
    return (
        image_coverage >= _IMAGE_COVERAGE_TRIGGER
        and len(text) < _IMAGE_HEAVY_TEXT_LIMIT
    )


# ---------------------------------------------------------------------------
# Extraction primitives
# ---------------------------------------------------------------------------


def _extract_text(page: fitz.Page) -> str:
    """Direct text extraction via PyMuPDF."""
    return (page.get_text("text") or "").strip()


def _render_page(page: fitz.Page) -> Image.Image:
    """Rasterise *page* to a PIL image suitable for Tesseract."""
    # Force RGB (alpha=False) so the PIL conversion is unambiguous regardless
    # of whether the source page uses CMYK, grayscale, or has transparency.
    pix = page.get_pixmap(dpi=_OCR_DPI, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _run_ocr(image: Image.Image) -> str:
    """Run Tesseract OCR with multilingual hint, falling back to English.

    If a language pack is missing, Tesseract raises ``TesseractError``;
    we retry with English so the call always returns *something*.
    """
    try:
        result: str = pytesseract.image_to_string(image, lang=_OCR_LANG_FULL)
    except pytesseract.TesseractError as exc:
        logger.debug(
            "Multilingual OCR failed ({}); retrying with English only.", exc
        )
        result = pytesseract.image_to_string(image, lang=_OCR_LANG_FALLBACK)
    return result.strip()


# ---------------------------------------------------------------------------
# Per-page orchestration
# ---------------------------------------------------------------------------


def _word_tokens(text: str) -> set[str]:
    """Lowercased alphabetic words of length >= 3 — used for content overlap.

    Short tokens (a, an, of) are dropped because they appear in nearly any
    text and dilute the overlap signal.
    """
    return {
        word
        for word in ("".join(c if c.isalpha() else " " for c in text.lower())).split()
        if len(word) >= 3
    }


def _merge_text_and_ocr(text: str, ocr: str) -> str:
    """Combine the parser's text with the OCR result.

    * If one side is empty, return the other.
    * If OCR's *unique* word set (tokens not already present in the parsed
      text) is too small a fraction of its own tokens, the OCR is mostly
      a re-recognition of the same content — keep the parser output, which
      is more accurate for born-digital glyphs.
    * Otherwise concatenate so we capture content that lives only in
      images (e.g. chart captions, embedded scans).
    """
    if not ocr:
        return text
    if not text:
        return ocr

    text_tokens = _word_tokens(text)
    ocr_tokens = _word_tokens(ocr)
    if not ocr_tokens:
        return text

    new_tokens = ocr_tokens - text_tokens
    if len(new_tokens) / len(ocr_tokens) < _OCR_NEW_TOKEN_RATIO:
        return text
    return text + "\n\n" + ocr


def _extract_page(page: fitz.Page, *, ocr_allowed: bool) -> tuple[str, bool]:
    """Extract text from a single page, OCR-augmenting if warranted.

    *ocr_allowed* lets the caller veto OCR (e.g. once the per-document OCR
    budget is spent).  Returns ``(text, used_ocr)`` so the caller can
    record metrics.
    """
    text = _extract_text(page)

    if not _TESSERACT_AVAILABLE or not ocr_allowed:
        return text, False

    coverage = _image_coverage(page)
    if not _needs_ocr(text, coverage):
        return text, False

    logger.debug(
        "Page {} flagged for OCR (text_chars={}, image_coverage={:.0%}).",
        page.number + 1,
        len(text),
        coverage,
    )

    try:
        ocr = _run_ocr(_render_page(page))
    except Exception as exc:  # noqa: BLE001 — OCR failures must not abort load.
        logger.warning("OCR failed on page {}: {}", page.number + 1, exc)
        return text, False

    return _merge_text_and_ocr(text, ocr), True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_pdf(
    path: Path,
    *,
    ocr_page_budget: int = _DEFAULT_OCR_PAGE_BUDGET,
) -> LoadedDocument:
    """Load a PDF file and return a :class:`LoadedDocument`.

    Pages are joined with two newlines so the chunker treats page breaks as
    paragraph boundaries.  ``doc_id`` defaults to the file stem.

    *ocr_page_budget* caps the number of pages that may invoke OCR for this
    document.  Once spent, remaining pages still extract direct text but
    skip OCR.  A warning is logged when the budget is exhausted.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    pages: list[str] = []
    ocr_pages = 0
    budget_exhausted_warned = False

    doc = fitz.open(str(path))
    try:
        page_count = doc.page_count
        for page in doc:
            ocr_allowed = ocr_pages < ocr_page_budget
            if not ocr_allowed and not budget_exhausted_warned:
                logger.warning(
                    "OCR page budget ({}) exhausted for {}; remaining pages "
                    "will use direct text only.",
                    ocr_page_budget,
                    path.name,
                )
                budget_exhausted_warned = True

            extracted, used_ocr = _extract_page(page, ocr_allowed=ocr_allowed)
            if used_ocr:
                ocr_pages += 1
            if extracted:
                pages.append(extracted)
    finally:
        doc.close()

    return LoadedDocument(
        doc_id=path.stem,
        text="\n\n".join(pages),
        metadata={
            "source_path": str(path),
            "page_count": str(page_count),
            "ocr_available": str(_TESSERACT_AVAILABLE),
            "ocr_pages": str(ocr_pages),
        },
    )
