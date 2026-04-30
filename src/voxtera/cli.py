"""Voxtera CLI — operator interface for hotel content management.

Subcommands
-----------
ingest       Load and embed files into the chunks store.
list-chunks  Show stored chunks for a hotel.
search       Semantic search over stored chunks.
delete       Remove a document's chunks from the store.
run          Start the voice bot.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from voxtera.rag.chunker import chunk_text
from voxtera.rag.embeddings import PREFIX_PASSAGE, embed
from voxtera.rag.loaders import load_document
from voxtera.rag.store import ChunksStore

# ---------------------------------------------------------------------------
# DB path helper
# ---------------------------------------------------------------------------

_DEFAULT_DB_PATH = Path.home() / ".voxtera" / "voxtera.db"


def _db_path() -> Path:
    return Path(os.environ.get("VOXTERA_DB_PATH", str(_DEFAULT_DB_PATH)))


def _open_store() -> ChunksStore:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    store = ChunksStore(path)
    store.init_schema()
    return store


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _ingest_file(
    file_path: Path,
    *,
    store: ChunksStore,
    hotel_id: str,
    language: str,
    category: str | None,
) -> None:
    """Load, chunk, embed, and store a single file."""
    try:
        doc = load_document(file_path)
    except (ValueError, OSError) as exc:
        logger.warning("Skipping {}: {}", file_path.name, exc)
        return

    chunks = chunk_text(doc.text)
    if not chunks:
        logger.info("No chunks from {}", file_path.name)
        return

    try:
        vectors = await embed([c.text for c in chunks], prefix=PREFIX_PASSAGE)
    except Exception:
        logger.opt(exception=True).warning("Embed failed for {}, skipping", file_path.name)
        return

    # If the operator did not pass --category, derive it from the filename
    # stem so a folder ingest (e.g. `voxtera ingest --hotel demo demo-hotel/`)
    # tags menu.md → "menu", spa.md → "spa", etc. Without this every file in
    # a folder walk would store NULL and `list-chunks --category X` would be
    # useless for multi-file ingest.
    effective_category = category if category is not None else file_path.stem.lower()

    # Re-ingest safety: a previous version of this doc may have produced more
    # chunks than the new version. Upsert by (hotel_id, doc_id, chunk_index)
    # would update indexes 0..N-1 but leave stale rows at indexes N..M-1
    # behind, which the retriever would keep returning. Wipe the doc first.
    store.delete_doc(hotel_id=hotel_id, doc_id=doc.doc_id)

    for i, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
        store.upsert_chunk(
            hotel_id=hotel_id,
            doc_id=doc.doc_id,
            chunk_index=i,
            language=language,
            category=effective_category,
            text=chunk.text,
            embedding=vec,
        )

    print(f"ingested {len(chunks)} chunks from {file_path.name}")


def _cmd_ingest(args: argparse.Namespace) -> None:
    target = Path(args.path)
    if not target.exists():
        print(f"Error: path not found: {target}", file=sys.stderr)
        sys.exit(1)

    store = _open_store()

    files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]

    if not files:
        print("No files found.")
        return

    async def _run() -> None:
        for file_path in files:
            await _ingest_file(
                file_path,
                store=store,
                hotel_id=args.hotel,
                language=args.language,
                category=args.category,
            )

    asyncio.run(_run())


def _cmd_list_chunks(args: argparse.Namespace) -> None:
    store = _open_store()
    chunks = store.fetch_for_hotel(hotel_id=args.hotel)

    if args.category:
        chunks = [c for c in chunks if c.category == args.category]

    if not chunks:
        print("No chunks found.")
        return

    print(f"{'doc_id':<20} {'idx':>4} {'cat':<15} {'lang':>5}  text")
    print("-" * 80)
    for c in chunks:
        preview = c.text[:60].replace("\n", " ")
        cat = c.category or ""
        print(f"{c.doc_id:<20} {c.chunk_index:>4} {cat:<15} {c.language:>5}  {preview}")

    print(f"\nTotal: {len(chunks)} chunks")


def _cmd_search(args: argparse.Namespace) -> None:
    from voxtera.rag.retriever import Retriever

    store = _open_store()

    # min_score uses the Retriever default (0.5) so CLI search and the live
    # bot apply the same relevance threshold.
    retriever = Retriever(store, top_k=args.top_k)
    results = asyncio.run(
        retriever.retrieve(hotel_id=args.hotel, query=args.query, language=args.language)
    )

    if not results:
        print("No matching chunks.")
        return

    for i, r in enumerate(results, 1):
        print(f"\n--- Result {i} (score: {r.score:.3f}, doc: {r.doc_id}) ---")
        print(r.text)

    print(f"\n{len(results)} result(s)")


def _cmd_delete(args: argparse.Namespace) -> None:
    store = _open_store()

    if not args.yes:
        answer = input(
            f"Delete all chunks for doc_id={args.doc_id!r} " f"in hotel={args.hotel!r}? [y/N] "
        )
        if answer.lower() != "y":
            print("Aborted.")
            return

    deleted = store.delete_doc(hotel_id=args.hotel, doc_id=args.doc_id)
    if deleted == 0:
        print("No chunks found for that document.")
    else:
        print(f"Deleted {deleted} chunk(s).")


def _cmd_run(_args: argparse.Namespace) -> None:
    from voxtera.bot import main as bot_main

    bot_main()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxtera",
        description="Voxtera — multilingual voice agent for tourism",
    )
    sub = parser.add_subparsers(dest="command")

    # -- ingest --------------------------------------------------------
    p_ingest = sub.add_parser("ingest", help="Ingest files into the chunks store")
    p_ingest.add_argument("--hotel", required=True, help="Hotel ID")
    p_ingest.add_argument("--category", default=None, help="Content category")
    p_ingest.add_argument("--language", default="en", help="Language code (default: en)")
    p_ingest.add_argument("path", help="File or folder to ingest")

    # -- list-chunks ---------------------------------------------------
    p_list = sub.add_parser("list-chunks", help="List stored chunks for a hotel")
    p_list.add_argument("--hotel", required=True, help="Hotel ID")
    p_list.add_argument("--category", default=None, help="Filter by category")

    # -- search --------------------------------------------------------
    p_search = sub.add_parser("search", help="Semantic search over stored chunks")
    p_search.add_argument("--hotel", required=True, help="Hotel ID")
    p_search.add_argument("--language", default=None, help="Filter by language")
    p_search.add_argument("--top-k", type=int, default=5, dest="top_k", help="Max results")
    p_search.add_argument("query", help="Search query")

    # -- delete --------------------------------------------------------
    p_delete = sub.add_parser("delete", help="Delete a document's chunks")
    p_delete.add_argument("--hotel", required=True, help="Hotel ID")
    p_delete.add_argument("--doc-id", required=True, dest="doc_id", help="Document ID")
    p_delete.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # -- run -----------------------------------------------------------
    sub.add_parser("run", help="Start the voice bot")

    return parser


def main() -> None:
    """CLI entry point."""
    load_dotenv()

    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "ingest": _cmd_ingest,
        "list-chunks": _cmd_list_chunks,
        "search": _cmd_search,
        "delete": _cmd_delete,
        "run": _cmd_run,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
