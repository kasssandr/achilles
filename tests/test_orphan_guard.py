"""Tests for the orphan-deletion guards (review 1.1).

Three paths derive "gone from the source" from a single scan and delete every
indexed book not in it: the two watchdog scanners and ``batch_index
--cleanup-orphans``. A scan that under-reports — a locked subtree, a source
mid-sync, a mistyped exclude pattern — therefore reads as a mass deletion, and
the Lab routine runs the least protected of the three daily and unattended.

Three guards, tested here:

(a) a proportionality bound shared by all three paths,
(b) a scan that hit an I/O error is not a usable deletion basis at all, and
(c) the deleted rows are written to Parquet — vectors included — before the
    delete, so a wrong deletion is recoverable independently of LanceDB's
    two-day version retention.
"""

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow.parquet as pq
import pytest

from src.adapters import folder_adapter
from src.adapters.folder_adapter import FolderAdapter
from src.archilles.orphan_guard import (
    ORPHAN_COUNT_LIMIT,
    ORPHAN_SHARE_LIMIT,
    backup_orphan_chunks,
    check_orphan_bound,
)
from src.storage.lancedb_store import LanceDBStore


# ── helpers ──────────────────────────────────────────────────────────

def _chunks(n, book_id):
    return [
        {
            "id": f"{book_id}_chunk_{i}",
            "text": f"chunk {i} of {book_id}",
            "book_id": book_id,
            "calibre_id": 0,
            "chunk_index": i,
            "chunk_type": "content",
        }
        for i in range(n)
    ]


def _emb(n, dim=1024):
    v = np.random.randn(n, dim).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


# ── (a) the proportionality bound ────────────────────────────────────

class TestProportionalityBound:
    """Refuse only when the orphan set is *both* a large share and large in
    absolute terms — small libraries must not be blocked by the percentage,
    large ones not by the count."""

    def test_ordinary_deletion_passes(self):
        bound = check_orphan_bound(orphan_count=3, indexed_count=1000)
        assert bound.allowed

    def test_large_share_and_large_count_refuses(self):
        bound = check_orphan_bound(orphan_count=200, indexed_count=1000)
        assert not bound.allowed
        assert "200" in bound.reason

    def test_large_count_but_small_share_passes(self):
        """A big library losing 30 of 10 000 books is a plausible clean-up."""
        bound = check_orphan_bound(orphan_count=30, indexed_count=10_000)
        assert bound.allowed

    def test_large_share_but_small_count_passes(self):
        """A 20-book library losing 5 is a normal day, not a catastrophe."""
        bound = check_orphan_bound(orphan_count=5, indexed_count=20)
        assert bound.allowed

    def test_boundary_is_strictly_greater(self):
        """Exactly at the limits is still allowed — both bounds are `>`."""
        at_count = ORPHAN_COUNT_LIMIT
        indexed = int(at_count / ORPHAN_SHARE_LIMIT)  # share == limit exactly
        assert check_orphan_bound(orphan_count=at_count, indexed_count=indexed).allowed

    def test_explicit_flag_overrides_the_refusal(self):
        """The deliberate deletion is one flag; the accident stays a report."""
        bound = check_orphan_bound(
            orphan_count=200, indexed_count=1000, allow_large=True
        )
        assert bound.allowed
        assert "allow_large" in bound.reason or "explicit" in bound.reason.lower()

    def test_empty_index_does_not_divide_by_zero(self):
        bound = check_orphan_bound(orphan_count=0, indexed_count=0)
        assert bound.allowed
        assert bound.share == 0.0

    def test_scan_error_alone_blocks_regardless_of_size(self):
        """A failed scan is not a deletion basis even for a single orphan."""
        bound = check_orphan_bound(
            orphan_count=1, indexed_count=1000, scan_incomplete=True
        )
        assert not bound.allowed
        assert "scan" in bound.reason.lower()

    def test_scan_error_is_not_overridable_by_the_flag(self):
        """--allow-large says "this many is intended", not "ignore the error"."""
        bound = check_orphan_bound(
            orphan_count=1, indexed_count=1000,
            scan_incomplete=True, allow_large=True,
        )
        assert not bound.allowed


# ── (b) a failed scan must be visible to the caller ──────────────────

