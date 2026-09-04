"""A source database lock must not kill a run (finding 1.16).

Observed live on 2026-09-04: the Zotero routine started 11:13:10 and died
11:34:54 inside book 58 of 360, right after::

    ⚠️  adapter metadata hash failed for DNP5KC77: database is locked
        — falling back to extracted-metadata hash.
    Index:   625 chunks (7.7s) | total 1270.6s

No JSON dump followed; routine_history.jsonl recorded ``exit_code: 1,
stats: {}``. Four parts, each survivable alone:

1. ``busy_timeout`` was 5 s, shorter than a Zotero sync holds its write lock.
2. That single book took 1270 s — 102 s extraction, 1160 s embedding on the
   T1000. Twenty-one minutes of exposure per book against a live, user-operated
   database.
3. One of seven adapter call sites was guarded. ``compute_metadata_hash`` had a
   try/except (hence the *warning*); ``get_file_path``, called per book by
   Phase 3, did not — and took the process with it.
4. The traceback was stored nowhere: ``run_routine.py`` deliberately leaves
   stderr on the terminal so tqdm renders in place, and on a crash the summary
   that would have gone to watchdog.log is never written.

There is a fifth, quieter defect. The fallback in (3) stores the *Calibre-style*
hash (comments/tags/title/author/publisher) while the scanner later computes the
*Zotero adapter* hash (title/authors/tags/abstract/date). Those never match, so
each affected book reads as ``metadata_changed`` on the next scan — violating
the reindex-storm invariant the docstring at indexing.py states.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


class TestBusyTimeoutIsLongEnoughForASync:
    def test_default_is_no_longer_five_seconds(self):
        """5 s was tuned for "a brief writer lock"; a sync is not brief."""
        import inspect

        from src.archilles.sqlite_ro import connect_readonly

        default = inspect.signature(connect_readonly).parameters[
            "busy_timeout_ms"
        ].default
        assert default >= 30_000, "a Zotero sync routinely exceeds 5 s"

    def test_the_pragma_actually_carries_the_value(self, tmp_path):
        from src.archilles.sqlite_ro import connect_readonly

        db = tmp_path / "x.sqlite"
        sqlite3.connect(db).close()

        conn = connect_readonly(db, busy_timeout_ms=45_000)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 45_000
        finally:
            conn.close()

    def test_callers_can_still_shorten_it(self, tmp_path):
        """An interactive path may prefer to fail fast."""
        from src.archilles.sqlite_ro import connect_readonly

        db = tmp_path / "x.sqlite"
        sqlite3.connect(db).close()

        conn = connect_readonly(db, busy_timeout_ms=100)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 100
        finally:
            conn.close()


class TestALockedItemIsSkippedNotFatal:
    """Phase 3's per-book adapter calls must fail the *book*, not the run."""

    def _scanner(self, tmp_path):
        from src.archilles.watchdog import ZoteroWatchdogScanner

        (tmp_path / ".archilles").mkdir(parents=True, exist_ok=True)
        return ZoteroWatchdogScanner(
            library_path=tmp_path,
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=tmp_path / ".archilles",
        )

    def test_locked_get_file_path_records_an_error_and_continues(self, tmp_path):
        from src.archilles.watchdog import _resolve_file_path_safely

        class _Locked:
            def get_file_path(self, key):
                raise sqlite3.OperationalError("database is locked")

        results = {'errors': [], 'skipped_no_file': []}
        path = _resolve_file_path_safely(_Locked(), "KEY1", "T", results, "phase 3")

        assert path is None
        assert results['errors'], "a lock is an error, unlike an unresolvable path"
        assert "locked" in results['errors'][0]['error']

    def test_a_resolvable_path_passes_through(self, tmp_path):
        from src.archilles.watchdog import _resolve_file_path_safely

        target = tmp_path / "a.pdf"
        target.write_bytes(b"x")

        class _Ok:
            def get_file_path(self, key):
                return target

        results = {'errors': [], 'skipped_no_file': []}
        path = _resolve_file_path_safely(_Ok(), "KEY1", "T", results, "phase 3")

        assert path == target
        assert results['errors'] == []

    def test_an_unresolvable_path_is_a_skip_not_an_error(self, tmp_path):
        """Finding 1.15's distinction survives: no file is not a failure."""
        from src.archilles.watchdog import _resolve_file_path_safely

        class _NoFile:
            def get_file_path(self, key):
                return None

            def describe_unresolved(self, key):
                return "no attachment", ""

        results = {'errors': [], 'skipped_no_file': []}
        path = _resolve_file_path_safely(_NoFile(), "K", "T", results, "phase 3")

        assert path is None
        assert results['errors'] == []
        assert len(results['skipped_no_file']) == 1


