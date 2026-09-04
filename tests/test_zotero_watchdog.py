"""Tests for the ZoteroWatchdogScanner and related helpers.

Covers:
  1. _zotero_metadata_for_scan() — batch SQLite read
  2. _compute_zotero_metadata_hash() — hash stability
  3. ZoteroWatchdogScanner.scan() — classification (new / metadata_changed /
     annotations_changed / unchanged / excluded-tag / no-attachment)
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.archilles.watchdog import (
    ZoteroWatchdogScanner,
    _compute_zotero_metadata_hash,
    _zotero_metadata_for_scan,
)
from src.adapters.zotero_adapter import ZoteroAdapter


# ── Fixture helpers ──────────────────────────────────────────────


def _build_zotero_db(library_path: Path, items: list[dict], *, att_content_type: str = "application/pdf") -> None:
    """Create a minimal zotero.sqlite for watchdog tests."""
    db_path = library_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        INSERT INTO itemTypes VALUES (1, 'annotation');
        INSERT INTO itemTypes VALUES (3, 'attachment');
        INSERT INTO itemTypes VALUES (7, 'book');
        INSERT INTO itemTypes VALUES (12, 'journalArticle');
        INSERT INTO itemTypes VALUES (27, 'note');

        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
            libraryID INTEGER DEFAULT 1, key TEXT UNIQUE,
            dateAdded TEXT, dateModified TEXT
        );
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
        CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        INSERT INTO fields VALUES (1, 'title');
        INSERT INTO fields VALUES (2, 'abstractNote');
        INSERT INTO fields VALUES (3, 'date');
        CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER, PRIMARY KEY (itemID, fieldID));
        CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
        INSERT INTO creatorTypes VALUES (1, 'author');
        CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT NOT NULL);
        CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER DEFAULT 0);
        CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER, type INTEGER DEFAULT 0);
        CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, linkMode INTEGER, contentType TEXT, path TEXT);
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INTEGER, libraryID INTEGER, key TEXT);
        CREATE TABLE collectionItems (collectionID INTEGER, itemID INTEGER);
    """)

    val_id = [0]

    def _set_field(item_id, field_id, value):
        val_id[0] += 1
        conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (val_id[0], value))
        conn.execute("INSERT INTO itemData VALUES (?, ?, ?)", (item_id, field_id, val_id[0]))

    cr_id = [0]
    tag_id = [0]
    att_id = [1000]
    coll_ids: dict[str, int] = {}

    for item in items:
        conn.execute(
            "INSERT INTO items VALUES (?, ?, 1, ?, ?, ?)",
            (item["itemID"], item.get("itemTypeID", 7), item["key"],
             item.get("dateAdded", "2025-01-01T00:00:00"),
             item.get("dateModified", "2025-06-01T00:00:00")),
        )
        if item.get("deleted"):
            conn.execute("INSERT INTO deletedItems VALUES (?)", (item["itemID"],))
        if item.get("title"):
            _set_field(item["itemID"], 1, item["title"])
        if item.get("abstract"):
            _set_field(item["itemID"], 2, item["abstract"])
        if item.get("date"):
            _set_field(item["itemID"], 3, item["date"])

        for first, last in item.get("authors", []):
            cr_id[0] += 1
            conn.execute("INSERT INTO creators VALUES (?, ?, ?)", (cr_id[0], first, last))
            conn.execute("INSERT INTO itemCreators VALUES (?, ?, 1, ?)", (item["itemID"], cr_id[0], cr_id[0]))

        for tag in item.get("tags", []):
            tag_id[0] += 1
            conn.execute("INSERT OR IGNORE INTO tags VALUES (?, ?)", (tag_id[0], tag))
            conn.execute("INSERT INTO itemTags (itemID, tagID, type) VALUES (?, ?, 0)", (item["itemID"], tag_id[0]))

        for coll in item.get("collections", []):
            if coll not in coll_ids:
                coll_ids[coll] = len(coll_ids) + 1
                conn.execute(
                    "INSERT INTO collections VALUES (?, ?, NULL, 1, ?)",
                    (coll_ids[coll], coll, f"COLL{coll_ids[coll]:04d}"),
                )
            conn.execute("INSERT INTO collectionItems VALUES (?, ?)", (coll_ids[coll], item["itemID"]))

        if item.get("has_attachment", True) and not item.get("deleted"):
            att_id[0] += 1
            att_modified = item.get("att_modified", item.get("dateModified", "2025-06-01T00:00:00"))
            conn.execute(
                "INSERT INTO items VALUES (?, 3, 1, ?, ?, ?)",
                (att_id[0], f"ATT{att_id[0]:04d}", item.get("dateAdded"), att_modified),
            )
            conn.execute(
                "INSERT INTO itemAttachments VALUES (?, ?, 0, ?, 'storage:file.pdf')",
                (att_id[0], item["itemID"], att_content_type),
            )

    conn.commit()
    conn.close()