class TestFolderScanErrorsAreVisible:
    """``os.walk`` swallows I/O and permission errors by default: a locked or
    syncing subtree silently yields a partial — and non-empty — tree, which the
    orphan path then reads as "those documents are gone"."""

    def test_clean_scan_is_not_flagged(self, tmp_path):
        (tmp_path / "a.md").write_text("# a", encoding="utf-8")
        adapter = FolderAdapter(tmp_path)
        adapter.list_documents()
        assert adapter.scan_incomplete is False

    def test_walk_error_marks_the_scan_incomplete(self, tmp_path, monkeypatch):
        (tmp_path / "a.md").write_text("# a", encoding="utf-8")
        real_walk = os.walk

        def failing_walk(top, *args, **kwargs):
            onerror = kwargs.get("onerror")
            yield from real_walk(top)
            if onerror is not None:
                onerror(OSError(13, "Permission denied", str(Path(top) / "locked")))

        monkeypatch.setattr(folder_adapter.os, "walk", failing_walk)
        adapter = FolderAdapter(tmp_path)
        docs = adapter.list_documents()

        assert docs, "the partial result is still returned — only flagged"
        assert adapter.scan_incomplete is True

    def test_flag_resets_on_a_clean_rescan(self, tmp_path, monkeypatch):
        (tmp_path / "a.md").write_text("# a", encoding="utf-8")
        real_walk = os.walk

        def failing_walk(top, *args, **kwargs):
            onerror = kwargs.get("onerror")
            yield from real_walk(top)
            if onerror is not None:
                onerror(OSError(13, "Permission denied", str(top)))

        monkeypatch.setattr(folder_adapter.os, "walk", failing_walk)
        adapter = FolderAdapter(tmp_path)
        adapter.list_documents()
        assert adapter.scan_incomplete is True

        monkeypatch.setattr(folder_adapter.os, "walk", real_walk)
        adapter.invalidate_cache()
        adapter.list_documents()
        assert adapter.scan_incomplete is False

    def test_adapters_default_to_a_healthy_scan(self):
        """Adapters that cannot report a partial scan must not read as broken."""
        from src.adapters.base import SourceAdapter

        assert SourceAdapter.scan_incomplete is False


# ── (c) the deleted rows survive the deletion ────────────────────────

class TestBackupBeforeDelete:
    """LanceDB's version retention is two days (`e8a5081`) and the report that
    would surface a wrong deletion is the *weekly* mail — so the rollback
    cannot depend on the version window. The rows themselves are cheap: an
    orphan set is a handful of books, not the 16 GB an index copy costs."""

    def test_orphan_rows_are_written_to_parquet(self, tmp_path):
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(_chunks(4, "gone_1"), _emb(4))
        store.add_chunks(_chunks(3, "gone_2"), _emb(3))
        store.add_chunks(_chunks(2, "stays"), _emb(2))

        path = backup_orphan_chunks(store, ["gone_1", "gone_2"], tmp_path / "backups")

        assert path is not None and path.exists()
        table = pq.read_table(path)
        assert table.num_rows == 7
        assert set(table.column("book_id").to_pylist()) == {"gone_1", "gone_2"}

    def test_backup_keeps_the_vectors(self, tmp_path):
        """A backup without vectors would need a re-embed to restore — the
        expensive half of what was lost."""
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(_chunks(2, "gone"), _emb(2))

        path = backup_orphan_chunks(store, ["gone"], tmp_path / "backups")

        table = pq.read_table(path)
        assert "vector" in table.column_names
        first = table.column("vector")[0].as_py()
        assert first is not None and len(first) == 1024

    def test_no_rows_means_no_file(self, tmp_path):
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(_chunks(2, "stays"), _emb(2))

        assert backup_orphan_chunks(store, ["never_indexed"], tmp_path / "backups") is None

    def test_backup_precedes_deletion(self, tmp_path):
        """Ordering is the whole point: a backup written after the delete would
        be empty."""
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(_chunks(3, "gone"), _emb(3))

        path = backup_orphan_chunks(store, ["gone"], tmp_path / "backups")
        store.delete_by_book_id("gone")

        assert store.get_by_book_id("gone", limit=1) == []
        assert pq.read_table(path).num_rows == 3


