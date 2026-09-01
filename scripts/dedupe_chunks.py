#!/usr/bin/env python3
"""Remove duplicate LanceDB rows that share a chunk id.

LanceDB does not enforce id uniqueness. Every re-index that wrote chunks
without deleting the previous ones first (the pre-finding-8.7 behaviour)
left the old rows in place, so a single chunk id can carry a dozen copies.
`find_duplicate_chunks.py` reports the inventory; this script performs the
cleanup: for each duplicated id it keeps the newest row by `indexed_at` and
deletes the rest.

Duplicates are not merely wasted space. A search that hits one of the
affected books gets the same annotation back N times, and those copies
compete in the ranking with genuine hits from other books.

Safety:
  * `--dry-run` reports what would change and touches nothing.
  * A real run first writes EVERY row carrying a duplicated id - vectors
    included - to a Parquet backup, so any deletion can be undone.
  * The delete/re-insert happens per batch, and the kept rows are read back
    into memory (and verified complete) before their id is deleted.
  * A checkpoint file records finished batches so an interrupted run resumes.
  * The whole run holds the global routine lock, so a scheduled phase A or B
    waits instead of writing into the same table concurrently.

Usage:
    python scripts/dedupe_chunks.py --dry-run
    python scripts/dedupe_chunks.py --type annotation
    python scripts/dedupe_chunks.py --type calibre_comment --limit 100
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import lancedb                                               # noqa: E402
import pyarrow.parquet as pq                                 # noqa: E402

from src.archilles.config import get_rag_db_path             # noqa: E402
from src.archilles.runtime_lock import routine_lock          # noqa: E402

PROJECTION = ["id", "book_id", "text", "indexed_at"]

_interrupted = False


def _handle_sigint(signum, frame):  # pragma: no cover - interactive only
    global _interrupted
    _interrupted = True
    print("\nInterrupt received - finishing the current batch, then stopping.")


def _sql_quote(value: str) -> str:
    return str(value).replace("'", "''")


def load_rows(table, chunk_type: str | None, book_id: str | None = None) -> list[dict]:
    """Read the projection for one chunk_type (or the whole table).

    `book_id` narrows the scan to a single book. That matters for the content
    type: loading a million-plus rows with their text just to clean one book
    would need gigabytes of memory for nothing.
    """
    conditions = []
    if chunk_type:
        conditions.append(f"chunk_type = '{_sql_quote(chunk_type)}'")
    if book_id:
        conditions.append(f"book_id = '{_sql_quote(book_id)}'")
    query = table.search()
    if conditions:
        query = query.where(" AND ".join(conditions))
    return query.select(PROJECTION).limit(0).to_list()


def plan_dedupe(rows: list[dict]) -> dict:
    """Decide which row survives for every duplicated id.

    Returns a plan dict with the ids to rewrite, the row counts involved and
    a breakdown of how many duplicated ids carry identical vs. differing text.
    """
    counts = Counter(r["id"] for r in rows)
    dup_ids = {i for i, n in counts.items() if n > 1}

    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["id"] in dup_ids:
            by_id[r["id"]].append(r)

    identical = differing = 0
    excess = 0
    books: Counter = Counter()
    differing_examples: list[dict] = []

    for cid, group in by_id.items():
        excess += len(group) - 1
        books[group[0].get("book_id", "")] += len(group) - 1
        hashes = {
            hashlib.md5((r.get("text") or "").encode("utf-8")).hexdigest()
            for r in group
        }
        if len(hashes) == 1:
            identical += 1
            continue
        differing += 1
        ordered = sorted(group, key=lambda r: str(r.get("indexed_at") or ""))
        differing_examples.append({
            "id": cid,
            "book_id": group[0].get("book_id", ""),
            "keep_indexed_at": str(ordered[-1].get("indexed_at"))[:19],
            "keep_text": (ordered[-1].get("text") or "")[:400],
            "drop": [
                {
                    "indexed_at": str(r.get("indexed_at"))[:19],
                    "text": (r.get("text") or "")[:400],
                }
                for r in ordered[:-1]
            ],
        })

    return {
        "total_rows": len(rows),
        "duplicate_ids": sorted(dup_ids),
        "excess_rows": excess,
        "books": books,
        "identical_ids": identical,
        "differing_ids": differing,
        "differing_examples": differing_examples,
    }


def _runs(stamps: list[datetime], gap: timedelta) -> list[datetime]:
    """Group sorted timestamps into runs, returning each run's start."""
    starts = [stamps[0]]
    for previous, current in zip(stamps, stamps[1:]):
        if current - previous > gap:
            starts.append(current)
    return starts