def _make_scanner(library_path: Path, tmp_path: Path) -> ZoteroWatchdogScanner:
    archilles_dir = tmp_path / ".archilles"
    archilles_dir.mkdir()
    return ZoteroWatchdogScanner(
        library_path=library_path,
        db_path=str(tmp_path / "rag_db"),
        archilles_dir=archilles_dir,
    )


def _mock_hashes(stored: dict[str, dict]) -> MagicMock:
    """Return a mock rag with get_hashes_by_book_id returning stored."""
    rag = MagicMock()
    rag.store.get_hashes_by_book_id.return_value = stored
    return rag


# ── Tests: _zotero_metadata_for_scan ────────────────────────────


class TestZoteroMetadataForScan:
    def test_basic_fields(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "ABC001", "title": "Test Book",
             "authors": [("Jane", "Doe")], "tags": ["History"],
             "date": "2020", "abstract": "A summary.",
             "dateModified": "2025-06-01T00:00:00"},
        ])
        data = _zotero_metadata_for_scan(tmp_path)
        assert "ABC001" in data
        item = data["ABC001"]
        assert item["title"] == "Test Book"
        assert item["authors"] == ["Jane Doe"]
        assert item["tags"] == ["History"]
        assert item["date"] == "2020"
        assert item["abstract"] == "A summary."
        assert item["has_attachment"] is True

    def test_authors_sorted(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "ABC001", "title": "T",
             "authors": [("Bob", "Zed"), ("Alice", "Apple")]},
        ])
        item = _zotero_metadata_for_scan(tmp_path)["ABC001"]
        assert item["authors"] == ["Alice Apple", "Bob Zed"]

    def test_tags_sorted(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "ABC001", "title": "T",
             "tags": ["Zotero", "History", "Ancient"]},
        ])
        item = _zotero_metadata_for_scan(tmp_path)["ABC001"]
        assert item["tags"] == ["Ancient", "History", "Zotero"]

    def test_deleted_items_excluded(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "LIVE001", "title": "Live"},
            {"itemID": 2, "key": "DEAD001", "title": "Deleted", "deleted": True},
        ])
        data = _zotero_metadata_for_scan(tmp_path)
        assert "LIVE001" in data
        assert "DEAD001" not in data

    def test_no_attachment_flagged(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "NOATT1", "title": "No file", "has_attachment": False},
        ])
        item = _zotero_metadata_for_scan(tmp_path)["NOATT1"]
        assert item["has_attachment"] is False

    def test_attachment_modified_at_captured(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "ABC001", "title": "T",
             "att_modified": "2025-09-15T12:00:00"},
        ])
        item = _zotero_metadata_for_scan(tmp_path)["ABC001"]
        assert item["attachment_modified_at"] == "2025-09-15T12:00:00"

    def test_missing_db_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _zotero_metadata_for_scan(tmp_path)

    def test_collections_surfaced_and_sorted(self, tmp_path):
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "ABC001", "title": "T",
             "collections": ["Zeta Project", "Current Project"]},
        ])
        item = _zotero_metadata_for_scan(tmp_path)["ABC001"]
        assert item["collections"] == ["Current Project", "Zeta Project"]

    def test_no_collections_is_empty_list(self, tmp_path):
        _build_zotero_db(tmp_path, [{"itemID": 1, "key": "ABC001", "title": "T"}])
        item = _zotero_metadata_for_scan(tmp_path)["ABC001"]
        assert item["collections"] == []

    def test_collections_do_not_affect_metadata_hash(self, tmp_path):
        """Moving an item into a collection is not a content change, so the
        metadata hash must ignore collections (no false metadata_changed)."""
        _build_zotero_db(tmp_path, [
            {"itemID": 1, "key": "ABC001", "title": "T",
             "collections": ["Current Project"]},
        ])
        data = _zotero_metadata_for_scan(tmp_path)["ABC001"]
        h_with = _compute_zotero_metadata_hash(data)
        h_without = _compute_zotero_metadata_hash(
            {k: v for k, v in data.items() if k != "collections"})
        assert h_with == h_without


