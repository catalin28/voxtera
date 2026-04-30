"""Tests for voxtera.cli — argument parsing and subcommand dispatch."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from voxtera.cli import _build_parser, main

# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


class TestParser:
    """Verify argparse wiring without running any real logic."""

    def test_ingest_all_flags(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["ingest", "--hotel", "h1", "--category", "spa", "--language", "fr", "data/"]
        )
        assert args.command == "ingest"
        assert args.hotel == "h1"
        assert args.category == "spa"
        assert args.language == "fr"
        assert args.path == "data/"

    def test_ingest_defaults(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["ingest", "--hotel", "h1", "file.md"])
        assert args.language == "en"
        assert args.category is None

    def test_list_chunks(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["list-chunks", "--hotel", "h1"])
        assert args.command == "list-chunks"
        assert args.hotel == "h1"
        assert args.category is None

    def test_list_chunks_with_category(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["list-chunks", "--hotel", "h1", "--category", "spa"])
        assert args.category == "spa"

    def test_search_all_flags(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            ["search", "--hotel", "h1", "--language", "de", "--top-k", "5", "pool hours"]
        )
        assert args.command == "search"
        assert args.hotel == "h1"
        assert args.language == "de"
        assert args.top_k == 5
        assert args.query == "pool hours"

    def test_search_defaults(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["search", "--hotel", "h1", "breakfast"])
        assert args.top_k == 5
        assert args.language is None

    def test_delete_with_yes(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["delete", "--hotel", "h1", "--doc-id", "abc123", "-y"])
        assert args.command == "delete"
        assert args.hotel == "h1"
        assert args.doc_id == "abc123"
        assert args.yes is True

    def test_delete_without_yes(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["delete", "--hotel", "h1", "--doc-id", "abc123"])
        assert args.yes is False

    def test_run(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["run"])
        assert args.command == "run"

    def test_no_command_prints_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No subcommand → help text + exit 0."""
        with patch("voxtera.cli.load_dotenv"), pytest.raises(SystemExit) as exc:
            sys.argv = ["voxtera"]
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "ingest" in out


# ---------------------------------------------------------------------------
# Subcommand integration tests (all external I/O mocked)
# ---------------------------------------------------------------------------


