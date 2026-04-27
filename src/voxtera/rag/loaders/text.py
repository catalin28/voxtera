"""Loader for Markdown and plain-text files.

Reads the file as UTF-8 with no transformation — the chunker already
handles markdown structure and degrades gracefully for plain text.

If the file isn't valid UTF-8 (common for hotel content authored on
European, Russian, or Turkish Windows machines), we auto-detect the
encoding via ``charset-normalizer`` and log a warning.
"""

from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes
from loguru import logger

from voxtera.rag.loaders import LoadedDocument


def _read_with_fallback(path: Path) -> str:
    """Read *path* as UTF-8, falling back to auto-detected encoding.

    charset-normalizer reliably detects Cyrillic (cp1251), Turkish (cp1254),
    Greek (cp1253), Arabic (cp1256), and CJK encodings.  For Latin-script
    content it often confuses Windows-1252 with other 8-bit codepages
    (cp775, cp850, etc.), so we trust it only for non-Latin families and
    default to Windows-1252 otherwise — which correctly covers the Western
    European languages Voxtera targets (EN, ES, FR, DE, IT, PT).
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        return text.replace("\r\n", "\n")
    except UnicodeDecodeError:
        pass

    # Encodings charset-normalizer reliably detects.
    trusted = {
        "cp1251", "koi8-r", "koi8-u",          # Russian / Ukrainian
        "cp1253",                                # Greek
        "cp1254",                                # Turkish
        "cp1255",                                # Hebrew
        "cp1256",                                # Arabic
        "shift_jis", "euc-jp", "iso-2022-jp",   # Japanese
        "gb2312", "gb18030", "gbk",             # Chinese
        "euc-kr",                                # Korean
        "big5",                                  # Traditional Chinese
    }

    result = from_bytes(raw).best()
    if result is not None and result.encoding in trusted:
        logger.warning(
            "{} is not valid UTF-8; detected {} instead.", path.name, result.encoding
        )
        text = str(result)
    else:
        logger.warning(
            "{} is not valid UTF-8; assuming Windows-1252 (Western European).",
            path.name,
        )
        text = raw.decode("windows-1252")

    return text.replace("\r\n", "\n")


def load_text(path: Path) -> LoadedDocument:
    """Load a ``.md``, ``.markdown``, or ``.txt`` file.

    Raises :class:`FileNotFoundError` if *path* does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Text file not found: {path}")

    text = _read_with_fallback(path)

    return LoadedDocument(
        doc_id=path.stem,
        text=text,
        metadata={
            "source_path": str(path),
            "format": path.suffix.lstrip(".") or "txt",
        },
    )