# ── Tests: _compute_zotero_metadata_hash ────────────────────────


class TestComputeZoteroMetadataHash:
    def _data(self, **kw):
        base = {"title": "T", "authors": ["A", "B"], "tags": ["x"], "abstract": "abs", "date": "2020"}
        base.update(kw)
        return base

    def test_identical_data_same_hash(self):
        d = self._data()
        assert _compute_zotero_metadata_hash(d) == _compute_zotero_metadata_hash(d)

    def test_title_change_changes_hash(self):
        a = _compute_zotero_metadata_hash(self._data(title="A"))
        b = _compute_zotero_metadata_hash(self._data(title="B"))
        assert a != b

    def test_author_change_changes_hash(self):
        a = _compute_zotero_metadata_hash(self._data(authors=["Alice"]))
        b = _compute_zotero_metadata_hash(self._data(authors=["Bob"]))
        assert a != b

    def test_tag_change_changes_hash(self):
        a = _compute_zotero_metadata_hash(self._data(tags=["old"]))
        b = _compute_zotero_metadata_hash(self._data(tags=["new"]))
        assert a != b

    def test_extra_fields_ignored(self):
        base = _compute_zotero_metadata_hash(self._data())
        with_extra = _compute_zotero_metadata_hash({**self._data(), "has_attachment": True, "modified_at": "now"})
        assert base == with_extra


# ── Tests: ZoteroWatchdogScanner.scan() ─────────────────────────


class TestZoteroWatchdogScan:

    def _scan(self, library_path, tmp_path, stored_hashes: dict, ann_cache: dict | None = None):
        """Run a dry-run scan with mocked LanceDB hashes."""
        scanner = _make_scanner(library_path, tmp_path)
        if ann_cache is not None:
            scanner.annotation_cache_file.write_text(json.dumps(ann_cache))

        with patch.object(scanner, '_load_indexed_hashes', return_value=stored_hashes):
            return scanner.scan(dry_run=True)

    def test_new_item_detected(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [{"itemID": 1, "key": "NEW001", "title": "New"}])
        results = self._scan(lib, tmp_path, stored_hashes={})
        doc_ids = [b['doc_id'] for b in results['new_books']]
        assert "NEW001" in doc_ids

    def test_unchanged_item(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "SAME01", "title": "Same", "authors": [("A", "B")],
             "tags": [], "abstract": "", "date": "2020"},
        ])
        items = _zotero_metadata_for_scan(lib)
        h = _compute_zotero_metadata_hash(items["SAME01"])
        results = self._scan(lib, tmp_path, stored_hashes={"SAME01": {"metadata_hash": h, "annotation_hash": ""}})
        assert "SAME01" in results['unchanged']
        assert results['metadata_changed'] == []

    def test_metadata_change_detected(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "CHG01", "title": "New Title"},
        ])
        results = self._scan(lib, tmp_path, stored_hashes={
            "CHG01": {"metadata_hash": "old_hash_that_will_not_match", "annotation_hash": ""}
        })
        assert "CHG01" in results['metadata_changed']

    def test_annotation_change_detected(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "ANN01", "title": "T", "att_modified": "2025-09-20T00:00:00"},
        ])
        items = _zotero_metadata_for_scan(lib)
        h = _compute_zotero_metadata_hash(items["ANN01"])
        # Cache shows old att_modified → annotation change
        old_cache = {"ANN01": "2025-01-01T00:00:00"}
        results = self._scan(lib, tmp_path,
                             stored_hashes={"ANN01": {"metadata_hash": h, "annotation_hash": ""}},
                             ann_cache=old_cache)
        assert "ANN01" in results['annotations_changed']
        assert "ANN01" not in results['metadata_changed']

    def test_excluded_tag_skipped(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "EXCL1", "title": "Excluded", "tags": ["exclude"]},
        ])
        scanner = _make_scanner(lib, tmp_path)
        scanner.excluded_tags_lower = {"exclude"}
        with patch.object(scanner, '_load_indexed_hashes', return_value={}):
            results = scanner.scan(dry_run=True)
        doc_ids = [b['doc_id'] for b in results['new_books']]
        assert "EXCL1" not in doc_ids

    def test_item_without_attachment_skipped(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "NOATT1", "title": "No file", "has_attachment": False},
        ])
        results = self._scan(lib, tmp_path, stored_hashes={})
        doc_ids = [b['doc_id'] for b in results['new_books']]
        assert "NOATT1" not in doc_ids

    def test_first_scan_seeds_annotation_cache(self, tmp_path):
        """On first scan of a known item (no cache entry), cache is seeded without marking changed."""
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "SEED1", "title": "T", "att_modified": "2025-06-01T00:00:00"},
        ])
        items = _zotero_metadata_for_scan(lib)
        h = _compute_zotero_metadata_hash(items["SEED1"])
        # Known to LanceDB, but no annotation cache entry yet
        results = self._scan(lib, tmp_path,
                             stored_hashes={"SEED1": {"metadata_hash": h, "annotation_hash": ""}},
                             ann_cache={})
        assert "SEED1" not in results['annotations_changed']
        assert "SEED1" in results['unchanged']