class TestIngest:
    """Tests for the ingest subcommand."""

    def test_ingest_single_file(self, tmp_path: Path) -> None:
        md_file = tmp_path / "info.md"
        md_file.write_text("# Hotel\nBreakfast at 7am.", encoding="utf-8")

        fake_doc = MagicMock(doc_id="info.md", text="# Hotel\nBreakfast at 7am.")
        fake_chunks = [MagicMock(text="Breakfast at 7am.")]
        fake_vectors = [[0.1] * 384]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", return_value=fake_doc) as mock_load,
            patch("voxtera.cli.chunk_text", return_value=fake_chunks) as mock_chunk,
            patch("voxtera.cli.embed", return_value=fake_vectors) as mock_embed,
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(md_file)]
            main()

            mock_load.assert_called_once_with(md_file)
            mock_chunk.assert_called_once_with("# Hotel\nBreakfast at 7am.")
            mock_embed.assert_called_once()
            # Re-ingest safety: stale chunks for this doc are wiped before upsert.
            mock_store.delete_doc.assert_called_once_with(hotel_id="h1", doc_id="info.md")
            mock_store.upsert_chunk.assert_called_once()
            call_kwargs = mock_store.upsert_chunk.call_args.kwargs
            assert call_kwargs["hotel_id"] == "h1"
            assert call_kwargs["doc_id"] == "info.md"
            assert call_kwargs["chunk_index"] == 0
            assert call_kwargs["language"] == "en"
            # No --category passed → derived from filename stem.
            assert call_kwargs["category"] == "info"

    def test_ingest_folder(self, tmp_path: Path) -> None:
        (tmp_path / "a.md").write_text("AAA", encoding="utf-8")
        (tmp_path / "b.txt").write_text("BBB", encoding="utf-8")

        fake_doc = MagicMock(doc_id="x", text="content")
        fake_chunks = [MagicMock(text="chunk")]
        fake_vectors = [[0.1] * 384]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", return_value=fake_doc),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", return_value=fake_vectors),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(tmp_path)]
            main()

            assert mock_store.upsert_chunk.call_count == 2

    def test_ingest_missing_path(self) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            pytest.raises(SystemExit) as exc,
        ):
            sys.argv = ["voxtera", "ingest", "--hotel", "h1", "/no/such/file.md"]
            main()

        assert exc.value.code == 1
        mock_store_fn.assert_not_called()

    def test_ingest_skips_unsupported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "data.xlsx").write_bytes(b"fake")
        (tmp_path / "readme.md").write_text("OK", encoding="utf-8")

        fake_doc = MagicMock(doc_id="readme.md", text="OK")
        fake_chunks = [MagicMock(text="OK")]
        fake_vectors = [[0.1] * 384]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch(
                "voxtera.cli.load_document",
                side_effect=lambda p: (
                    (_ for _ in ()).throw(ValueError("Unsupported"))
                    if p.suffix == ".xlsx"
                    else fake_doc
                ),
            ),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", return_value=fake_vectors),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(tmp_path)]
            main()

            # Only readme.md was ingested, xlsx was skipped
            mock_store.upsert_chunk.assert_called_once()

    def test_ingest_skips_os_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If load_document raises OSError, the file is skipped gracefully."""
        md_file = tmp_path / "locked.md"
        md_file.write_text("content", encoding="utf-8")

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch(
                "voxtera.cli.load_document",
                side_effect=OSError("Permission denied"),
            ),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(md_file)]
            main()

            mock_store.upsert_chunk.assert_not_called()

    def test_ingest_embed_error_skips_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If embed() raises, the file is skipped gracefully."""
        md_file = tmp_path / "info.md"
        md_file.write_text("# Hello", encoding="utf-8")

        fake_doc = MagicMock(doc_id="info.md", text="# Hello")
        fake_chunks = [MagicMock(text="Hello")]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", return_value=fake_doc),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", side_effect=RuntimeError("API down")),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(md_file)]
            main()

            mock_store.upsert_chunk.assert_not_called()

        out = capsys.readouterr().out
        assert "ingested" not in out

    def test_ingest_empty_folder(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store"),
        ):
            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(empty_dir)]
            main()

        out = capsys.readouterr().out
        assert "No files found" in out

    def test_ingest_explicit_category_overrides_filename(self, tmp_path: Path) -> None:
        """Operator-supplied --category wins over the filename-stem default."""
        md_file = tmp_path / "menu.md"
        md_file.write_text("Breakfast 7am.", encoding="utf-8")

        fake_doc = MagicMock(doc_id="menu.md", text="Breakfast 7am.")
        fake_chunks = [MagicMock(text="Breakfast 7am.")]
        fake_vectors = [[0.1] * 384]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", return_value=fake_doc),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", return_value=fake_vectors),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = [
                "voxtera",
                "ingest",
                "--hotel",
                "h1",
                "--category",
                "food",
                str(md_file),
            ]
            main()

            assert mock_store.upsert_chunk.call_args.kwargs["category"] == "food"

    def test_ingest_folder_derives_per_file_category(self, tmp_path: Path) -> None:
        """A folder ingest with no --category should tag each file by its stem
        (menu.md -> 'menu', spa.md -> 'spa'), enabling the documented
        `voxtera ingest --hotel demo demo-hotel/` workflow."""
        (tmp_path / "menu.md").write_text("Breakfast 7am.", encoding="utf-8")
        (tmp_path / "spa.md").write_text("Spa 9-5.", encoding="utf-8")

        # load_document returns a doc whose doc_id matches the file name.
        def _load(p: Path) -> MagicMock:
            return MagicMock(doc_id=p.name, text=p.read_text(encoding="utf-8"))

        fake_chunks = [MagicMock(text="x")]
        fake_vectors = [[0.1] * 384]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", side_effect=_load),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", return_value=fake_vectors),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "demo", str(tmp_path)]
            main()

        categories_seen = {
            call.kwargs["category"] for call in mock_store.upsert_chunk.call_args_list
        }
        assert categories_seen == {"menu", "spa"}

    def test_ingest_clears_stale_chunks_before_upsert(self, tmp_path: Path) -> None:
        """Re-ingest of a doc must wipe its previous chunks first; otherwise
        an old version with N chunks would leave indexes >= new_len behind
        and the retriever would keep returning stale text."""
        md_file = tmp_path / "menu.md"
        md_file.write_text("New shorter version.", encoding="utf-8")

        fake_doc = MagicMock(doc_id="menu.md", text="New shorter version.")
        fake_chunks = [MagicMock(text="chunk-1"), MagicMock(text="chunk-2")]
        fake_vectors = [[0.1] * 384, [0.2] * 384]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", return_value=fake_doc),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", return_value=fake_vectors),
        ):
            mock_store = MagicMock()
            # Track call order across delete_doc and upsert_chunk so we can
            # assert the delete happened FIRST.
            order: list[str] = []
            mock_store.delete_doc.side_effect = lambda **_: order.append("delete")
            mock_store.upsert_chunk.side_effect = lambda **_: order.append("upsert")
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(md_file)]
            main()

        mock_store.delete_doc.assert_called_once_with(hotel_id="h1", doc_id="menu.md")
        assert mock_store.upsert_chunk.call_count == 2
        # Delete must precede the first upsert.
        assert order == ["delete", "upsert", "upsert"]

    def test_ingest_does_not_delete_when_load_fails(self, tmp_path: Path) -> None:
        """If the loader fails, we must NOT call delete_doc — otherwise a bad
        re-ingest would wipe the previous good version of the doc."""
        md_file = tmp_path / "menu.md"
        md_file.write_text("anything", encoding="utf-8")

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", side_effect=ValueError("Unsupported")),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(md_file)]
            main()

        mock_store.delete_doc.assert_not_called()
        mock_store.upsert_chunk.assert_not_called()

    def test_ingest_does_not_delete_when_embed_fails(self, tmp_path: Path) -> None:
        """If embedding fails, we must NOT call delete_doc — same reason as
        the loader-failure case: we'd silently wipe good prior data."""
        md_file = tmp_path / "menu.md"
        md_file.write_text("# Hi", encoding="utf-8")

        fake_doc = MagicMock(doc_id="menu.md", text="# Hi")
        fake_chunks = [MagicMock(text="Hi")]

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.cli.load_document", return_value=fake_doc),
            patch("voxtera.cli.chunk_text", return_value=fake_chunks),
            patch("voxtera.cli.embed", side_effect=RuntimeError("API down")),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "ingest", "--hotel", "h1", str(md_file)]
            main()

        mock_store.delete_doc.assert_not_called()
        mock_store.upsert_chunk.assert_not_called()