def plan_stale(rows: list[dict], gap_minutes: int = 15) -> dict:
    """Find rows left over from an earlier indexing run of the same book.

    Chunk ids are positional (`<book>_comment_3`, `<book>_annot_7`). When a
    comment is shortened or a highlight removed, the current run writes fewer
    chunks and the high numbers from the previous run keep sitting in the
    table. They are not duplicates - no id repeats - so deduping never touches
    them, yet they answer searches with text the book no longer contains.

    A "run" is a cluster of `indexed_at` stamps less than `gap_minutes` apart:
    writing a few thousand chunks takes minutes, so a single run spans several
    timestamps. Any surviving row older than its book's newest run is stale.
    """
    gap = timedelta(minutes=gap_minutes)

    newest_per_id: dict[str, dict] = {}
    for row in rows:
        current = newest_per_id.get(row["id"])
        if current is None or str(row.get("indexed_at") or "") > str(current.get("indexed_at") or ""):
            newest_per_id[row["id"]] = row

    by_book: dict[str, list[dict]] = defaultdict(list)
    for row in newest_per_id.values():
        by_book[row.get("book_id", "")].append(row)

    stale_ids: list[str] = []
    books: Counter = Counter()
    for book, book_rows in by_book.items():
        stamps = sorted({s for s in (_parse_stamp(r.get("indexed_at")) for r in book_rows) if s})
        if len(stamps) < 2:
            continue
        last_start = _runs(stamps, gap)[-1]
        for row in book_rows:
            stamp = _parse_stamp(row.get("indexed_at"))
            if stamp is not None and stamp < last_start:
                stale_ids.append(row["id"])
                books[book] += 1

    return {"stale_ids": sorted(stale_ids), "books": books}


