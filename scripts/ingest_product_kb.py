"""Ingest docs/voxtera-rag-knowledge-base.md into a dedicated product vector store.

The script splits the document on ``## CHUNK:`` section boundaries (each section
is already a self-contained semantic unit), embeds each chunk with the local
multilingual-e5-small model, and upserts them into a SQLite database at
~/.voxtera/voxtera-product.db under tenant ``product``.

The demo server (serve.py) reads this database for the /api/product-chat endpoint
that powers the "About Voxtera" widget on the marketing landing pages.

Usage:
    uv run python scripts/ingest_product_kb.py
    # or with a custom output path:
    VOXTERA_PRODUCT_DB_PATH=/some/path/voxtera-product.db uv run python scripts/ingest_product_kb.py
"""

import os
import re
import sys
from pathlib import Path

# Make the voxtera package importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from voxtera.rag.embeddings import PREFIX_PASSAGE, embed_sync  # noqa: E402
from voxtera.rag.store import ChunksStore  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KB_PATH = Path(__file__).resolve().parent.parent / "docs" / "voxtera-rag-knowledge-base.md"
DEFAULT_DB = Path.home() / ".voxtera" / "voxtera-product.db"
DB_PATH = Path(os.environ.get("VOXTERA_PRODUCT_DB_PATH", str(DEFAULT_DB)))
TENANT_ID = "product"
DOC_ID = "voxtera-product-kb"

# ---------------------------------------------------------------------------
# Split on ## CHUNK: boundaries
# ---------------------------------------------------------------------------

_CHUNK_RE = re.compile(r"^## CHUNK:\s*(.+)$", re.MULTILINE)


def split_chunks(text: str) -> list[tuple[str, str]]:
    """Return list of (title, full_chunk_text) pairs split on ## CHUNK: headings."""
    matches = list(_CHUNK_RE.finditer(text))
    if not matches:
        raise ValueError("No ## CHUNK: sections found in the knowledge base file.")

    chunks = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        chunks.append((title, body))

    return chunks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if not KB_PATH.exists():
        print(f"ERROR: Knowledge base not found at {KB_PATH}")
        sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    raw = KB_PATH.read_text(encoding="utf-8")
    chunks = split_chunks(raw)
    print(f"Found {len(chunks)} chunks in {KB_PATH.name}")

    store = ChunksStore(DB_PATH)
    store.init_schema()

    # Delete any previous version of this doc so a re-run is clean.
    deleted = store.delete_doc(hotel_id=TENANT_ID, doc_id=DOC_ID)
    if deleted:
        print(f"Removed {deleted} old chunks from existing index")

    texts = [body for _, body in chunks]
    titles = [title for title, _ in chunks]

    print(f"Embedding {len(texts)} chunks (model: multilingual-e5-small)…")
    # Use passage prefix so the model treats these as documents, not queries.
    embeddings = embed_sync(texts, prefix=PREFIX_PASSAGE)

    for idx, (title, body, emb) in enumerate(zip(titles, texts, embeddings, strict=False)):
        store.upsert_chunk(
            hotel_id=TENANT_ID,
            doc_id=DOC_ID,
            chunk_index=idx,
            language="en",
            category=title,
            text=body,
            embedding=emb,
        )

    total = store.count(hotel_id=TENANT_ID)
    print(f"Done. {total} chunks stored in {DB_PATH}")
    print()
    print("Set VOXTERA_PRODUCT_DB_PATH in .env if you used a custom path:")
    print(f"  VOXTERA_PRODUCT_DB_PATH={DB_PATH}")


if __name__ == "__main__":
    main()