# ── the three deletion paths, wired ──────────────────────────────────

class _FakeAdapter:
    """A library that reports fewer documents than the index holds."""

    adapter_type = "folder"

    def __init__(self, doc_ids, scan_incomplete=False):
        self._doc_ids = list(doc_ids)
        self.scan_incomplete = scan_incomplete

    def list_documents(self, **_kwargs):
        return [SimpleNamespace(doc_id=d) for d in self._doc_ids]


class _FakeRAG:
    def __init__(self, store):
        self.store = store


def _reopen(store):
    """A second handle on the same database.

    ``_cleanup_orphaned_books`` opens its own store, so a handle created before
    the call keeps pointing at the pre-deletion table version — the test must
    read the database, not its own stale view of it.
    """
    return LanceDBStore(db_path=str(store.db_path))


def _store_with_books(tmp_path, n_books, chunks_each=2):
    store = LanceDBStore(db_path=str(tmp_path / "db"))
    for i in range(n_books):
        store.add_chunks(_chunks(chunks_each, f"b{i:03d}"), _emb(chunks_each))
    return store


class TestCleanupOrphansIsBounded:
    """``batch_index --cleanup-orphans`` is the least protected of the three
    paths and the one the Lab routine runs daily, unattended."""

    def test_oversized_orphan_set_is_refused(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100)
        # The library reports only 10 of the 100 indexed documents.
        adapter = _FakeAdapter([f"b{i:03d}" for i in range(10)])

        result = cleanup_orphans(_FakeRAG(store), tmp_path, adapter=adapter)

        assert result['orphans_found'] == 90
        assert result['orphans_removed'] == 0
        assert result['refused'] is True
        assert store.get_by_book_id("b099", limit=1), "nothing may be deleted"

    def test_explicit_flag_lets_the_deliberate_deletion_through(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100)
        adapter = _FakeAdapter([f"b{i:03d}" for i in range(10)])

        result = cleanup_orphans(
            _FakeRAG(store), tmp_path, adapter=adapter, allow_large=True
        )

        assert result['orphans_removed'] == 90
        assert store.get_by_book_id("b099", limit=1) == []

    def test_ordinary_deletion_still_proceeds(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100)
        adapter = _FakeAdapter([f"b{i:03d}" for i in range(98)])

        result = cleanup_orphans(_FakeRAG(store), tmp_path, adapter=adapter)

        assert result['orphans_removed'] == 2
        assert store.get_by_book_id("b099", limit=1) == []

    def test_failed_scan_blocks_even_a_single_orphan(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100)
        adapter = _FakeAdapter(
            [f"b{i:03d}" for i in range(99)], scan_incomplete=True
        )

        result = cleanup_orphans(_FakeRAG(store), tmp_path, adapter=adapter)

        assert result['orphans_removed'] == 0
        assert result['refused'] is True
        assert store.get_by_book_id("b099", limit=1)

    def test_failed_scan_is_not_overridable_by_the_flag(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100)
        adapter = _FakeAdapter(
            [f"b{i:03d}" for i in range(99)], scan_incomplete=True
        )

        result = cleanup_orphans(
            _FakeRAG(store), tmp_path, adapter=adapter, allow_large=True
        )

        assert result['orphans_removed'] == 0
        assert store.get_by_book_id("b099", limit=1)

    def test_deletion_writes_a_rollback_file(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100, chunks_each=3)
        adapter = _FakeAdapter([f"b{i:03d}" for i in range(98)])

        result = cleanup_orphans(_FakeRAG(store), tmp_path, adapter=adapter)

        backup = Path(result['backup_path'])
        assert backup.exists()
        table = pq.read_table(backup)
        assert table.num_rows == 6  # two books, three chunks each
        assert set(table.column("book_id").to_pylist()) == {"b098", "b099"}
        assert "vector" in table.column_names

    def test_dry_run_deletes_nothing_and_writes_nothing(self, tmp_path):
        from scripts.batch_index import cleanup_orphans

        store = _store_with_books(tmp_path, 100)
        adapter = _FakeAdapter([f"b{i:03d}" for i in range(98)])

        result = cleanup_orphans(
            _FakeRAG(store), tmp_path, dry_run=True, adapter=adapter
        )

        assert result['orphans_found'] == 2
        assert result['orphans_removed'] == 0
        assert 'backup_path' not in result
        assert store.get_by_book_id("b099", limit=1)