def _parse_stamp(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


def print_report(plan: dict, chunk_type: str | None, top: int = 12) -> None:
    label = chunk_type or "ALL types"
    print(f"\n{'=' * 62}")
    print(f"  Duplicate row cleanup - {label}")
    print(f"{'=' * 62}")
    print(f"  Rows scanned .......... {plan['total_rows']:>9,}")
    print(f"  Duplicated ids ........ {len(plan['duplicate_ids']):>9,}")
    print(f"    text identical ...... {plan['identical_ids']:>9,}")
    print(f"    text differing ...... {plan['differing_ids']:>9,}")
    print(f"  Rows to delete ........ {plan['excess_rows']:>9,}")
    print(f"  Books affected ........ {len(plan['books']):>9,}")
    if plan["books"]:
        print("\n  Most affected books (excess rows):")
        for book_id, n in plan["books"].most_common(top):
            print(f"    {book_id:>8}  {n:>7,}")
        if len(plan["books"]) > top:
            print(f"    ... and {len(plan['books']) - top} more books")


def _batch_filter(batch: list[str], chunk_type: str | None) -> str:
    """SQL predicate for one id batch, pinned to the chunk_type being cleaned.

    The type filter is not cosmetic: ids are only unique per chunk_type, so a
    bare `id IN (...)` delete would also remove a content row that happens to
    share an annotation's id - and the re-insert would not bring it back.
    """
    id_list = ", ".join(f"'{_sql_quote(i)}'" for i in batch)
    predicate = f"id IN ({id_list})"
    if chunk_type:
        predicate += f" AND chunk_type = '{_sql_quote(chunk_type)}'"
    return predicate


def backup_rows(table, dup_ids: list[str], backup_path: Path, batch_size: int,
                chunk_type: str | None) -> int:
    """Write every row carrying a duplicated id - vectors included - to Parquet."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    written = 0
    try:
        for start in range(0, len(dup_ids), batch_size):
            batch = dup_ids[start:start + batch_size]
            arrow = table.search().where(_batch_filter(batch, chunk_type)).limit(0).to_arrow()
            if arrow.num_rows == 0:
                continue
            if writer is None:
                writer = pq.ParquetWriter(backup_path, arrow.schema)
            writer.write_table(arrow)
            written += arrow.num_rows
            print(f"    backed up {written:,} rows", end="\r")
    finally:
        if writer is not None:
            writer.close()
    print(f"    backed up {written:,} rows        ")
    return written


def dedupe(table, dup_ids: list[str], batch_size: int, checkpoint: Path,
           chunk_type: str | None = None) -> dict:
    """Delete duplicated ids batch by batch and re-insert the newest row."""
    done: set[str] = set()
    if checkpoint.exists():
        try:
            done = set(json.loads(checkpoint.read_text(encoding="utf-8")))
            if done:
                print(f"  Resuming - {len(done):,} ids already deduped")
        except Exception as exc:
            print(f"  Could not read checkpoint ({exc}) - starting fresh")

    remaining = [i for i in dup_ids if i not in done]
    stats = {"ids": 0, "deleted": 0, "reinserted": 0, "batches": 0}

    for start in range(0, len(remaining), batch_size):
        if _interrupted:
            break
        batch = remaining[start:start + batch_size]
        predicate = _batch_filter(batch, chunk_type)

        arrow = table.search().where(predicate).limit(0).to_arrow()
        if arrow.num_rows == 0:
            done.update(batch)
            continue

        # Pick the newest row per id, in memory, before deleting anything.
        ids = arrow.column("id").to_pylist()
        stamps = arrow.column("indexed_at").to_pylist()
        newest: dict[str, int] = {}
        for pos, (cid, stamp) in enumerate(zip(ids, stamps)):
            prev = newest.get(cid)
            if prev is None or str(stamp or "") >= str(stamps[prev] or ""):
                newest[cid] = pos
        keep = arrow.take(sorted(newest.values()))

        found = set(ids)
        if keep.num_rows != len(found):
            raise RuntimeError(
                f"keep set incomplete: {keep.num_rows} rows for {len(found)} ids - aborting"
            )

        before = table.count_rows()
        table.delete(predicate)
        after_delete = table.count_rows()
        table.add(keep)
        after_add = table.count_rows()

        stats["ids"] += len(found)
        stats["deleted"] += before - after_delete
        stats["reinserted"] += after_add - after_delete
        stats["batches"] += 1
        done.update(batch)
        checkpoint.write_text(json.dumps(sorted(done)), encoding="utf-8")

        print(
            f"  [{stats['ids']:>6,}/{len(dup_ids):,} ids] "
            f"deleted {before - after_delete:>5,}, re-inserted {after_add - after_delete:>4,}, "
            f"net -{before - after_add:,}",
            end="\r",
        )

    print()
    return stats


def drop_stale(table, stale_ids: list[str], batch_size: int, chunk_type: str | None,
               backup_path: Path) -> int:
    """Back up and delete rows left over from earlier runs."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    deleted = 0
    try:
        for start in range(0, len(stale_ids), batch_size):
            if _interrupted:
                break
            batch = stale_ids[start:start + batch_size]
            predicate = _batch_filter(batch, chunk_type)

            arrow = table.search().where(predicate).limit(0).to_arrow()
            if arrow.num_rows == 0:
                continue
            if writer is None:
                writer = pq.ParquetWriter(backup_path, arrow.schema)
            writer.write_table(arrow)

            before = table.count_rows()
            table.delete(predicate)
            deleted += before - table.count_rows()
            print(f"  deleted {deleted:,}/{len(stale_ids):,} stale rows", end="\r")
    finally:
        if writer is not None:
            writer.close()
    print()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove duplicate LanceDB rows sharing a chunk id (keeps the newest)."
    )
    parser.add_argument("--type", default="annotation",
                        help="chunk_type to clean (default: annotation; 'all' for the whole table)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only - nothing is written")
    parser.add_argument("--limit", type=int,
                        help="Process at most N duplicated ids (for a cautious first run)")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Ids per delete/re-insert batch (default: 200)")
    parser.add_argument("--db-path", help="LanceDB directory (default: configured rag_db)")
    parser.add_argument("--backup-dir",
                        help="Where to write the Parquet backup (default: <db>/../backups)")
    parser.add_argument("--book", metavar="BOOK_ID",
                        help="Restrict the cleanup to a single book_id")
    parser.add_argument("--drop-stale", action="store_true",
                        help="Also delete rows left over from an earlier run of the same book "
                             "(positional ids that the current run no longer writes)")
    parser.add_argument("--gap-minutes", type=int, default=15,
                        help="Timestamps closer than this belong to the same run (default: 15)")
    parser.add_argument("--dump-differing", metavar="FILE",
                        help="Write the differing-text rows that would be dropped to a JSON file")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)

    db_path = Path(args.db_path) if args.db_path else Path(get_rag_db_path())
    chunk_type = None if args.type.lower() == "all" else args.type

    print(f"Database: {db_path}")
    db = lancedb.connect(str(db_path))
    table = db.open_table("chunks")

    scope = chunk_type or 'all types'
    if args.book:
        scope += f", book {args.book}"
    print(f"Reading rows ({scope}) ...")
    t0 = time.time()
    rows = load_rows(table, chunk_type, args.book)
    print(f"   {len(rows):,} rows in {time.time() - t0:.1f}s")

    plan = plan_dedupe(rows)
    print_report(plan, chunk_type)

    stale = {"stale_ids": [], "books": Counter()}
    if args.drop_stale:
        stale = plan_stale(rows, args.gap_minutes)
        print(f"\n  Stale rows (earlier run) {len(stale['stale_ids']):>9,}")
        print(f"  Books affected ........ {len(stale['books']):>9,}")
        for book_id, n in stale["books"].most_common(5):
            print(f"    {book_id:>8}  {n:>7,}")

    if args.dump_differing:
        out = Path(args.dump_differing)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan["differing_examples"], indent=1, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\n  Differing-text rows written to {out}")

    dup_ids = plan["duplicate_ids"]
    if not dup_ids and not stale["stale_ids"]:
        print("\nNothing to do - no duplicated ids, no stale rows.")
        return 0

    if args.limit:
        dup_ids = dup_ids[:args.limit]
        print(f"\n  Limited to the first {len(dup_ids):,} duplicated ids")

    if args.dry_run:
        print("\nDry run - nothing was written.")
        return 0

    if not args.yes:
        total = plan["excess_rows"] + len(stale["stale_ids"])
        answer = input(f"\nDelete {total:,} rows? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 1

    backup_dir = Path(args.backup_dir) if args.backup_dir else db_path.parent / "backups"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"dedupe_{chunk_type or 'all'}_{stamp}.parquet"
    checkpoint = db_path.parent / f".dedupe_{chunk_type or 'all'}_checkpoint.json"

    with routine_lock("dedupe_chunks", wait_s=1800) as acquired:
        if not acquired:
            print("Another ARCHILLES routine holds the lock - try again later.")
            return 1

        t0 = time.time()
        stats = {"ids": 0, "deleted": 0, "reinserted": 0, "batches": 0}
        if dup_ids:
            print(f"\nBacking up affected rows -> {backup_path}")
            backup_rows(table, dup_ids, backup_path, args.batch_size, chunk_type)

            print(f"\nDeduping {len(dup_ids):,} ids ...")
            stats = dedupe(table, dup_ids, args.batch_size, checkpoint, chunk_type)

        stale_deleted = 0
        if stale["stale_ids"] and not _interrupted:
            stale_backup = backup_path.with_name(backup_path.stem + "_stale.parquet")
            print(f"\nDropping {len(stale['stale_ids']):,} stale rows -> backup {stale_backup.name}")
            stale_deleted = drop_stale(table, stale["stale_ids"], args.batch_size,
                                       chunk_type, stale_backup)
        elapsed = time.time() - t0

    print(f"\n{'=' * 62}")
    print(f"  ids processed ......... {stats['ids']:>9,}")
    print(f"  rows deleted .......... {stats['deleted']:>9,}")
    print(f"  rows re-inserted ...... {stats['reinserted']:>9,}")
    print(f"  net rows removed ...... {stats['deleted'] - stats['reinserted']:>9,}")
    if args.drop_stale:
        print(f"  stale rows deleted .... {stale_deleted:>9,}")
    print(f"  elapsed ............... {elapsed / 60:>9.1f} min")
    print(f"  backup ................ {backup_path}")
    print(f"{'=' * 62}")

    if _interrupted:
        print("\nStopped early - re-run to continue from the checkpoint.")
        return 1

    checkpoint.unlink(missing_ok=True)
    print("\nDone. Consider compacting the index on the next routine run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
