#!/usr/bin/env python3
"""Report and fix the KB tenant for the Çırağan/Kempinski docs.

The Çırağan docs (incl. the Tuğra menu) were ingested under hotel_id="demo",
but the WhatsApp bot queries hotel_id="kempinski_ciragan", so KB lookups return
nothing. The chunks are already embedded correctly — only the tenant tag is
wrong — so the fix is a relabel (UPDATE), not a re-ingest. Turkish docs (doc_id
ending "_tr") were also mis-tagged language="en"; we fix that too.

stdlib only (no sqlite3 CLI, no app deps needed).

USAGE
  python kb_tenant_report.py                 # report only (read-only)
  python kb_tenant_report.py --apply         # relabel tenant + fix language
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path


def _db_path() -> str:
    return os.environ.get("VOXTERA_DB_PATH") or str(Path.home() / ".voxtera" / "voxtera.db")


# Docs that belong to the Çırağan property — match by doc_id.
_MATCH = "(doc_id LIKE 'kempinski%' OR doc_id LIKE '%ciragan%')"


def _report(conn: sqlite3.Connection, target: str) -> None:
    print("\n-- chunks per (hotel_id, language) --")
    for hid, lang, n in conn.execute(
        "SELECT hotel_id, language, COUNT(*) FROM chunks GROUP BY hotel_id, language ORDER BY 1,2"
    ):
        print(f"   {hid:<22} {lang:<4} {n}")

    print("\n-- Çırağan docs: where they live now --")
    rows = list(
        conn.execute(
            f"SELECT hotel_id, language, doc_id, COUNT(*) FROM chunks WHERE {_MATCH} "
            "GROUP BY hotel_id, language, doc_id ORDER BY hotel_id, doc_id"
        )
    )
    if not rows:
        print("   (none found)")
    for hid, lang, doc, n in rows:
        print(f"   {hid:<22} {lang:<4} {doc:<40} {n} chunks")

    target_n = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE hotel_id=?", (target,)
    ).fetchone()[0]
    print(f"\n-- bot reads hotel_id={target!r}: {target_n} chunks "
          f"({'OK' if target_n else 'EMPTY → this is the bug'}) --")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="relabel tenant + fix language")
    ap.add_argument("--source-hotel", default="demo")
    ap.add_argument("--target-hotel", default="kempinski_ciragan")
    args = ap.parse_args()

    db = _db_path()
    if not Path(db).exists():
        print(f"ERROR: DB not found at {db}", file=sys.stderr)
        return 1
    print(f"DB={db}")
    conn = sqlite3.connect(db)

    print("\n=== BEFORE ===")
    _report(conn, args.target_hotel)

    if args.apply:
        cur = conn.execute(
            f"UPDATE chunks SET hotel_id=? WHERE hotel_id=? AND {_MATCH}",
            (args.target_hotel, args.source_hotel),
        )
        moved = cur.rowcount
        # Turkish docs (doc_id …_tr) were tagged en at ingest — correct them.
        cur2 = conn.execute(
            "UPDATE chunks SET language='tr' WHERE hotel_id=? AND doc_id LIKE '%\\_tr' ESCAPE '\\'",
            (args.target_hotel,),
        )
        retagged = cur2.rowcount
        conn.commit()
        print(f"\n=== APPLIED: moved {moved} chunks to {args.target_hotel!r}; "
              f"set language=tr on {retagged} chunks ===")
        print("\n=== AFTER ===")
        _report(conn, args.target_hotel)
        print("\nNOTE: restart the KB-caching service so it reloads "
              "(systemctl restart voxtera-concierge).")
    else:
        print("\nReport-only. Re-run with --apply to relabel.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