class TestWatchdogCleanupIsBounded:
    """Both watchdog scanners route through ``_cleanup_orphaned_books``; the
    empty-snapshot check they already had stays, the bound is added to it."""

    def test_oversized_orphan_set_is_refused(self, tmp_path):
        from src.archilles.watchdog import _cleanup_orphaned_books

        store = _store_with_books(tmp_path, 100)
        orphans = [f"b{i:03d}" for i in range(90)]
        results = {'errors': []}

        _cleanup_orphaned_books(
            str(store.db_path), orphans, False, results, indexed_count=100
        )

        assert results['orphans_removed'] == 0
        assert results['orphan_cleanup_refused'] is True
        assert _reopen(store).get_by_book_id("b000", limit=1)

    def test_ordinary_deletion_proceeds_and_is_backed_up(self, tmp_path):
        from src.archilles.watchdog import _cleanup_orphaned_books

        store = _store_with_books(tmp_path, 100, chunks_each=3)
        results = {'errors': []}

        _cleanup_orphaned_books(
            str(store.db_path), ["b000", "b001"], False, results, indexed_count=100
        )

        assert results['orphans_removed'] == 2
        assert _reopen(store).get_by_book_id("b000", limit=1) == []
        assert pq.read_table(Path(results['orphan_backup_path'])).num_rows == 6

    def test_explicit_flag_lets_the_deliberate_deletion_through(self, tmp_path):
        from src.archilles.watchdog import _cleanup_orphaned_books

        store = _store_with_books(tmp_path, 100)
        orphans = [f"b{i:03d}" for i in range(90)]
        results = {'errors': []}

        _cleanup_orphaned_books(
            str(store.db_path), orphans, False, results,
            indexed_count=100, allow_large=True,
        )

        assert results['orphans_removed'] == 90

    def test_dry_run_reports_the_bound_without_deleting(self, tmp_path):
        from src.archilles.watchdog import _cleanup_orphaned_books

        store = _store_with_books(tmp_path, 100)
        orphans = [f"b{i:03d}" for i in range(90)]
        results = {'errors': []}

        _cleanup_orphaned_books(
            str(store.db_path), orphans, True, results, indexed_count=100
        )

        assert results['orphans_removed'] == 0
        assert _reopen(store).get_by_book_id("b000", limit=1)


# ── 1.10(c): the deletion appears in the report someone actually reads ──

class TestWeeklyMailReportsDeletions:
    """``orphans_removed`` reached ``routine_history.jsonl`` but never the
    weekly mail — the one report in which a wrong cleanup would surface."""

    def _rows(self, orphans_removed):
        return [{
            "timestamp": "2026-09-04T09:00:00+02:00",
            "exit_code": 0,
            "duration_s": 12,
            "stats": {"new_books": 1, "orphans_removed": orphans_removed},
        }]

    def test_zero_is_still_reported(self):
        from scripts.weekly_status_mail import _format_source_block

        block = _format_source_block("lab", "folder", Path("D:/lib"), self._rows(0))
        assert "Waisen" in block and ": 0" in block

    def test_deletion_is_reported_with_the_rollback_location(self):
        from scripts.weekly_status_mail import _format_source_block

        block = _format_source_block("lab", "folder", Path("D:/lib"), self._rows(3))
        assert "Waisen): 3" in block
        assert "orphans_*.parquet" in block

    def test_large_deletion_is_flagged(self):
        from scripts.weekly_status_mail import _format_source_block

        block = _format_source_block("lab", "folder", Path("D:/lib"),
                                     self._rows(ORPHAN_COUNT_LIMIT + 1))
        assert "⚠" in block

    def test_ordinary_deletion_is_not_flagged(self):
        from scripts.weekly_status_mail import _format_source_block

        block = _format_source_block("lab", "folder", Path("D:/lib"), self._rows(2))
        assert "⚠" not in block

    def test_calibre_block_reports_it_too(self):
        from scripts.weekly_status_mail import _format_source_block

        block = _format_source_block("calibre", "calibre", Path("D:/lib"), self._rows(4))
        assert "Waisen): 4" in block
