"""Tests for the loader registry (load_document dispatch)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from voxtera.rag.loaders import LoadedDocument, load_document

_FAKE_DOC = LoadedDocument(doc_id="fake", text="hello", metadata={})


class TestLoadDocumentDispatch:
    """load_document routes to the correct loader by extension."""

    @patch("voxtera.rag.loaders.pdf.load_pdf", return_value=_FAKE_DOC)
    def test_pdf(self, mock_load: MagicMock) -> None:
        result = load_document(Path("hotel.pdf"))
        assert result is _FAKE_DOC
        mock_load.assert_called_once_with(Path("hotel.pdf"))

    @patch("voxtera.rag.loaders.pdf.load_pdf", return_value=_FAKE_DOC)
    def test_pdf_uppercase(self, mock_load: MagicMock) -> None:
        result = load_document(Path("hotel.PDF"))
        assert result is _FAKE_DOC
        mock_load.assert_called_once_with(Path("hotel.PDF"))

    @patch("voxtera.rag.loaders.text.load_text", return_value=_FAKE_DOC)
    def test_md(self, mock_load: MagicMock) -> None:
        result = load_document(Path("readme.md"))
        assert result is _FAKE_DOC
        mock_load.assert_called_once_with(Path("readme.md"))

    @patch("voxtera.rag.loaders.text.load_text", return_value=_FAKE_DOC)
    def test_markdown(self, mock_load: MagicMock) -> None:
        result = load_document(Path("readme.markdown"))
        assert result is _FAKE_DOC
        mock_load.assert_called_once_with(Path("readme.markdown"))

    @patch("voxtera.rag.loaders.text.load_text", return_value=_FAKE_DOC)
    def test_txt(self, mock_load: MagicMock) -> None:
        result = load_document(Path("notes.txt"))
        assert result is _FAKE_DOC
        mock_load.assert_called_once_with(Path("notes.txt"))

    @patch("voxtera.rag.loaders.text.load_text", return_value=_FAKE_DOC)
    def test_txt_mixed_case(self, mock_load: MagicMock) -> None:
        result = load_document(Path("notes.TxT"))
        assert result is _FAKE_DOC
        mock_load.assert_called_once_with(Path("notes.TxT"))


class TestLoadDocumentErrors:
    """Unsupported extensions raise ValueError."""

    def test_unsupported_docx(self) -> None:
        with pytest.raises(ValueError, match=r"Unsupported file extension.*\.docx"):
            load_document(Path("file.docx"))

    def test_unsupported_xlsx(self) -> None:
        with pytest.raises(ValueError, match=r"Unsupported file extension.*\.xlsx"):
            load_document(Path("file.xlsx"))

    def test_unsupported_csv(self) -> None:
        with pytest.raises(ValueError, match=r"Unsupported file extension.*\.csv"):
            load_document(Path("file.csv"))

    def test_no_extension(self) -> None:
        with pytest.raises(ValueError, match=r"Unsupported file extension"):
            load_document(Path("Makefile"))
