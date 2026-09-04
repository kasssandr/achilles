"""An item that cannot be indexed must say so (finding 1.15).

The scanner and the indexer disagreed about what "has an attachment" means.
``_zotero_metadata_for_scan`` sets ``has_attachment`` from a row in
``itemAttachments`` — the row exists, so the item is queued. Phase 3 then asks
the filesystem, gets ``None`` from ``adapter.get_file_path`` and does::

    file_path = adapter.get_file_path(key)
    if not file_path:
        continue

No log line, no ``results['errors']`` entry, no counter. The item stays in
``new_books``, stays in the queue, and the run exits 0.

Live consequence: ``Rezensionen`` is indexed 4 of 201. All 297 linked
attachments resolve through ``linked_attachment_base``, which is not set for
the ``archilles-zotero`` source, so ``_resolve_attachment_path`` returns None —
199 items queued, reached, and silently dropped on every run for two months.
``watchdog.log`` contains no trace, because the "No file found" warning exists
only on the *delta* path.

Counting is not enough: the number alone would not have named the missing
config key. The adapter explains, the scanner aggregates.
"""

import sqlite3
from pathlib import Path

import pytest


def _build_library(path, *, link_mode, raw_path, with_file=False):
    """A Zotero library with exactly one item and one attachment."""
    db = path / "zotero.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
                            key TEXT, dateAdded TEXT, dateModified TEXT);
        CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY,
                                      parentItemID INTEGER, linkMode INTEGER,
                                      path TEXT, contentType TEXT);
        CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY,
                                parentItemID INTEGER, note TEXT, title TEXT);
        CREATE TABLE itemAnnotations (itemID INTEGER PRIMARY KEY,
                                      parentItemID INTEGER, type INTEGER,
                                      text TEXT, comment TEXT);
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
    """)
    conn.execute("INSERT INTO items VALUES (1, 2, 'ZKEY0001', '', '')")
    conn.execute("INSERT INTO items VALUES (2, 3, 'ATT00001', '', '')")
    conn.execute(
        "INSERT INTO itemAttachments VALUES (2, 1, ?, ?, 'application/pdf')",
        (link_mode, raw_path),
    )
    conn.commit()
    conn.close()
    (path / "storage").mkdir(exist_ok=True)
    return path


class TestAdapterExplainsWhyThereIsNoFile:
    """``get_file_path`` returning None is a fact; the reason is what a user
    can act on."""

    def test_missing_linked_base_names_the_config_key(self, tmp_path):
        """The live case: 199 review items, one unset config key."""
        from src.adapters.zotero_adapter import ZoteroAdapter

        lib = _build_library(tmp_path, link_mode=2,
                             raw_path="attachments:0042/review.pdf")
        adapter = ZoteroAdapter(lib)

        assert adapter.get_file_path("ZKEY0001") is None
        category, detail = adapter.describe_unresolved("ZKEY0001")

        assert "linked_attachment_base" in category
        assert "0042/review.pdf" in detail
        assert "0042" not in category, (
            "a path in the category makes every entry unique and the summary "
            "useless — measured on the live queue: 199 items, 199 groups of one"
        )

    def test_absolute_link_that_is_gone_names_the_path(self, tmp_path):
        from src.adapters.zotero_adapter import ZoteroAdapter

        lib = _build_library(tmp_path, link_mode=2,
                             raw_path=r"D:\Calibre-Bibliothek\gone\book.pdf")
        category, detail = ZoteroAdapter(lib).describe_unresolved("ZKEY0001")

        assert "does not exist" in category
        assert "gone" in detail and "book.pdf" in detail

    def test_linked_url_says_there_is_no_local_file(self, tmp_path):
        from src.adapters.zotero_adapter import ZoteroAdapter

        lib = _build_library(tmp_path, link_mode=3, raw_path="https://example.org")
        category, _detail = ZoteroAdapter(lib).describe_unresolved("ZKEY0001")

        assert category, "a linked URL must still be explained, not left blank"
        assert "url" in category.lower() or "no local file" in category.lower()

    def test_item_without_any_attachment(self, tmp_path):
        from src.adapters.zotero_adapter import ZoteroAdapter

        db = tmp_path / "zotero.sqlite"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
                                key TEXT, dateAdded TEXT, dateModified TEXT);
            CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY,
                                          parentItemID INTEGER, linkMode INTEGER,
                                          path TEXT, contentType TEXT);
            CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY,
                                    parentItemID INTEGER, note TEXT, title TEXT);
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        """)
        conn.execute("INSERT INTO items VALUES (1, 2, 'ZKEY0001', '', '')")
        conn.commit()
        conn.close()

        category, _detail = ZoteroAdapter(tmp_path).describe_unresolved("ZKEY0001")
        assert "no attachment" in category.lower()

    def test_unknown_item(self, tmp_path):
        from src.adapters.zotero_adapter import ZoteroAdapter

        lib = _build_library(tmp_path, link_mode=2, raw_path="attachments:x/y.pdf")
        category, _detail = ZoteroAdapter(lib).describe_unresolved("NOSUCHKEY")
        assert "not found" in category.lower()

    def test_base_adapter_returns_an_empty_explanation(self):
        """Adapters that cannot explain must not break the caller."""
        from src.adapters.base import SourceAdapter

        assert SourceAdapter.describe_unresolved(None, "any") == ("", "")


