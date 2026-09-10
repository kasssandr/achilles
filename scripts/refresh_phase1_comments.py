#!/usr/bin/env python3
"""Re-run phase 1 for phase1-only books so their Calibre comments become
structure-aware calibre_comment chunks.

Stubs written before the phase-1 comment fix hold the ENTIRE Calibre comment
inside the single PHASE1_METADATA vector — a 20k-word comment and a 50-word
one were treated identically — and carry no metadata_hash, so the watchdog
never noticed later comment edits. Re-indexing a stub with the current code
replaces it with a slim bibliographic stub plus properly split
calibre_comment chunks, and writes the hash that puts the book back into the
watchdog's delta detection.

Only phase1-only books are touched: books that already carry content chunks
are left completely alone, as are their content vectors.

Usage:
    python scripts/refresh_phase1_comments.py --dry-run
    python scripts/refresh_phase1_comments.py --limit 20
    python scripts/refresh_phase1_comments.py
"""
from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.watchdog import _resolve_paths                  # noqa: E402
from src.archilles.constants import ChunkType                # noqa: E402
from src.archilles.watchdog import WatchdogScanner           # noqa: E402
from src.archilles.book_files import discover_formats        # noqa: E402
from src.archilles.config import get_excluded_tags           # noqa: E402
from src.archilles.sqlite_ro import connect_readonly         # noqa: E402
from src.archilles.runtime_lock import routine_lock          # noqa: E402

CONTENT_TYPES = set(ChunkType.CONTENT_TYPES) | set(ChunkType.HIERARCHICAL_TYPES)


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    s = str(value)
    return "" if s == "nan" else s


def _survey(db_path: str) -> tuple[set[int], set[int]]:
    """Return (phase1_only_ids, ids_that_already_have_comment_chunks)."""
    import lance

    ds = lance.dataset(str(Path(db_path) / "chunks.lance"))
    rows = ds.to_table(columns=["calibre_id", "chunk_type"]).to_pylist()

    types: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        cid = r.get("calibre_id")
        if cid is None or (isinstance(cid, float) and cid != cid):
            continue
        types[int(cid)].add(r.get("chunk_type") or "")

    phase1_only = {
        cid for cid, t in types.items()
        if ChunkType.PHASE1_METADATA in t and not (t & CONTENT_TYPES)
    }
    migrated = {cid for cid in phase1_only
                if ChunkType.CALIBRE_COMMENT in types[cid]}
    return phase1_only, migrated


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="only report what would be re-indexed")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N books (repeatable — progress is checkpointed)")
    ap.add_argument("--force", action="store_true",
                    help="also redo stubs that already have comment chunks")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="ignore and do not write the checkpoint file")
    ap.add_argument("--wait-for-lock", type=int, default=7200,
                    help="seconds to wait for the global routine lock (default: 7200)")
    args = ap.parse_args()

    library_path, db_path, archilles_dir = _resolve_paths()
    print(f"Library:  {library_path}")
    print(f"Database: {db_path}")
    if args.dry_run:
        print("Mode:     dry-run (nothing will be written)")
    print()

    phase1_only, migrated = _survey(db_path)
    todo = sorted(phase1_only if args.force else phase1_only - migrated)

    ckpt_file = archilles_dir / "refresh_phase1_comments_checkpoint.json"
    done: set[int] = set()
    if ckpt_file.exists() and not args.no_checkpoint:
        try:
            done = {int(x) for x in json.loads(ckpt_file.read_text())["done"]}
        except Exception as exc:
            print(f"⚠️  Checkpoint unreadable ({exc}) — starting fresh.")
    todo = [c for c in todo if c not in done]

    # Calibre metadata: path + comment presence
    con = connect_readonly(Path(library_path) / "metadata.db", row_factory=sqlite3.Row)
    paths = {r["id"]: r["path"] for r in con.execute("SELECT id, path FROM books")}
    with_comment = {r[0] for r in con.execute(
        "SELECT book FROM comments WHERE text IS NOT NULL AND text != ''")}
    con.close()

    print(f"Phase-1-Stubs im Index      : {len(phase1_only):,}")
    print(f"  bereits migriert          : {len(migrated):,}")
    print(f"  laut Checkpoint erledigt  : {len(done):,}")
    print(f"  zu bearbeiten             : {len(todo):,}")
    print(f"  davon mit Kommentar       : {sum(1 for c in todo if c in with_comment):,}")
    if args.limit:
        todo = todo[:args.limit]
        print(f"  in diesem Lauf (--limit)  : {len(todo):,}")
    print()

    if args.dry_run or not todo:
        if not todo:
            print("Nichts zu tun.")
        return 0

    excluded = get_excluded_tags(library_path)
    # Same GPU / LanceDB / Calibre-SQLite resources as the scheduled routines —
    # take the global mutex so a Phase A or B task that fires mid-run waits
    # instead of writing into the same table concurrently.
    lock = routine_lock("refresh_phase1_comments", wait_s=args.wait_for_lock)
    if not lock.__enter__():
        print("ERROR: routine lock is held by another automation — aborting.",
              file=sys.stderr)
        return 2
    try:
        return _run(args, library_path, db_path, archilles_dir, excluded,
                    todo, done, ckpt_file, paths)
    finally:
        lock.__exit__(None, None, None)


def _run(args, library_path, db_path, archilles_dir, excluded,
         todo, done, ckpt_file, paths) -> int:
    scanner = WatchdogScanner(library_path=Path(library_path), db_path=db_path,
                              archilles_dir=archilles_dir, excluded_tags=excluded)
    rag = scanner._load_rag()

    stop = {"requested": False}

    def _handler(signum, frame):
        stop["requested"] = True
        print("\n⏸️  Abbruch angefordert — aktuelles Buch wird beendet, "
              "Fortschritt ist gesichert.")

    signal.signal(signal.SIGINT, _handler)

    t0 = time.time()
    ok = skipped = failed = 0
    for i, cid in enumerate(todo, 1):
        if stop["requested"]:
            break
        rel = paths.get(cid)
        if not rel:
            skipped += 1
            continue
        formats = discover_formats(Path(library_path) / rel)
        if not formats:
            skipped += 1
            continue
        print(f"[{i}/{len(todo)}] calibre_id={cid}")
        try:
            rag.index_book(formats[0]["path"], str(cid), phase="phase1")
            ok += 1
            done.add(cid)
        except Exception as exc:
            failed += 1
            print(f"  ✗ {exc}")
        if not args.no_checkpoint and (ok % 25 == 0 or i == len(todo)):
            ckpt_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")

    if not args.no_checkpoint:
        ckpt_file.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")

    dt = time.time() - t0
    print(f"\nFertig: {ok} neu indiziert, {skipped} uebersprungen, {failed} Fehler "
          f"in {dt:.0f}s ({dt/max(ok,1):.1f}s/Buch)")
    print(f"Checkpoint: {ckpt_file}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