class TestListChunks:
    """Tests for the list-chunks subcommand."""

    def test_list_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store.fetch_for_hotel.return_value = []
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "list-chunks", "--hotel", "h1"]
            main()

        out = capsys.readouterr().out
        assert "No chunks found" in out

    def test_list_shows_chunks(self, capsys: pytest.CaptureFixture[str]) -> None:
        chunk = MagicMock(
            doc_id="info.md",
            chunk_index=0,
            category="general",
            language="en",
            text="Breakfast is served from 7 to 10.",
        )

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store.fetch_for_hotel.return_value = [chunk]
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "list-chunks", "--hotel", "h1"]
            main()

        out = capsys.readouterr().out
        assert "info.md" in out
        assert "Total: 1 chunks" in out

    def test_list_filters_by_category(self, capsys: pytest.CaptureFixture[str]) -> None:
        spa = MagicMock(
            doc_id="spa.md", chunk_index=0, category="spa", language="en", text="Spa open 9-5."
        )
        food = MagicMock(
            doc_id="menu.md", chunk_index=0, category="food", language="en", text="Breakfast 7am."
        )

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store.fetch_for_hotel.return_value = [spa, food]
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "list-chunks", "--hotel", "h1", "--category", "spa"]
            main()

        out = capsys.readouterr().out
        assert "spa.md" in out
        assert "menu.md" not in out
        assert "Total: 1 chunks" in out


class TestSearch:
    """Tests for the search subcommand."""

    def test_search_no_results(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.rag.retriever.Retriever.retrieve", return_value=[]),
        ):
            mock_store_fn.return_value = MagicMock()

            sys.argv = ["voxtera", "search", "--hotel", "h1", "pool hours"]
            main()

        out = capsys.readouterr().out
        assert "No matching chunks" in out

    def test_search_with_results(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = MagicMock(score=0.85, doc_id="info.md", text="Pool open 8am-10pm")

        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("voxtera.rag.retriever.Retriever.retrieve", return_value=[result]),
        ):
            mock_store_fn.return_value = MagicMock()

            sys.argv = ["voxtera", "search", "--hotel", "h1", "pool hours"]
            main()

        out = capsys.readouterr().out
        assert "0.850" in out
        assert "Pool open 8am-10pm" in out
        assert "1 result(s)" in out


class TestDelete:
    """Tests for the delete subcommand."""

    def test_delete_confirmed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("builtins.input", return_value="y"),
        ):
            mock_store = MagicMock()
            mock_store.delete_doc.return_value = 3
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "delete", "--hotel", "h1", "--doc-id", "abc"]
            main()

            mock_store.delete_doc.assert_called_once_with(hotel_id="h1", doc_id="abc")

        out = capsys.readouterr().out
        assert "Deleted 3 chunk(s)" in out

    def test_delete_aborted(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
            patch("builtins.input", return_value="n"),
        ):
            mock_store = MagicMock()
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "delete", "--hotel", "h1", "--doc-id", "abc"]
            main()

            mock_store.delete_doc.assert_not_called()

        out = capsys.readouterr().out
        assert "Aborted" in out

    def test_delete_yes_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store.delete_doc.return_value = 5
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "delete", "--hotel", "h1", "--doc-id", "abc", "-y"]
            main()

            mock_store.delete_doc.assert_called_once()

        out = capsys.readouterr().out
        assert "Deleted 5 chunk(s)" in out

    def test_delete_zero_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Delete with -y when doc_id doesn't exist reports zero."""
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.cli._open_store") as mock_store_fn,
        ):
            mock_store = MagicMock()
            mock_store.delete_doc.return_value = 0
            mock_store_fn.return_value = mock_store

            sys.argv = ["voxtera", "delete", "--hotel", "h1", "--doc-id", "nope", "-y"]
            main()

        out = capsys.readouterr().out
        assert "No chunks found for that document" in out


class TestRun:
    """Tests for the run subcommand."""

    def test_run_delegates_to_bot(self) -> None:
        with (
            patch("voxtera.cli.load_dotenv"),
            patch("voxtera.bot.main") as mock_bot,
        ):
            sys.argv = ["voxtera", "run"]
            main()

            mock_bot.assert_called_once()