class TestScannerCountsAndReportsTheSkip:
    """The silent ``continue`` becomes a counted, explained skip."""

    def test_skip_is_recorded_with_its_reason(self):
        from src.archilles.watchdog import _record_unindexable

        results = {'skipped_no_file': []}

        class _A:
            def describe_unresolved(self, key):
                return "linked_attachment_base is not set", "0042/x.pdf"

        _record_unindexable(results, _A(), "ZKEY0001", "Some Review", "phase 3")

        assert len(results['skipped_no_file']) == 1
        entry = results['skipped_no_file'][0]
        assert entry['doc_id'] == "ZKEY0001"
        assert "linked_attachment_base" in entry['reason']
        assert entry['detail'] == "0042/x.pdf"

    def test_adapter_without_explanation_still_records(self):
        from src.archilles.watchdog import _record_unindexable

        results = {'skipped_no_file': []}
        _record_unindexable(results, object(), "K", "T", "phase 3")

        assert results['skipped_no_file'][0]['doc_id'] == "K"

    def test_an_explaining_failure_does_not_break_the_scan(self):
        from src.archilles.watchdog import _record_unindexable

        class _Broken:
            def describe_unresolved(self, key):
                raise RuntimeError("database is locked")

        results = {'skipped_no_file': []}
        _record_unindexable(results, _Broken(), "K", "T", "phase 3")

        assert len(results['skipped_no_file']) == 1


class TestSummaryGroupsTheReasons:
    """199 identical lines are noise; "199 items, all one config key" is a
    finding."""

    def test_reasons_are_grouped_and_counted(self):
        from src.archilles.watchdog import summarise_unindexable

        entries = [
            {'doc_id': 'A', 'reason': 'linked_attachment_base is not set',
             'detail': '0042/a.pdf'},
            {'doc_id': 'B', 'reason': 'linked_attachment_base is not set',
             'detail': '0043/b.pdf'},
            {'doc_id': 'C', 'reason': 'linked file does not exist',
             'detail': 'D:/x.pdf'},
        ]
        summary = summarise_unindexable(entries)

        assert "3 item(s)" in summary
        assert "2×" in summary and "1×" in summary
        assert summary.count("linked_attachment_base") == 1, (
            "one line per reason, not one per item"
        )

    def test_fifty_items_do_not_print_fifty_lines(self):
        """The point of grouping: the live queue holds 199 of one reason."""
        from src.archilles.watchdog import summarise_unindexable

        entries = [
            {'doc_id': f'K{i}', 'reason': 'same reason', 'detail': f'{i}/f.pdf'}
            for i in range(50)
        ]
        summary = summarise_unindexable(entries)

        assert "50×" in summary
        assert summary.count("e.g.") == 1
        assert len(summary.splitlines()) == 3

    def test_empty_input_yields_nothing(self):
        from src.archilles.watchdog import summarise_unindexable

        assert summarise_unindexable([]) == ""


class TestTheCounterReachesTheReports:
    def test_run_routine_collects_it(self):
        from scripts.run_routine import _parse_stats

        stdout = '{"scanned": 10, "new_books": [], "skipped_no_file": [1, 2, 3]}'
        stats = _parse_stats(stdout, "zotero")

        assert stats["skipped_no_file"] == 3

    def test_weekly_mail_shows_it(self):
        from scripts.weekly_status_mail import _format_source_block

        rows = [{
            "timestamp": "2026-09-04T09:00:00+02:00", "exit_code": 0,
            "duration_s": 5, "stats": {"skipped_no_file": 199},
        }]
        block = _format_source_block("zot", "zotero", Path("D:/Zotero"), rows)

        assert "199" in block
        assert "unindexierbar" in block.lower() or "übersprungen" in block.lower()

    def test_weekly_mail_stays_quiet_at_zero(self):
        from scripts.weekly_status_mail import _format_source_block

        rows = [{
            "timestamp": "2026-09-04T09:00:00+02:00", "exit_code": 0,
            "duration_s": 5, "stats": {"skipped_no_file": 0},
        }]
        block = _format_source_block("zot", "zotero", Path("D:/Zotero"), rows)

        assert "unindexierbar" not in block.lower()
