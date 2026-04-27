"""Loader package — shared data model and dispatch registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoadedDocument:
    """Result of loading a single file."""

    doc_id: str
    text: str
    metadata: dict[str, str]


# Extension → loader name mapping (case-insensitive lookup at call site)
_EXT_MAP: dict[str, str] = {
    ".pdf": "pdf",
    ".md": "text",
    ".markdown": "text",
    ".txt": "text",
}


def load_document(path: Path) -> LoadedDocument:
    """Pick the right loader based on file extension and return a LoadedDocument."""
    ext = path.suffix.lower()
    loader = _EXT_MAP.get(ext)
    if loader is None:
        raise ValueError(f"Unsupported file extension: {ext!r}")

    if loader == "pdf":
        from voxtera.rag.loaders.pdf import load_pdf

        return load_pdf(path)

    # loader == "text"
    from voxtera.rag.loaders.text import load_text

    return load_text(path)
