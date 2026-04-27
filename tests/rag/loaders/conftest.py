"""Shared fixtures for loader tests."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest


@pytest.fixture()
def sample_pdf(tmp_path: Path) -> Path:
    """Create a minimal 2-page PDF with known text content."""
    doc = fitz.open()

    # Page 1
    page1 = doc.new_page(width=595, height=842)
    page1.insert_text(
        (72, 100),
        "Welcome to the Grand Hotel Barcelona",
        fontsize=16,
    )
    page1.insert_text(
        (72, 140),
        "Our hotel offers luxurious rooms with Mediterranean views.",
        fontsize=11,
    )

    # Page 2
    page2 = doc.new_page(width=595, height=842)
    page2.insert_text(
        (72, 100),
        "Restaurant Menu",
        fontsize=16,
    )
    page2.insert_text(
        (72, 140),
        "Breakfast is served from 7 AM to 10 AM daily.",
        fontsize=11,
    )

    path = tmp_path / "sample.pdf"
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def empty_pdf(tmp_path: Path) -> Path:
    """Create a PDF with one blank page."""
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    path = tmp_path / "empty.pdf"
    doc.save(str(path))
    doc.close()
    return path