# ── index_new: new items are indexed, capped and ordered newest-first ──


class TestZoteroIndexNew:
    """The routine passes --index-new for Zotero; scan() must index new items,
    cap the run at max_new, and process the newest (highest item_id) first."""

    def _run(self, tmp_path, monkeypatch, *, max_new=None):
        lib = tmp_path / "lib"
        lib.mkdir()
        # Three new items; item_id encodes add order (30 = newest).
        _build_zotero_db(lib, [
            {"itemID": 10, "key": "key10", "title": "Oldest"},
            {"itemID": 20, "key": "key20", "title": "Middle"},
            {"itemID": 30, "key": "key30", "title": "Newest"},
        ])
        scanner = _make_scanner(lib, tmp_path)
        scanner._load_indexed_hashes = lambda: {}  # everything is new

        # Local plan: embed_local True → no pending_external marking.
        plan = MagicMock()
        plan.embed_local = True
        plan.mode = "balanced"
        scanner._resolve_plan = lambda: plan

        indexed_calls: list[dict] = []

        class RecordingRAG:
            def index_book(self, path, key, force=False):
                indexed_calls.append({"key": key})
                return {}

        scanner._load_rag = lambda: RecordingRAG()
        monkeypatch.setattr(ZoteroAdapter, "get_file_path", lambda self, key: Path("f.pdf"))

        results = scanner.scan(index_new=True, queue_new=False, max_new=max_new)
        return results, indexed_calls

    def test_index_new_caps_and_orders_newest_first(self, tmp_path, monkeypatch):
        results, indexed_calls = self._run(tmp_path, monkeypatch, max_new=2)
        assert results['new_indexed'] == 2
        # Newest two by item_id (30, 20); oldest (10) skipped this run.
        assert [c['key'] for c in indexed_calls] == ["key30", "key20"]

    def test_index_new_without_cap_indexes_all_newest_first(self, tmp_path, monkeypatch):
        results, indexed_calls = self._run(tmp_path, monkeypatch, max_new=None)
        assert results['new_indexed'] == 3
        assert [c['key'] for c in indexed_calls] == ["key30", "key20", "key10"]


# ── DB-lock hardening: a running Zotero holds zotero.sqlite ──