class TestTheHashFallbackRefusesTheWrongShape:
    """The quiet half: storing a hash of the wrong shape guarantees the book
    reads as changed on every later scan."""

    def _indexer(self, adapter):
        from src.archilles.engine.indexing import Indexer

        return Indexer(SimpleNamespace(_adapter=adapter))

    def test_zotero_adapter_failure_yields_no_hash_at_all(self):
        """Better an empty hash — which the scanner reads as "unknown" — than a
        Calibre-shaped one it will compare against a Zotero-shaped one."""
        class _Locked:
            adapter_type = "zotero"

            def compute_metadata_hash(self, doc_id):
                raise sqlite3.OperationalError("database is locked")

        idx = self._indexer(_Locked())
        result = idx._resolve_metadata_hash("ZKEY1", {"title": "T", "comments": "c"})

        assert result == "", "a wrong-shaped hash re-indexes the book forever"

    def test_calibre_adapter_failure_may_still_fall_back(self):
        """For Calibre the two shapes are identical, so the fallback is safe —
        measured at 120/120 on the live library."""
        from src.archilles.hashing import compute_metadata_hash

        class _Locked:
            adapter_type = "calibre"

            def compute_metadata_hash(self, doc_id):
                raise sqlite3.OperationalError("database is locked")

        meta = {"title": "T", "author": "A", "comments": "c"}
        idx = self._indexer(_Locked())

        assert idx._resolve_metadata_hash("42", meta) == compute_metadata_hash(meta)

    def test_no_adapter_is_unaffected(self):
        from src.archilles.hashing import compute_metadata_hash

        meta = {"title": "T", "author": "A"}
        idx = self._indexer(None)

        assert idx._resolve_metadata_hash("42", meta) == compute_metadata_hash(meta)

    def test_a_working_adapter_still_wins(self):
        class _Fine:
            adapter_type = "zotero"

            def compute_metadata_hash(self, doc_id):
                return "adapter-hash"

        idx = self._indexer(_Fine())
        assert idx._resolve_metadata_hash("K", {"title": "T"}) == "adapter-hash"


class TestACrashLeavesEvidence:
    """``run_routine`` leaves stderr on the terminal so tqdm renders in place —
    a trade-off worth keeping. But it means a crash's traceback exists only in
    a console window: watchdog.log holds nothing, because the summary is
    written *after* the scan. Diagnosing the 2026-09-04 death needed a live
    re-run for exactly that reason.

    The fix belongs in the child, not in the pump: whatever kills a scan is
    written to the library's own watchdog.log first."""

    def test_traceback_is_written_to_the_watchdog_log(self, tmp_path):
        from src.archilles.watchdog import log_crash

        log = tmp_path / "watchdog.log"
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as exc:
            log_crash(log, exc)

        text = log.read_text(encoding="utf-8")
        assert "database is locked" in text
        assert "Traceback" in text
        assert "CRASH" in text

    def test_it_appends_rather_than_replacing(self, tmp_path):
        from src.archilles.watchdog import log_crash

        log = tmp_path / "watchdog.log"
        log.write_text("earlier run\n", encoding="utf-8")
        try:
            raise ValueError("boom")
        except ValueError as exc:
            log_crash(log, exc)

        text = log.read_text(encoding="utf-8")
        assert text.startswith("earlier run")
        assert "boom" in text

    def test_an_unwritable_log_does_not_mask_the_original_error(self, tmp_path):
        """The crash handler must never become the crash."""
        from src.archilles.watchdog import log_crash

        unwritable = tmp_path / "no-such-dir" / "watchdog.log"
        try:
            raise ValueError("original")
        except ValueError as exc:
            log_crash(unwritable, exc)  # must not raise


class TestTheScanStartFailsFast:
    """The long timeout is for mid-run locks, not for the scan's first read.

    Measured 2026-09-04: with 60 s at the scan start, a run against an open
    Zotero took 88 s to report ``db_locked`` and learned nothing it did not
    know after 5 s. Nothing is invested at that point — the next scheduled run
    simply tries again. Mid-run is the opposite: an abort throws away up to
    twenty minutes of embedding per book.
    """

    def test_scan_start_uses_a_short_timeout(self, tmp_path, monkeypatch):
        from src.archilles import watchdog

        (tmp_path / "zotero.sqlite").touch()
        seen = {}

        def spy(db_path, **kwargs):
            seen.update(kwargs)
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(watchdog, "connect_readonly", spy)

        with pytest.raises(sqlite3.OperationalError):
            watchdog._zotero_metadata_for_scan(tmp_path)

        assert seen["busy_timeout_ms"] <= 10_000, (
            "waiting a minute at scan start buys nothing — Zotero stays open"
        )

    def test_the_general_default_stays_long(self):
        """Everything not explicitly shortened keeps the mid-run value."""
        import inspect

        from src.archilles.sqlite_ro import connect_readonly

        assert inspect.signature(connect_readonly).parameters[
            "busy_timeout_ms"
        ].default >= 30_000
