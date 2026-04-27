"""Tests for the Markdown / plain-text loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from voxtera.rag.loaders import LoadedDocument
from voxtera.rag.loaders.text import load_text

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MARKDOWN_CONTENT = """\
# Grand Hotel Barcelona

Welcome to our hotel.

## Amenities

- Pool
- Spa
- Restaurant

Breakfast is served **daily** from 7 AM to 10 AM.
"""

PLAIN_CONTENT = """\
Grand Hotel Barcelona

Welcome to our hotel.

Breakfast is served daily from 7 AM to 10 AM.
"""

# Non-ASCII: accents, Japanese, emoji
UNICODE_CONTENT = """\
Bienvenue à l'hôtel — café, résumé, naïve.

ホテルへようこそ。朝食は毎日提供されます。

Precio: €120 por noche 🌊
"""


@pytest.fixture()
def md_file(tmp_path: Path) -> Path:
    p = tmp_path / "hotel.md"
    p.write_text(MARKDOWN_CONTENT, encoding="utf-8")
    return p


@pytest.fixture()
def txt_file(tmp_path: Path) -> Path:
    p = tmp_path / "hotel.txt"
    p.write_text(PLAIN_CONTENT, encoding="utf-8")
    return p


@pytest.fixture()
def unicode_file(tmp_path: Path) -> Path:
    p = tmp_path / "intl.md"
    p.write_text(UNICODE_CONTENT, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLoadText:
    def test_returns_loaded_document(self, md_file: Path) -> None:
        assert isinstance(load_text(md_file), LoadedDocument)

    def test_md_text_intact(self, md_file: Path) -> None:
        result = load_text(md_file)
        assert result.text == MARKDOWN_CONTENT

    def test_txt_text_intact(self, txt_file: Path) -> None:
        result = load_text(txt_file)
        assert result.text == PLAIN_CONTENT

    def test_doc_id_is_stem(self, md_file: Path) -> None:
        assert load_text(md_file).doc_id == "hotel"

    def test_source_path_in_metadata(self, md_file: Path) -> None:
        assert load_text(md_file).metadata["source_path"] == str(md_file)

    def test_format_md(self, md_file: Path) -> None:
        assert load_text(md_file).metadata["format"] == "md"

    def test_format_txt(self, txt_file: Path) -> None:
        assert load_text(txt_file).metadata["format"] == "txt"

    def test_markdown_not_escaped(self, md_file: Path) -> None:
        result = load_text(md_file)
        assert "# Grand Hotel Barcelona" in result.text
        assert "**daily**" in result.text

    def test_unicode_roundtrip(self, unicode_file: Path) -> None:
        result = load_text(unicode_file)
        assert "Bienvenue à l'hôtel" in result.text
        assert "ホテルへようこそ" in result.text
        assert "€120" in result.text
        assert "🌊" in result.text


# ---------------------------------------------------------------------------
# Errors and edge cases
# ---------------------------------------------------------------------------


class TestLoadTextErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Text file not found"):
            load_text(tmp_path / "nope.md")


class TestLoadTextEdgeCases:
    def test_latin1_fallback(self, tmp_path: Path) -> None:
        """Western European content in Latin-1."""
        # Longer text gives charset_normalizer enough statistical signal.
        content = (
            "Bienvenue dans notre hôtel à Barcelone. "
            "Le petit-déjeuner est servi de sept heures à dix heures. "
            "Réservez une chambre supérieure avec vue méditerranéenne. "
            "Le café et le thé sont inclus dans le tarif journalier."
        )
        p = tmp_path / "latin.txt"
        p.write_bytes(content.encode("latin-1"))
        result = load_text(p)
        assert "hôtel" in result.text
        assert "déjeuner" in result.text

    def test_russian_windows1251(self, tmp_path: Path) -> None:
        """Russian hotel content in Windows-1251."""
        content = (
            "Добро пожаловать в наш отель в центре Барселоны. "
            "Завтрак подаётся ежедневно с семи до десяти часов утра. "
            "Бассейн и спа-центр открыты для всех гостей отеля. "
            "Ресторан предлагает блюда средиземноморской кухни."
        )
        p = tmp_path / "russian.txt"
        p.write_bytes(content.encode("windows-1251"))
        result = load_text(p)
        assert "Добро пожаловать" in result.text
        assert "Завтрак" in result.text

    def test_turkish_windows1254(self, tmp_path: Path) -> None:
        """Turkish hotel content in Windows-1254."""
        content = (
            "Otelimize hoş geldiniz. Kahvaltı her gün yedi ile on arasında "
            "sunulmaktadır. Havuz ve spa tüm misafirlerimize açıktır. "
            "Restoranımız Akdeniz mutfağından lezzetli yemekler sunmaktadır. "
            "Odalarımız şehir manzaralı ve deniz manzaralı seçenekleriyle."
        )
        p = tmp_path / "turkish.txt"
        p.write_bytes(content.encode("windows-1254"))
        result = load_text(p)
        assert "hoş geldiniz" in result.text
        assert "Kahvaltı" in result.text