class TestZoteroDbLockHardening:
    """When the Zotero app is open it holds a lock on zotero.sqlite; the
    initial metadata read then raises sqlite3.OperationalError. A locked DB is
    transient — the scan must skip cleanly (db_locked, exit 0, no traceback)
    rather than crash. A non-lock OperationalError is a real error."""

    def _scanner(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        scanner = _make_scanner(lib, tmp_path)
        plan = MagicMock()
        plan.embed_local = True
        plan.mode = "balanced"
        scanner._resolve_plan = lambda: plan
        return scanner

    def test_locked_db_skips_cleanly(self, tmp_path, monkeypatch):
        scanner = self._scanner(tmp_path)
        monkeypatch.setattr(
            "src.archilles.watchdog._zotero_metadata_for_scan",
            lambda _lib: (_ for _ in ()).throw(
                sqlite3.OperationalError("database is locked")),
        )
        results = scanner.scan(dry_run=True)
        assert results.get("db_locked") is True
        assert results["errors"] == []          # transient → not an error (exit 0)
        assert results["scanned"] == 0

    def test_non_lock_operational_error_is_reported(self, tmp_path, monkeypatch):
        scanner = self._scanner(tmp_path)
        monkeypatch.setattr(
            "src.archilles.watchdog._zotero_metadata_for_scan",
            lambda _lib: (_ for _ in ()).throw(
                sqlite3.OperationalError("disk I/O error")),
        )
        results = scanner.scan(dry_run=True)
        assert results.get("db_locked") is not True
        assert results["errors"]                 # real failure → surfaced (exit 1)


# ── _zotero_priority_key: Variante A (explicit priority beats recency) ──


class TestZoteroPriorityKey:
    def test_tag_beats_newer_untagged(self, tmp_path):
        from src.archilles.watchdog import _zotero_priority_key
        items = {
            "old_tagged":   {"item_id": 10, "authors": [], "tags": ["Prio"], "title": "", "collections": []},
            "new_untagged": {"item_id": 99, "authors": [], "tags": [],       "title": "", "collections": []},
        }
        entries = [{"doc_id": "new_untagged"}, {"doc_id": "old_tagged"}]
        ordered = sorted(entries, key=lambda e: _zotero_priority_key(
            e, items, [], ["prio"], [], []))
        # Old-but-tagged item first (group 0), then newer untagged (group 1)
        assert [e["doc_id"] for e in ordered] == ["old_tagged", "new_untagged"]

    def test_collection_match_is_group_zero(self, tmp_path):
        from src.archilles.watchdog import _zotero_priority_key
        items = {"c": {"item_id": 5, "authors": [], "tags": [], "title": "",
                       "collections": ["Current Project"]}}
        key = _zotero_priority_key({"doc_id": "c"}, items, [], [], [], ["current project"])
        assert key[0] == 0  # group 0 = priority

    def test_author_substring_and_recency_within_group(self, tmp_path):
        from src.archilles.watchdog import _zotero_priority_key
        items = {
            "a_old": {"item_id": 1, "authors": ["Hannah Arendt"], "tags": [], "title": "", "collections": []},
            "a_new": {"item_id": 9, "authors": ["Hannah Arendt"], "tags": [], "title": "", "collections": []},
        }
        entries = [{"doc_id": "a_old"}, {"doc_id": "a_new"}]
        ordered = sorted(entries, key=lambda e: _zotero_priority_key(
            e, items, ["arendt"], [], [], []))
        # Both priority (author match) → newest first within the group
        assert [e["doc_id"] for e in ordered] == ["a_new", "a_old"]


# ── Finding 4.3: annotation cache commits per successful Phase-2 update ──


class TestZoteroAnnotationCacheCommit:
    """The att_modified cache must advance only AFTER a successful re-index, so
    a failed or interrupted delta re-detects the item on the next scan instead
    of silently swallowing the annotation update (the Calibre scanner is immune
    because it compares against the hash stored in LanceDB)."""

    OLD = "2025-06-01T00:00:00"
    NEW = "2025-06-02T00:00:00"

    def _prep(self, tmp_path, keys):
        lib = tmp_path / "lib"
        lib.mkdir()
        items = [{"itemID": i + 1, "key": k, "title": "T", "att_modified": self.NEW}
                 for i, k in enumerate(keys)]
        _build_zotero_db(lib, items)
        scanner = _make_scanner(lib, tmp_path)
        scan_items = _zotero_metadata_for_scan(lib)
        stored = {
            k: {"metadata_hash": _compute_zotero_metadata_hash(v), "annotation_hash": ""}
            for k, v in scan_items.items()
        }
        scanner._load_indexed_hashes = lambda: stored
        # Older cached att_mod → each item is an annotation change.
        scanner._annotation_cache = {k: self.OLD for k in keys}
        return scanner

    def test_failed_delta_is_redetected_next_scan(self, tmp_path, monkeypatch):
        scanner = self._prep(tmp_path, ["AKEY"])
        monkeypatch.setattr(ZoteroAdapter, "get_file_path", lambda self, key: Path("f.pdf"))

        class RaisingRAG:
            def index_book(self, path, key, force=False):
                raise RuntimeError("boom")

        scanner._load_rag = lambda: RaisingRAG()

        r1 = scanner.scan(dry_run=False, queue_new=False)
        assert "AKEY" in r1["annotations_changed"]
        assert r1["errors"]
        # Cache must NOT have advanced past the failure.
        assert scanner._annotation_cache["AKEY"] == self.OLD

        r2 = scanner.scan(dry_run=False, queue_new=False)
        assert "AKEY" in r2["annotations_changed"]

    def test_successful_update_is_unchanged_next_scan(self, tmp_path, monkeypatch):
        scanner = self._prep(tmp_path, ["AKEY"])
        monkeypatch.setattr(ZoteroAdapter, "get_file_path", lambda self, key: Path("f.pdf"))

        class OkRAG:
            def index_book(self, path, key, force=False):
                return {"status": None}

        scanner._load_rag = lambda: OkRAG()

        r1 = scanner.scan(dry_run=False, queue_new=False)
        assert "AKEY" in r1["annotations_changed"]
        assert scanner._annotation_cache["AKEY"] == self.NEW  # committed

        r2 = scanner.scan(dry_run=False, queue_new=False)
        assert "AKEY" in r2["unchanged"]
        assert "AKEY" not in r2["annotations_changed"]

    def test_shutdown_after_first_item_redetects_second(self, tmp_path, monkeypatch):
        scanner = self._prep(tmp_path, ["AKEY", "BKEY"])
        monkeypatch.setattr(ZoteroAdapter, "get_file_path", lambda self, key: Path("f.pdf"))

        class ShutdownAfterFirst:
            def __init__(self, sc):
                self._sc = sc

            def index_book(self, path, key, force=False):
                # Processed one item, then request shutdown so the loop breaks
                # before the second (checked at the top of the next iteration).
                self._sc._shutdown_requested = True
                return {"status": None}

        scanner._load_rag = lambda: ShutdownAfterFirst(scanner)

        r1 = scanner.scan(dry_run=False, queue_new=False)
        assert r1["delta_updates"] == 1                       # only AKEY processed
        assert scanner._annotation_cache["AKEY"] == self.NEW  # committed
        assert scanner._annotation_cache["BKEY"] == self.OLD  # not reached

        # Next scan runs cleanly and must re-detect the un-committed BKEY.
        scanner._shutdown_requested = False
        scanner._load_rag = lambda: type("OK", (), {
            "index_book": lambda self, path, key, force=False: {"status": None}
        })()
        r2 = scanner.scan(dry_run=False, queue_new=False)
        assert "BKEY" in r2["annotations_changed"]
        assert "AKEY" in r2["unchanged"]


# ---------------------------------------------------------------------------
# Constructor default excluded_tags aligned with the Calibre scanner (4.6)
# ---------------------------------------------------------------------------


class TestExcludedTagsDefault:
    def test_defaults_to_DEFAULT_EXCLUDED_TAGS_not_empty(self, tmp_path):
        """Calibre's WatchdogScanner falls back to DEFAULT_EXCLUDED_TAGS when
        excluded_tags isn't passed; the Zotero scanner fell back to [] —
        any direct/API instantiation without an explicit list silently
        indexed excluded items. Both scanners must agree by default."""
        from src.archilles.config import DEFAULT_EXCLUDED_TAGS

        library_path = tmp_path / "zotero_lib"
        library_path.mkdir()
        scanner = _make_scanner(library_path, tmp_path)

        assert scanner.excluded_tags_lower == {t.lower() for t in DEFAULT_EXCLUDED_TAGS}

    def test_explicit_empty_list_still_disables_exclusion(self, tmp_path):
        # Explicit [] (as opposed to omitting the argument) must still mean
        # "exclude nothing" — the CLI's --include-excluded path relies on this.
        archilles_dir = tmp_path / ".archilles"
        archilles_dir.mkdir()
        scanner = ZoteroWatchdogScanner(
            library_path=tmp_path / "zotero_lib",
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=archilles_dir,
            excluded_tags=[],
        )

        assert scanner.excluded_tags_lower == set()


# ── Orphan cleanup: indexed items deleted from Zotero ────────────


class TestZoteroOrphanCleanup:
    """Scheduled scans must remove index entries for items that vanished
    from Zotero (deleted outright or moved to the trash)."""

    def test_missing_item_reported_in_dry_run(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [{"itemID": 1, "key": "KEEP01", "title": "Keep"}])
        scanner = _make_scanner(lib, tmp_path)
        stored = {
            "KEEP01": {"metadata_hash": "", "annotation_hash": ""},
            "GONE01": {"metadata_hash": "h", "annotation_hash": ""},
        }
        with patch.object(scanner, '_load_indexed_hashes', return_value=stored):
            results = scanner.scan(dry_run=True)

        assert results['orphans_found'] == ["GONE01"]
        assert results['orphans_removed'] == 0

    def test_trashed_item_is_an_orphan(self, tmp_path):
        """deletedItems (Zotero trash) are excluded from the scan snapshot,
        so a trashed indexed item counts as deleted."""
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [
            {"itemID": 1, "key": "KEEP01", "title": "Keep"},
            {"itemID": 2, "key": "TRASH1", "title": "Trashed", "deleted": True},
        ])
        scanner = _make_scanner(lib, tmp_path)
        stored = {
            "KEEP01": {"metadata_hash": "", "annotation_hash": ""},
            "TRASH1": {"metadata_hash": "h", "annotation_hash": ""},
        }
        with patch.object(scanner, '_load_indexed_hashes', return_value=stored):
            results = scanner.scan(dry_run=True)

        assert results['orphans_found'] == ["TRASH1"]

    def test_missing_item_removed_from_index(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [{"itemID": 1, "key": "KEEP01", "title": "Keep"}])
        scanner = _make_scanner(lib, tmp_path)
        stored = {
            "KEEP01": {"metadata_hash": "", "annotation_hash": ""},
            "GONE01": {"metadata_hash": "h", "annotation_hash": ""},
        }
        with patch.object(scanner, '_load_indexed_hashes', return_value=stored), \
             patch("src.storage.lancedb_store.LanceDBStore") as mock_store_cls, \
             patch("src.archilles.watchdog.backup_orphan_chunks") as mock_backup:
            mock_store_cls.return_value.delete_by_book_id.return_value = 3
            mock_backup.return_value = tmp_path / "backups" / "orphans.parquet"
            results = scanner.scan(dry_run=False, queue_new=False)

        mock_store_cls.return_value.delete_by_book_id.assert_called_once_with("GONE01")
        mock_backup.assert_called_once()
        assert results['orphans_removed'] == 1
        assert not results['errors']

    def test_empty_snapshot_skips_cleanup(self, tmp_path):
        """Zero scanned items must never wipe the index."""
        lib = tmp_path / "lib"
        lib.mkdir()
        _build_zotero_db(lib, [])
        scanner = _make_scanner(lib, tmp_path)
        stored = {"KEEP01": {"metadata_hash": "h", "annotation_hash": ""}}
        with patch.object(scanner, '_load_indexed_hashes', return_value=stored), \
             patch("src.storage.lancedb_store.LanceDBStore") as mock_store_cls:
            results = scanner.scan(dry_run=False, queue_new=False)

        mock_store_cls.return_value.delete_by_book_id.assert_not_called()
        assert results['orphans_found'] == []
        assert results['orphans_removed'] == 0
