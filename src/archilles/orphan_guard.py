"""Bounds on orphan deletion — the shared guard for every "gone from the
source" path (review 1.1).

Three paths derive orphans from a single scan and delete every indexed book
missing from it: ``WatchdogScanner.scan``, ``ZoteroWatchdogScanner.scan`` and
``batch_index.cleanup_orphans``. They agree on one thing only — a scan is
trusted completely. It should not be: a locked or syncing subtree, a source
opened mid-write, a mistyped ``exclude_patterns`` entry all yield a partial but
*non-empty* library, and every book missing from it then looks deleted. The
existing empty-snapshot check catches the total failure and nothing between it
and a correct scan.

Two independent reasons to refuse, kept in one place so all three paths refuse
alike:

* **Proportion.** More than ``ORPHAN_SHARE_LIMIT`` of the index *and* more than
  ``ORPHAN_COUNT_LIMIT`` books. Both conditions together, so a small library is
  not blocked by the percentage and a large one not by the count. A deliberate
  bulk deletion then costs one explicit flag; an accident stays a report.
* **A failed scan.** Not a size question at all: a scan that hit an I/O or
  permission error is not a deletion basis for even one book, and no flag
  overrides that — the caller does not know what it did not see.

Recovery is the other half. LanceDB keeps superseded versions for two days
(``LanceDBStore.optimize_indexes``, cut from seven in ``e8a5081`` because a
copy of this corpus is ~16 GB), while the report that would surface a wrong
deletion is the *weekly* status mail — the version is gone days before anyone
reads about it. So the rollback cannot be the version window.
``backup_orphan_chunks`` writes the doomed rows, vectors included, to Parquet
before the delete: an orphan set is a handful of books, costs megabytes rather
than gigabytes, and stays readable for as long as the file is kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: Refuse above this share of the index — but only together with the count.
ORPHAN_SHARE_LIMIT = 0.02
#: Refuse above this many books — but only together with the share.
ORPHAN_COUNT_LIMIT = 25

#: Name of the CLI flag that authorises a deliberate bulk deletion.
ALLOW_LARGE_FLAG = "--allow-large-orphan-cleanup"


@dataclass(frozen=True)
class OrphanBound:
    """Verdict on one proposed orphan deletion."""

    allowed: bool
    orphan_count: int
    indexed_count: int
    share: float
    reason: str

    def as_dict(self) -> dict:
        """Shape for ``results`` / JSON output, so a refusal is reportable."""
        return {
            "allowed": self.allowed,
            "orphan_count": self.orphan_count,
            "indexed_count": self.indexed_count,
            "share": round(self.share, 4),
            "reason": self.reason,
        }


def check_orphan_bound(
    orphan_count: int,
    indexed_count: int,
    *,
    allow_large: bool = False,
    scan_incomplete: bool = False,
) -> OrphanBound:
    """Decide whether this orphan set may be deleted.

    Args:
        orphan_count: books indexed but missing from the current scan.
        indexed_count: books in the index the scan was compared against.
        allow_large: the operator states the size is intended (the CLI flag).
        scan_incomplete: the scan reported an I/O or permission error.

    Returns an :class:`OrphanBound`; callers must not delete when
    ``allowed`` is False, and should report ``reason`` either way.
    """
    share = (orphan_count / indexed_count) if indexed_count else 0.0

    if scan_incomplete:
        return OrphanBound(
            allowed=False,
            orphan_count=orphan_count,
            indexed_count=indexed_count,
            share=share,
            reason=(
                "Refusing to delete: the library scan reported an I/O or "
                "permission error, so books missing from it may simply not "
                "have been seen. Fix the scan and re-run — this is not "
                f"overridable by {ALLOW_LARGE_FLAG}."
            ),
        )

    if orphan_count == 0:
        return OrphanBound(True, orphan_count, indexed_count, share, "No orphans.")

    oversized = share > ORPHAN_SHARE_LIMIT and orphan_count > ORPHAN_COUNT_LIMIT

    if oversized and allow_large:
        return OrphanBound(
            allowed=True,
            orphan_count=orphan_count,
            indexed_count=indexed_count,
            share=share,
            reason=(
                f"Deleting {orphan_count} of {indexed_count} indexed book(s) "
                f"({share:.1%}) — above the bound, allowed explicitly via "
                f"{ALLOW_LARGE_FLAG} (allow_large)."
            ),
        )

    if oversized:
        return OrphanBound(
            allowed=False,
            orphan_count=orphan_count,
            indexed_count=indexed_count,
            share=share,
            reason=(
                f"Refusing to delete {orphan_count} of {indexed_count} indexed "
                f"book(s) ({share:.1%}): above both bounds "
                f"(>{ORPHAN_SHARE_LIMIT:.0%} and >{ORPHAN_COUNT_LIMIT} books). "
                f"If the deletion is intended, re-run with {ALLOW_LARGE_FLAG}."
            ),
        )

    return OrphanBound(
        allowed=True,
        orphan_count=orphan_count,
        indexed_count=indexed_count,
        share=share,
        reason=(
            f"Deleting {orphan_count} of {indexed_count} indexed book(s) "
            f"({share:.1%}) — within the bound."
        ),
    )


def backup_orphan_chunks(
    store,
    book_ids: list[str],
    backup_dir: Path,
    *,
    batch_size: int = 50,
) -> Path | None:
    """Write every chunk of ``book_ids`` — vectors included — to one Parquet file.

    Called immediately *before* the deletion, never after: the ordering is the
    whole point. Same shape as the backup in ``scripts/dedupe_chunks.py``, so a
    restore uses the same reader.

    Returns the file path, or ``None`` when the books hold no rows (nothing to
    lose) or when the store cannot be read. Never raises: a failed backup must
    be reported by the caller and stop the deletion, not crash the routine.
    """
    if not book_ids:
        return None

    table = getattr(store, "table", None)
    if table is None:
        logger.warning("Orphan backup skipped: store has no open table")
        return None

    import pyarrow.parquet as pq

    from src.storage.lancedb_store import _sql_quote

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = backup_dir / f"orphans_{stamp}.parquet"

    writer = None
    written = 0
    try:
        for start in range(0, len(book_ids), batch_size):
            batch = book_ids[start:start + batch_size]
            ids = ", ".join(f"'{_sql_quote(b)}'" for b in batch)
            arrow = table.search().where(f"book_id IN ({ids})").limit(0).to_arrow()
            if arrow.num_rows == 0:
                continue
            if writer is None:
                writer = pq.ParquetWriter(path, arrow.schema)
            writer.write_table(arrow)
            written += arrow.num_rows
    except Exception as exc:
        logger.error("Orphan backup failed: %s", exc)
        if writer is not None:
            writer.close()
            writer = None
        # Best-effort: a writer that failed inside its own constructor can
        # leave the file open, and on Windows an open file cannot be removed.
        # A stray empty Parquet file is harmless; raising here would turn a
        # refused deletion into a crashed routine.
        try:
            path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            logger.warning("Could not remove partial backup %s: %s", path, unlink_exc)
        return None
    finally:
        if writer is not None:
            writer.close()

    if written == 0:
        path.unlink(missing_ok=True)
        return None

    logger.info("Backed up %d orphan chunk(s) to %s", written, path)
    return path
