#!/usr/bin/env python3
"""
ARCHILLES Safe Indexer

Graceful-shutdown wrapper around indexing: the first CTRL+C finishes the
current book and stops, the second exits hard.

Backups used to live here (a full copytree of the LanceDB every 50 books,
keeping two copies). That is gone: at 1.5M chunks a copy is tens of GB, it
snapshotted a half-written database mid-run, it only ever ran from
batch_index (never from the scheduled routines, so it silently stopped
producing backups when those took over), and the copies landed on the same
drive as the database — no protection against disk loss, but a real risk of
filling the disk.

What protects the index now, in order of what each actually covers:

* **Deletions** — the orphan-cleanup paths write every doomed row, vectors
  included, to ``<db>/../backups/orphans_<stamp>.parquet`` *before* deleting
  (src/archilles/orphan_guard.py). This is the rollback that matters, because
  deletion is the only irreversible operation the routines perform, and the
  file stays until someone removes it.
* **Accidental mass deletion** — a proportionality bound refuses a cleanup
  above 2% *and* 25 books, and any scan that reported an I/O error blocks the
  cleanup outright.
* **Recent writes** — LanceDB keeps superseded table versions for **two days**
  (LanceDBStore.optimize_indexes; cut from seven in e8a5081 because one copy of
  this corpus is ~16 GB). Two days is shorter than the weekly status mail, so
  it covers a same-week mistake noticed quickly — it is not a deletion
  rollback, which is why the Parquet files exist.
* **Total loss** — the database is reproducible from Calibre plus
  prepared_chunks. Slow, but complete.
"""

import sys
import signal
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class SafeIndexer:
    """
    Graceful-shutdown wrapper for indexing runs.

    Handles:
    - CTRL+C graceful shutdown (finish current book, then stop)
    - Resume after interruption (skip-existing comes from LanceDB, not here)
    """

    def __init__(self, db_path: Path):
        """
        Initialize safe indexer.

        Args:
            db_path: Path to LanceDB directory
        """
        self.db_path = Path(db_path)

        # State
        self.shutdown_requested = False

        # Register signal handlers
        self._register_signal_handlers()

    def _register_signal_handlers(self):
        """Register handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            if not self.shutdown_requested:
                print(f"\n\n{'='*60}")
                print(f"⏸️  SHUTDOWN REQUESTED (CTRL+C)")
                print(f"{'='*60}")
                print(f"  Finishing current book, then stopping...")
                print(f"  Press CTRL+C again to force quit (may corrupt database!)")
                print(f"{'='*60}\n")
                self.shutdown_requested = True
            else:
                print(f"\n⚠️  FORCE QUIT - Database may be corrupted!")
                print(f"   Use --reset-db on next run if needed.\n")
                sys.exit(1)

        signal.signal(signal.SIGINT, signal_handler)
        # Also handle SIGTERM (systemd, Docker, etc.)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, signal_handler)

    def should_shutdown(self) -> bool:
        """Check if shutdown was requested."""
        return self.shutdown_requested
