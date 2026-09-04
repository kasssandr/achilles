"""Tests for the WatchdogScanner core logic.

Exercises the three pieces that were previously untested:
  1. Hash equivalence with ``ArchillesRAG._compute_metadata_hash`` — the
     watchdog must produce the exact same hash as the indexer, otherwise
     every scan would show false-positive metadata changes.
  2. ``_calibre_metadata_for_hash`` SQL: author ordering, tag aggregation,
     HTML stripping of comments.
  3. ``WatchdogScanner.scan()`` classification (new / metadata_changed /
     annotations_changed / unchanged / excluded-tag) against an in-memory
     Calibre metadata.db and a mocked LanceDB hash store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src.archilles.watchdog import (
    DEFAULT_EXCLUDED_TAGS,
    WatchdogScanner,
    _calibre_metadata_for_hash,
    _clean_html,
    _compute_metadata_hash,
)


# ---------------------------------------------------------------------------
# Helpers: build a minimal Calibre metadata.db in a tmp directory
# ---------------------------------------------------------------------------

def _create_calibre_db(library_path: Path) -> None:
    """Create the Calibre SQLite tables the watchdog actually reads."""
    db = sqlite3.connect(str(library_path / "metadata.db"))
    db.executescript("""
        CREATE TABLE books (
            id INTEGER PRIMARY KEY,
            title TEXT,
            path TEXT
        );
        CREATE TABLE authors (
            id INTEGER PRIMARY KEY,
            name TEXT
        );
        CREATE TABLE books_authors_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book INTEGER,
            author INTEGER
        );
        CREATE TABLE comments (
            book INTEGER PRIMARY KEY,
            text TEXT
        );
        CREATE TABLE tags (
            id INTEGER PRIMARY KEY,
            name TEXT
        );
        CREATE TABLE books_tags_link (
            book INTEGER,
            tag INTEGER
        );
        CREATE TABLE publishers (
            id INTEGER PRIMARY KEY,
            name TEXT
        );
        CREATE TABLE books_publishers_link (
            book INTEGER,
            publisher INTEGER
        );
        CREATE TABLE ratings (
            id INTEGER PRIMARY KEY,
            rating INTEGER
        );
        CREATE TABLE books_ratings_link (
            book INTEGER,
            rating INTEGER
        );
    """)
    db.commit()
    db.close()


def _add_book(
    library_path: Path,
    calibre_id: int,
    title: str,
    authors: list[str] | None = None,
    tags: list[str] | None = None,
    comments_html: str | None = None,
    publisher: str | None = None,
    book_dir: str | None = None,
    with_file: str | None = None,
) -> None:
    """Insert a book row plus optional tags/authors/comments/publisher.

    If ``with_file`` is given (e.g. "book.epub"), a zero-byte file with that
    extension is created in the book directory so that ``_discover_formats``
    picks it up.
    """
    book_dir = book_dir or f"{(authors or ['Unknown'])[0]}/{title} ({calibre_id})"
    full_book_path = library_path / book_dir
    full_book_path.mkdir(parents=True, exist_ok=True)
    if with_file:
        (full_book_path / with_file).touch()

    db = sqlite3.connect(str(library_path / "metadata.db"))
    db.execute("INSERT INTO books (id, title, path) VALUES (?, ?, ?)",
               (calibre_id, title, book_dir))

    for name in (authors or []):
        row = db.execute("SELECT id FROM authors WHERE name = ?", (name,)).fetchone()
        if row:
            author_id = row[0]
        else:
            cur = db.execute("INSERT INTO authors (name) VALUES (?)", (name,))
            author_id = cur.lastrowid
        db.execute(
            "INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
            (calibre_id, author_id),
        )

    for name in (tags or []):
        row = db.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
        if row:
            tag_id = row[0]
        else:
            cur = db.execute("INSERT INTO tags (name) VALUES (?)", (name,))
            tag_id = cur.lastrowid
        db.execute(
            "INSERT INTO books_tags_link (book, tag) VALUES (?, ?)",
            (calibre_id, tag_id),
        )

    if comments_html is not None:
        db.execute(
            "INSERT INTO comments (book, text) VALUES (?, ?)",
            (calibre_id, comments_html),
        )

    if publisher:
        row = db.execute("SELECT id FROM publishers WHERE name = ?", (publisher,)).fetchone()
        if row:
            pub_id = row[0]
        else:
            cur = db.execute("INSERT INTO publishers (name) VALUES (?)", (publisher,))
            pub_id = cur.lastrowid
        db.execute(
            "INSERT INTO books_publishers_link (book, publisher) VALUES (?, ?)",
            (calibre_id, pub_id),
        )

    db.commit()
    db.close()


@pytest.fixture
def calibre_library(tmp_path: Path) -> Path:
    _create_calibre_db(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# _clean_html
# ---------------------------------------------------------------------------

class TestCleanHtml:
    def test_strips_tags(self):
        assert _clean_html("<p>Hello <b>world</b>.</p>") == "Hello world."

    def test_decodes_entities(self):
        assert _clean_html("a &amp; b &lt;c&gt;") == "a & b <c>"

    def test_collapses_whitespace(self):
        assert _clean_html("a\n\n  b\t c") == "a b c"

    def test_empty_string(self):
        assert _clean_html("") == ""
        assert _clean_html(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _compute_metadata_hash equivalence with ArchillesRAG
# ---------------------------------------------------------------------------

class TestMetadataHashEquivalence:
    """The watchdog must produce the same hash as the indexer — any drift
    would make every scan flag every book as ``metadata_changed``."""

    @staticmethod
    def _rag_hash(meta: dict) -> str:
        # Imported lazily so test collection doesn't require heavy RAG deps
        from src.archilles.engine import ArchillesRAG
        return ArchillesRAG._compute_metadata_hash(meta)

    def test_identical_input_yields_identical_hash(self):
        meta = {
            'title': 'De Bello Gallico',
            'author': 'Julius Caesar',
            'tags': ['History', 'Latin'],
            'comments': 'A commentary on the Gallic Wars.',
            'publisher': 'Loeb',
        }
        assert _compute_metadata_hash(meta) == self._rag_hash(meta)

    def test_tag_order_irrelevant(self):
        meta_a = {
            'title': 'X', 'author': 'Y', 'publisher': 'Z',
            'comments': '', 'tags': ['B', 'A', 'C'],
        }
        meta_b = {**meta_a, 'tags': ['C', 'A', 'B']}
        assert _compute_metadata_hash(meta_a) == _compute_metadata_hash(meta_b)
        assert _compute_metadata_hash(meta_a) == self._rag_hash(meta_a)

    def test_different_comment_changes_hash(self):
        base = {
            'title': 'X', 'author': 'Y', 'publisher': 'Z',
            'comments': 'Original.', 'tags': [],
        }
        changed = {**base, 'comments': 'Revised.'}
        assert _compute_metadata_hash(base) != _compute_metadata_hash(changed)

    def test_tags_as_comma_string_matches_list(self):
        """The watchdog path passes ``tags`` as list, but the helper also
        accepts a comma-string for robustness."""
        as_list = {'title': '', 'author': '', 'publisher': '',
                   'comments': '', 'tags': ['A', 'B']}
        as_str = {**as_list, 'tags': 'B, A'}
        assert _compute_metadata_hash(as_list) == _compute_metadata_hash(as_str)


# ---------------------------------------------------------------------------
# _calibre_metadata_for_hash
# ---------------------------------------------------------------------------

class TestCalibreMetadataForHash:
    def test_reads_single_book(self, calibre_library: Path):
        _add_book(
            calibre_library, 1, "Test Book",
            authors=["Alice"],
            tags=["History"],
            comments_html="<p>Desc.</p>",
            publisher="PubCo",
        )
        result = _calibre_metadata_for_hash(calibre_library)
        assert 1 in result
        book = result[1]
        assert book['title'] == "Test Book"
        assert book['author'] == "Alice"
        assert book['tags'] == ["History"]
        assert book['comments'] == "Desc."
        assert book['publisher'] == "PubCo"

    def test_multi_author_joined_in_link_order(self, calibre_library: Path):
        _add_book(calibre_library, 1, "Collab",
                  authors=["First Author", "Second Author", "Third Author"])
        result = _calibre_metadata_for_hash(calibre_library)
        # link-insertion order is preserved via ORDER BY bal2.id
        assert result[1]['author'] == "First Author & Second Author & Third Author"

    def test_tags_sorted(self, calibre_library: Path):
        _add_book(calibre_library, 1, "Tagged",
                  authors=["A"], tags=["Zulu", "Alpha", "Mike"])
        result = _calibre_metadata_for_hash(calibre_library)
        assert result[1]['tags'] == ["Alpha", "Mike", "Zulu"]

    def test_comments_html_stripped(self, calibre_library: Path):
        _add_book(calibre_library, 1, "X", authors=["A"],
                  comments_html="<b>Bold</b>&nbsp;text &amp; more.")
        assert _calibre_metadata_for_hash(calibre_library)[1]['comments'] == \
            "Bold text & more."

    def test_missing_comments_is_empty_string(self, calibre_library: Path):
        _add_book(calibre_library, 1, "X", authors=["A"])
        assert _calibre_metadata_for_hash(calibre_library)[1]['comments'] == ""

    def test_missing_db_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            _calibre_metadata_for_hash(tmp_path)


# ---------------------------------------------------------------------------
# WatchdogScanner.scan classification
# ---------------------------------------------------------------------------

class TestScannerClassification:
    """Drive the scanner with a real SQLite DB + mocked hash store."""

    @pytest.fixture
    def scanner_factory(self, tmp_path: Path):
        def _make(indexed_hashes: dict, excluded: list[str] | None = None):
            scanner = WatchdogScanner(
                library_path=tmp_path,
                db_path=str(tmp_path / ".archilles" / "rag_db"),
                archilles_dir=tmp_path / ".archilles",
                excluded_tags=excluded,
            )
            scanner._load_indexed_hashes = lambda: indexed_hashes
            scanner._annotation_changed = lambda file_path, stored_hash: False
            return scanner
        return _make

    def test_new_book_detected(self, calibre_library: Path, scanner_factory):
        _add_book(calibre_library, 42, "Fresh Book",
                  authors=["Alice"], with_file="book.epub")
        scanner = scanner_factory(indexed_hashes={})

        results = scanner.scan(dry_run=True)

        assert results['scanned'] == 1
        assert len(results['new_books']) == 1
        assert results['new_books'][0]['calibre_id'] == 42
        assert results['new_books'][0]['title'] == "Fresh Book"
        assert not results['metadata_changed']
        assert not results['unchanged']

    def test_metadata_changed_detected(self, calibre_library: Path, scanner_factory):
        _add_book(calibre_library, 1, "T", authors=["A"],
                  comments_html="Old comment", with_file="x.epub")

        # Pretend it was indexed with a different metadata hash
        scanner = scanner_factory(indexed_hashes={
            1: {'book_id': '1',
                'metadata_hash': 'stale-hash-from-old-metadata',
                'annotation_hash': ''}
        })

        results = scanner.scan(dry_run=True)
        assert 1 in results['metadata_changed']
        assert 1 not in results['unchanged']
        assert not results['new_books']

    def test_unchanged_when_hash_matches(self, calibre_library: Path, scanner_factory):
        _add_book(calibre_library, 7, "Stable", authors=["A"],
                  comments_html="x", with_file="x.epub")

        # Compute the current hash and feed it back as the stored hash
        current_hash = _compute_metadata_hash(
            _calibre_metadata_for_hash(calibre_library)[7]
        )
        scanner = scanner_factory(indexed_hashes={
            7: {'book_id': '7', 'metadata_hash': current_hash,
                'annotation_hash': ''}
        })

        results = scanner.scan(dry_run=True)
        assert 7 in results['unchanged']
        assert 7 not in results['metadata_changed']

    def test_missing_stored_hash_does_not_flag_change(
        self, calibre_library: Path, scanner_factory,
    ):
        """Books indexed before hash tracking have stored_hash=''. The
        scanner must NOT classify these as metadata_changed; they should
        remain in ``unchanged`` until a separate backfill populates hashes."""
        _add_book(calibre_library, 5, "Pre-hash", authors=["A"],
                  with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={
            5: {'book_id': '5', 'metadata_hash': '', 'annotation_hash': ''}
        })
        results = scanner.scan(dry_run=True)
        assert 5 in results['unchanged']
        assert 5 not in results['metadata_changed']

    def test_excluded_tag_skipped(self, calibre_library: Path, scanner_factory):
        _add_book(calibre_library, 1, "Skip Me",
                  authors=["A"], tags=["exclude"], with_file="x.epub")
        _add_book(calibre_library, 2, "Keep Me",
                  authors=["A"], tags=["History"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={})

        results = scanner.scan(dry_run=True)
        new_ids = {b['calibre_id'] for b in results['new_books']}
        assert new_ids == {2}
        assert 1 not in new_ids

    def test_custom_excluded_tag_from_config(
        self, calibre_library: Path, scanner_factory,
    ):
        """Users can extend exclusions by configuring custom tags (e.g.
        a language-specific ``Übersetzung`` or ``draft``). The scanner
        must honour whatever list the caller passes in."""
        _add_book(calibre_library, 1, "Die Ilias (Übersetzung)",
                  authors=["Homer"], tags=["Übersetzung"], with_file="x.epub")
        scanner = scanner_factory(
            indexed_hashes={},
            excluded=["exclude", "Übersetzung"],
        )
        results = scanner.scan(dry_run=True)
        assert not results['new_books']

    def test_excluded_tag_case_insensitive(self, calibre_library: Path, scanner_factory):
        _add_book(calibre_library, 1, "X", authors=["A"],
                  tags=["EXCLUDE"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={})
        results = scanner.scan(dry_run=True)
        assert not results['new_books']

    def test_include_excluded_via_empty_list(
        self, calibre_library: Path, scanner_factory,
    ):
        """Passing ``excluded_tags=[]`` (via ``--include-excluded``)
        must disable the filter."""
        _add_book(calibre_library, 1, "X", authors=["A"],
                  tags=["exclude"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={}, excluded=[])
        results = scanner.scan(dry_run=True)
        assert len(results['new_books']) == 1

    def test_book_without_readable_file_is_skipped(
        self, calibre_library: Path, scanner_factory,
    ):
        """Books whose directory contains no supported format are ignored."""
        _add_book(calibre_library, 1, "No File", authors=["A"],
                  with_file=None)  # no file on disk
        scanner = scanner_factory(indexed_hashes={})
        results = scanner.scan(dry_run=True)
        assert not results['new_books']

    def test_annotation_change_detected_bidirectionally(
        self, calibre_library: Path, scanner_factory,
    ):
        """An empty stored hash paired with a non-empty current hash must
        flag the book as annotations_changed — regression for the gate that
        previously skipped first-time annotations."""
        _add_book(calibre_library, 1, "X", authors=["A"], with_file="x.epub")

        current_hash = _compute_metadata_hash(
            _calibre_metadata_for_hash(calibre_library)[1]
        )
        scanner = scanner_factory(indexed_hashes={
            1: {'book_id': '1', 'metadata_hash': current_hash,
                'annotation_hash': ''}
        })
        # Simulate "first-time annotations appeared": current hash != stored ''
        scanner._annotation_changed = lambda file_path, stored_hash: True

        results = scanner.scan(dry_run=True)
        assert 1 in results['annotations_changed']


# ---------------------------------------------------------------------------
# Queue file handling
# ---------------------------------------------------------------------------

class TestQueueFile:
    def test_queue_new_creates_file(self, tmp_path: Path):
        scanner = WatchdogScanner(
            library_path=tmp_path,
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=tmp_path / ".archilles",
        )
        scanner._queue_new_books([3, 1, 2])
        assert scanner.queue_file.exists()
        assert json.loads(scanner.queue_file.read_text()) == [1, 2, 3]

    def test_queue_merges_and_deduplicates(self, tmp_path: Path):
        archilles_dir = tmp_path / ".archilles"
        archilles_dir.mkdir()
        (archilles_dir / "index_queue.json").write_text("[1, 2, 3]")

        scanner = WatchdogScanner(
            library_path=tmp_path,
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=archilles_dir,
        )
        scanner._queue_new_books([3, 4, 5])
        assert json.loads(scanner.queue_file.read_text()) == [1, 2, 3, 4, 5]

    def test_queue_handles_corrupt_file(self, tmp_path: Path):
        archilles_dir = tmp_path / ".archilles"
        archilles_dir.mkdir()
        (archilles_dir / "index_queue.json").write_text("not-json!!")

        scanner = WatchdogScanner(
            library_path=tmp_path,
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=archilles_dir,
        )
        scanner._queue_new_books([7])
        assert json.loads(scanner.queue_file.read_text()) == [7]


# ---------------------------------------------------------------------------
# Counter semantics (Issue #5 regression)
# ---------------------------------------------------------------------------

class TestCounters:
    def test_delta_updates_separate_from_new_indexed(
        self, calibre_library: Path, tmp_path: Path,
    ):
        """Phase-2 delta updates and Phase-3 new-book indexes must use
        distinct counters."""
        _add_book(calibre_library, 1, "Existing-Changed",
                  authors=["A"], with_file="x.epub")
        _add_book(calibre_library, 2, "Brand-New",
                  authors=["A"], with_file="x.epub")

        scanner = WatchdogScanner(
            library_path=calibre_library,
            db_path=str(calibre_library / ".archilles" / "rag_db"),
            archilles_dir=calibre_library / ".archilles",
        )
        scanner._load_indexed_hashes = lambda: {
            1: {'book_id': '1',
                'metadata_hash': 'stale', 'annotation_hash': ''},
            # book 2 not in index → new book
        }
        scanner._annotation_changed = lambda file_path, stored_hash: False

        class FakeRAG:
            def index_book(self, path, book_id, force=False):
                return None
        scanner._load_rag = lambda: FakeRAG()

        results = scanner.scan(dry_run=False, queue_new=False, index_new=True)
        assert results['delta_updates'] == 1      # book 1
        assert results['new_indexed'] == 1        # book 2
        assert 'new_indexed_time' in results


# ---------------------------------------------------------------------------
# Annotation cache (regression for the "scan opens every PDF every run" bug)
# ---------------------------------------------------------------------------

class TestAnnotationCache:
    """Cold path opens the book once; warm path reuses cached signature."""

    @pytest.fixture
    def scanner(self, tmp_path: Path):
        return WatchdogScanner(
            library_path=tmp_path,
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=tmp_path / ".archilles",
        )

    def test_first_call_opens_and_seeds_cache(self, tmp_path: Path, scanner):
        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": [{"text": "x"}]}
            mock_hash.return_value = "abc123"

            changed = scanner._annotation_changed(book, stored_hash="")

        assert mock_get.call_count == 1
        assert changed is True  # current 'abc123' != stored ''
        assert str(book) in scanner._annotation_cache
        assert scanner._annotation_cache[str(book)]["annotation_hash"] == "abc123"
        assert scanner._annotation_cache_dirty is True

    def test_warm_cache_skips_book_open(self, tmp_path: Path, scanner):
        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        # Cold call seeds cache
        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": []}
            mock_hash.return_value = "deadbeef"
            scanner._annotation_changed(book, stored_hash="deadbeef")
            assert mock_get.call_count == 1

        # Second call with unchanged mtime/size must NOT open the book
        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            changed = scanner._annotation_changed(book, stored_hash="deadbeef")
            assert mock_get.call_count == 0
            assert changed is False  # cached hash matches stored

    def test_warm_cache_detects_change_against_old_stored_hash(
        self, tmp_path: Path, scanner,
    ):
        """Cache hit but stored_hash is stale → still flag annotations_changed."""
        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": [{"text": "x"}]}
            mock_hash.return_value = "current-hash"
            scanner._annotation_changed(book, stored_hash="current-hash")

        # File hasn't changed but the index still has the *previous* hash
        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get:
            changed = scanner._annotation_changed(book, stored_hash="old-stored-hash")
            assert mock_get.call_count == 0  # cache hit
            assert changed is True  # cached 'current-hash' differs from old stored

    def test_changed_size_invalidates_cache(self, tmp_path: Path, scanner):
        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": []}
            mock_hash.return_value = "v1"
            scanner._annotation_changed(book, stored_hash="v1")

        # Rewrite the file with different content (forces new size)
        book.write_bytes(b"%PDF-1.4 fake plus more bytes here to change size")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": [{"text": "new"}]}
            mock_hash.return_value = "v2"
            changed = scanner._annotation_changed(book, stored_hash="v1")
            assert mock_get.call_count == 1  # reopened
            assert changed is True

    def test_cache_persists_across_scanner_instances(
        self, tmp_path: Path, scanner,
    ):
        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": []}
            mock_hash.return_value = "h"
            scanner._annotation_changed(book, stored_hash="h")
            scanner._save_annotation_cache()

        assert scanner.annotation_cache_file.exists()

        # Fresh scanner reads cache from disk
        scanner2 = WatchdogScanner(
            library_path=tmp_path,
            db_path=str(tmp_path / "rag_db"),
            archilles_dir=tmp_path / ".archilles",
        )
        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get:
            scanner2._annotation_changed(book, stored_hash="h")
            assert mock_get.call_count == 0  # cache hit — no PDF open

    def test_corrupt_cache_falls_back_to_fresh(self, tmp_path: Path, scanner):
        scanner.archilles_dir.mkdir(parents=True, exist_ok=True)
        scanner.annotation_cache_file.write_text("not-json!!", encoding='utf-8')

        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get, \
             patch("src.archilles.engine.core.ArchillesRAG._compute_annotation_hash") as mock_hash:
            mock_get.return_value = {"annotations": []}
            mock_hash.return_value = "x"
            scanner._annotation_changed(book, stored_hash="x")
            assert mock_get.call_count == 1
            assert scanner._annotation_cache == {str(book): {
                'sig': scanner._annotation_files_signature(book),
                'annotation_hash': 'x',
            }}

    def test_extraction_failure_does_not_poison_cache(self, tmp_path: Path, scanner):
        book = tmp_path / "book.pdf"
        book.write_bytes(b"%PDF-1.4 fake")

        with patch("src.calibre_mcp.annotations.get_combined_annotations") as mock_get:
            mock_get.side_effect = RuntimeError("PDF parser blew up")
            changed = scanner._annotation_changed(book, stored_hash="x")
            assert changed is False  # treat-as-unchanged on error
            # No cache entry written for the failure
            assert str(book) not in scanner._annotation_cache


# ---------------------------------------------------------------------------
# Two-phase indexing: phase1 stubs, max_new cap, fulltext checkpoint
# ---------------------------------------------------------------------------

class TestTwoPhaseIndexing:
    """The Calibre routine indexes in two phases:
      A — fast metadata stubs (has_content=False) for new books
      B — drain the fulltext backlog (turn stubs into full content)
    Phase 1 of scan() classifies stub-only books into results['fulltext_pending'];
    Phase 4 drains that backlog with its own checkpoint independent of Phase 3.
    """

    @staticmethod
    def _make_scanner(library_path: Path, indexed_hashes: dict) -> WatchdogScanner:
        scanner = WatchdogScanner(
            library_path=library_path,
            db_path=str(library_path / ".archilles" / "rag_db"),
            archilles_dir=library_path / ".archilles",
        )
        scanner._load_indexed_hashes = lambda: indexed_hashes
        scanner._annotation_changed = lambda file_path, stored_hash: False
        return scanner

    def test_phase1_stub_detected_as_fulltext_pending(
        self, calibre_library: Path,
    ):
        """A book with has_content=False is a phase1 stub and must land in
        results['fulltext_pending'], NOT in results['unchanged'] — otherwise
        Phase 4 would never see the backlog."""
        _add_book(calibre_library, 101, "Stub Book",
                  authors=["A"], with_file="x.epub")

        stored_meta = _compute_metadata_hash(
            _calibre_metadata_for_hash(calibre_library)[101]
        )
        scanner = self._make_scanner(calibre_library, indexed_hashes={
            101: {
                'book_id': '101',
                'metadata_hash': stored_meta,
                'annotation_hash': '',
                'has_content': False,
            }
        })

        results = scanner.scan(dry_run=True)
        pending_ids = {e['calibre_id'] for e in results['fulltext_pending']}
        assert 101 in pending_ids
        assert 101 not in results['unchanged']
        assert 101 not in results['new_books']

    def test_full_content_book_not_in_fulltext_pending(
        self, calibre_library: Path,
    ):
        """has_content=True means the book has real content chunks — it must
        NOT be classified as fulltext_pending even when metadata is unchanged."""
        _add_book(calibre_library, 102, "Full Book",
                  authors=["A"], with_file="x.epub")

        stored_meta = _compute_metadata_hash(
            _calibre_metadata_for_hash(calibre_library)[102]
        )
        scanner = self._make_scanner(calibre_library, indexed_hashes={
            102: {
                'book_id': '102',
                'metadata_hash': stored_meta,
                'annotation_hash': '',
                'has_content': True,
            }
        })

        results = scanner.scan(dry_run=True)
        assert not results['fulltext_pending']
        assert 102 in results['unchanged']

    def test_max_new_limits_phase3(self, calibre_library: Path):
        """max_new=2 must cap how many new books Phase 3 indexes per run."""
        for cid in (1, 2, 3, 4, 5):
            _add_book(calibre_library, cid, f"Book {cid}",
                      authors=["A"], with_file="x.epub")

        scanner = self._make_scanner(calibre_library, indexed_hashes={})

        indexed_calls: list[str] = []

        class FakeRAG:
            def index_book(self, path, book_id, force=False, phase=None):
                indexed_calls.append(book_id)
        scanner._load_rag = lambda: FakeRAG()

        results = scanner.scan(
            dry_run=False, queue_new=False, index_new=True, max_new=2,
        )

        assert len(results['new_books']) == 5  # all detected
        assert results['new_indexed'] == 2     # only 2 actually indexed
        assert len(indexed_calls) == 2

    def test_fulltext_checkpoint_resume(self, calibre_library: Path):
        """When the fulltext checkpoint contains already-done IDs, Phase 4
        must skip them and only process the remaining stubs."""
        for cid in (1, 2, 3):
            _add_book(calibre_library, cid, f"Stub {cid}",
                      authors=["A"], with_file="x.epub")

        meta_by_id = _calibre_metadata_for_hash(calibre_library)
        indexed_hashes = {
            cid: {
                'book_id': str(cid),
                'metadata_hash': _compute_metadata_hash(meta_by_id[cid]),
                'annotation_hash': '',
                'has_content': False,
            }
            for cid in (1, 2, 3)
        }
        scanner = self._make_scanner(calibre_library, indexed_hashes)

        # Pre-seed the checkpoint as if book 1 was already drained in a prior run
        from src.archilles.indexer import IndexingCheckpoint
        scanner.archilles_dir.mkdir(parents=True, exist_ok=True)
        seed = IndexingCheckpoint.create_new(
            scanner.fulltext_checkpoint_file, profile="", book_ids=["1", "2", "3"],
            phase="phase2",
        )
        seed.complete_book("1")

        indexed_calls: list[str] = []

        class FakeRAG:
            def index_book(self, path, book_id, force=False, phase=None):
                indexed_calls.append(book_id)
        scanner._load_rag = lambda: FakeRAG()

        results = scanner.scan(
            dry_run=False, queue_new=False, index_fulltext_pending=True,
        )

        assert sorted(indexed_calls) == ['2', '3']
        assert results['fulltext_indexed'] == 2
        # Clean run drains the checkpoint
        assert not scanner.fulltext_checkpoint_file.exists()

    def test_checkpoint_kept_on_interrupted_run_and_resumes(
        self, calibre_library: Path, tmp_path: Path,
    ):
        """After a graceful shutdown the checkpoint file must survive so that
        a subsequent run can reload the done-set and resume from where it left
        off.  Simulated at unit level via IndexingCheckpoint directly."""
        from src.archilles.indexer import IndexingCheckpoint

        cp_path = tmp_path / "test_cp.json"
        cp = IndexingCheckpoint.create_new(
            cp_path, profile="", book_ids=["10", "20", "30"], phase="phase2"
        )
        cp.complete_book("10")
        cp.complete_book("20")
        # Do NOT call cp.delete() — simulates a shutdown mid-run.

        # Checkpoint file must still exist.
        assert cp_path.exists()

        # Reload must reconstruct the same done set.
        loaded = IndexingCheckpoint.load(cp_path)
        assert loaded is not None
        assert set(loaded.completed_books) == {"10", "20"}
        assert loaded.total_books == 3

    def test_scanner_keeps_checkpoint_on_shutdown(self, calibre_library: Path):
        """Drives scan() through the full Phase-4 loop with _shutdown_requested
        tripped by FakeRAG after the first book.

        Exercises the real ``if not self._shutdown_requested: cp.delete()`` gate
        inside scan(), not just IndexingCheckpoint in isolation.  The checkpoint
        file must survive (kept for resume on next run) and
        results['interrupted'] must be True.
        """
        for cid in (1, 2, 3):
            _add_book(calibre_library, cid, f"Stub {cid}",
                      authors=["A"], with_file="x.epub")

        meta_by_id = _calibre_metadata_for_hash(calibre_library)
        indexed_hashes = {
            cid: {
                'book_id': str(cid),
                'metadata_hash': _compute_metadata_hash(meta_by_id[cid]),
                'annotation_hash': '',
                'has_content': False,
            }
            for cid in (1, 2, 3)
        }
        scanner = self._make_scanner(calibre_library, indexed_hashes)

        indexed_calls: list[str] = []

        class FakeRAG:
            def index_book(self_inner, path, book_id, force=False, phase=None):
                indexed_calls.append(book_id)
                # Trip shutdown after the first book so the loop breaks at the
                # start of the second iteration — the gate is checked BEFORE
                # each book, so book 1 is fully recorded in the checkpoint.
                scanner._shutdown_requested = True

        scanner._load_rag = lambda: FakeRAG()

        results = scanner.scan(
            dry_run=False, queue_new=False, index_fulltext_pending=True,
        )

        assert results['interrupted'] is True
        assert scanner.fulltext_checkpoint_file.exists(), (
            "Checkpoint must be KEPT on interrupted run (needed for resume)"
        )
        # Exactly one book was indexed before shutdown tripped
        assert len(indexed_calls) == 1

    def test_scanner_keeps_phase3_checkpoint_on_shutdown(self, calibre_library: Path):
        """Drives scan() through the Phase-3 (index_new) loop with _shutdown_requested
        tripped by FakeRAG after the first book.

        Exercises the real ``if not self._shutdown_requested: cp_new.delete()`` gate
        inside scan(), not just IndexingCheckpoint in isolation.  The checkpoint
        file must survive (kept for resume on next run), results['interrupted']
        must be True, and exactly one book was indexed before the break.
        """
        for cid in (1, 2, 3):
            _add_book(calibre_library, cid, f"New Book {cid}",
                      authors=["A"], with_file="x.epub")

        scanner = WatchdogScanner(
            library_path=calibre_library,
            db_path=str(calibre_library / ".archilles" / "rag_db"),
            archilles_dir=calibre_library / ".archilles",
        )
        # All books are new (not in indexed_hashes)
        scanner._load_indexed_hashes = lambda: {}
        scanner._annotation_changed = lambda file_path, stored_hash: False

        indexed_calls: list[str] = []

        class FakeRAG:
            def index_book(self_inner, path, book_id, force=False, phase=None):
                indexed_calls.append(book_id)
                # Trip shutdown after the first book so the loop breaks at the
                # start of the second iteration — the gate is checked BEFORE
                # each book, so book 1 is fully recorded in the checkpoint.
                scanner._shutdown_requested = True

        scanner._load_rag = lambda: FakeRAG()

        results = scanner.scan(
            dry_run=False, queue_new=False, index_new=True,
        )

        assert results['interrupted'] is True
        assert scanner.checkpoint_file.exists(), (
            "Phase-3 checkpoint must be KEPT on interrupted run (needed for resume)"
        )
        # Exactly one book was indexed before shutdown tripped
        assert len(indexed_calls) == 1

    def test_phase2_skips_phase1_stubs_when_phase4_drains_all(
        self, calibre_library: Path,
    ):
        """When Phase 4 will process the whole backlog (no max_new cap),
        Phase 2 must NOT also refresh phase1-stub books — that would just
        be embedding work Phase 4 overwrites immediately."""
        # Book 10: phase1 stub with a metadata change → could land in BOTH
        # metadata_changed and fulltext_pending.
        _add_book(calibre_library, 10, "Changed Stub",
                  authors=["A"], with_file="x.epub")

        scanner = self._make_scanner(calibre_library, indexed_hashes={
            10: {
                'book_id': '10',
                'metadata_hash': 'stale-hash',  # forces meta_changed=True
                'annotation_hash': '',
                'has_content': False,           # marks it as phase1 stub
            }
        })

        all_calls: list[tuple[str, str | None, bool]] = []

        class CountingRAG:
            def index_book(self, path, book_id, force=False, phase=None):
                all_calls.append((book_id, phase, force))
        scanner._load_rag = lambda: CountingRAG()

        results = scanner.scan(
            dry_run=False, queue_new=False, index_fulltext_pending=True,
        )

        # Book 10 should be indexed exactly once — by Phase 4 (force=False,
        # phase=None), not twice (once by Phase 2 with phase='phase1' then
        # again by Phase 4).
        book_10_calls = [c for c in all_calls if c[0] == '10']
        assert len(book_10_calls) == 1, (
            f"Expected book 10 indexed once by Phase 4, got: {book_10_calls}"
        )
        assert book_10_calls[0] == ('10', None, False)
        assert results['fulltext_indexed'] == 1
        assert results['delta_updates'] == 0  # Phase 2 skipped

    def test_phase2_still_refreshes_phase1_stubs_when_max_new_set(
        self, calibre_library: Path,
    ):
        """When Phase 4 is capped by max_new, some phase1 stubs may stay
        as stubs after the run — so Phase 2 must keep refreshing their
        metadata for them rather than silently dropping the update."""
        _add_book(calibre_library, 20, "Changed Stub", authors=["A"],
                  with_file="x.epub")

        scanner = self._make_scanner(calibre_library, indexed_hashes={
            20: {
                'book_id': '20',
                'metadata_hash': 'stale',
                'annotation_hash': '',
                'has_content': False,
            }
        })

        calls: list[tuple[str, str | None]] = []

        class CountingRAG:
            def index_book(self, path, book_id, force=False, phase=None):
                calls.append((book_id, phase))
        scanner._load_rag = lambda: CountingRAG()

        # max_new=0 means Phase 4 indexes nothing → Phase 2 must still refresh
        results = scanner.scan(
            dry_run=False, queue_new=False, index_fulltext_pending=True,
            max_new=0,
        )

        # Exactly the Phase 2 refresh call should remain
        assert ('20', 'phase1') in calls
        assert results['delta_updates'] == 1

    def test_phase2_still_refreshes_phase1_stubs_when_rating_filter_set(
        self, calibre_library: Path,
    ):
        """Finding 4.7: with --rating set (no --max-new), Phase 4 only drains
        the matching-rating subset — Phase 2 must not skip stub refreshes for
        stubs Phase 4 won't touch this run just because no max_new cap is
        set."""
        _add_book(calibre_library, 30, "Changed Stub", authors=["A"],
                  with_file="x.epub")  # unrated (rating=0) — never matches rating_filter=3

        scanner = self._make_scanner(calibre_library, indexed_hashes={
            30: {
                'book_id': '30',
                'metadata_hash': 'stale',
                'annotation_hash': '',
                'has_content': False,
            }
        })

        calls: list[tuple[str, str | None]] = []

        class CountingRAG:
            def index_book(self, path, book_id, force=False, phase=None):
                calls.append((book_id, phase))
        scanner._load_rag = lambda: CountingRAG()

        # No max_new cap, but rating_filter=3 means Phase 4 only processes
        # 3-star books — the unrated stub is left untouched by Phase 4.
        results = scanner.scan(
            dry_run=False, queue_new=False, index_fulltext_pending=True,
            rating_filter=3,
        )

        assert ('30', 'phase1') in calls
        assert results['delta_updates'] == 1


# ---------------------------------------------------------------------------
# Default excluded tags constant stays stable (batch_index.py sibling)
# ---------------------------------------------------------------------------

def test_default_excluded_tags_single_source_of_truth():
    # ``DEFAULT_EXCLUDED_TAGS`` lives in ``src.archilles.config``; both
    # the watchdog and batch_index re-export the same list. If anyone
    # redefines it locally, this guard catches the divergence.
    from scripts import batch_index
    from src.archilles import config
    assert DEFAULT_EXCLUDED_TAGS is config.DEFAULT_EXCLUDED_TAGS
    assert batch_index.DEFAULT_EXCLUDED_TAGS is config.DEFAULT_EXCLUDED_TAGS
    assert DEFAULT_EXCLUDED_TAGS == ['exclude']


# ---------------------------------------------------------------------------
# Orphan cleanup: indexed books deleted from Calibre
# ---------------------------------------------------------------------------

class TestOrphanCleanup:
    """Scheduled scans must remove index entries for deleted books instead of
    letting them accumulate (watchdog counterpart of --cleanup-orphans)."""

    @pytest.fixture
    def scanner_factory(self, tmp_path: Path):
        def _make(indexed_hashes: dict, excluded: list[str] | None = None):
            scanner = WatchdogScanner(
                library_path=tmp_path,
                db_path=str(tmp_path / ".archilles" / "rag_db"),
                archilles_dir=tmp_path / ".archilles",
                excluded_tags=excluded,
            )
            scanner._load_indexed_hashes = lambda: indexed_hashes
            scanner._annotation_changed = lambda file_path, stored_hash: False
            return scanner
        return _make

    def test_deleted_book_reported_in_dry_run(
        self, calibre_library: Path, scanner_factory,
    ):
        _add_book(calibre_library, 1, "Still here",
                  authors=["A"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={
            1:  {'book_id': '1', 'metadata_hash': '', 'annotation_hash': ''},
            99: {'book_id': '99', 'metadata_hash': 'h', 'annotation_hash': ''},
        })

        results = scanner.scan(dry_run=True)

        assert results['orphans_found'] == ['99']
        assert results['orphans_removed'] == 0

    def test_deleted_book_removed_from_index(
        self, tmp_path: Path, calibre_library: Path, scanner_factory,
    ):
        _add_book(calibre_library, 1, "Still here",
                  authors=["A"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={
            1:  {'book_id': '1', 'metadata_hash': '', 'annotation_hash': ''},
            99: {'book_id': '99', 'metadata_hash': 'h', 'annotation_hash': ''},
        })

        with patch("src.storage.lancedb_store.LanceDBStore") as mock_store_cls, \
             patch("src.archilles.watchdog.backup_orphan_chunks") as mock_backup:
            mock_store_cls.return_value.delete_by_book_id.return_value = 7
            mock_backup.return_value = tmp_path / "backups" / "orphans.parquet"
            results = scanner.scan(dry_run=False, queue_new=False)

        mock_store_cls.return_value.delete_by_book_id.assert_called_once_with('99')
        mock_backup.assert_called_once()
        assert results['orphan_backup_path'].endswith("orphans.parquet")
        assert results['orphans_found'] == ['99']
        assert results['orphans_removed'] == 1
        assert not results['errors']

    def test_deletion_is_refused_when_the_backup_fails(
        self, calibre_library: Path, scanner_factory,
    ):
        """No rollback file, no deletion: LanceDB's two-day version window is
        shorter than the weekly mail that would surface the mistake."""
        _add_book(calibre_library, 1, "Still here",
                  authors=["A"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={
            1:  {'book_id': '1', 'metadata_hash': '', 'annotation_hash': ''},
            99: {'book_id': '99', 'metadata_hash': 'h', 'annotation_hash': ''},
        })

        with patch("src.storage.lancedb_store.LanceDBStore") as mock_store_cls, \
             patch("src.archilles.watchdog.backup_orphan_chunks", return_value=None):
            results = scanner.scan(dry_run=False, queue_new=False)

        mock_store_cls.return_value.delete_by_book_id.assert_not_called()
        assert results['orphans_removed'] == 0
        assert results['orphan_cleanup_refused'] is True

    def test_empty_library_snapshot_skips_cleanup(
        self, calibre_library: Path, scanner_factory,
    ):
        """An empty metadata.db read must never wipe the index: with zero
        library books every indexed entry would look orphaned — the scanner
        skips the cleanup instead."""
        scanner = scanner_factory(indexed_hashes={
            1: {'book_id': '1', 'metadata_hash': 'h', 'annotation_hash': ''},
        })

        with patch("src.storage.lancedb_store.LanceDBStore") as mock_store_cls:
            results = scanner.scan(dry_run=False, queue_new=False)

        mock_store_cls.return_value.delete_by_book_id.assert_not_called()
        assert results['orphans_found'] == []
        assert results['orphans_removed'] == 0

    def test_excluded_tag_book_is_not_an_orphan(
        self, calibre_library: Path, scanner_factory,
    ):
        """Excluded-tag books are skipped for indexing but still exist in the
        library — they must not be treated as deleted."""
        _add_book(calibre_library, 1, "Excluded but present",
                  authors=["A"], tags=["exclude"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={
            1: {'book_id': '1', 'metadata_hash': 'h', 'annotation_hash': ''},
        })

        results = scanner.scan(dry_run=True)

        assert results['orphans_found'] == []

    def test_deletion_failure_is_recorded_not_fatal(
        self, tmp_path: Path, calibre_library: Path, scanner_factory,
    ):
        _add_book(calibre_library, 1, "Still here",
                  authors=["A"], with_file="x.epub")
        scanner = scanner_factory(indexed_hashes={
            1:  {'book_id': '1', 'metadata_hash': '', 'annotation_hash': ''},
            99: {'book_id': '99', 'metadata_hash': 'h', 'annotation_hash': ''},
        })

        with patch("src.storage.lancedb_store.LanceDBStore") as mock_store_cls, \
             patch("src.archilles.watchdog.backup_orphan_chunks") as mock_backup:
            mock_store_cls.return_value.delete_by_book_id.side_effect = \
                RuntimeError("commit conflict")
            mock_backup.return_value = tmp_path / "backups" / "orphans.parquet"
            results = scanner.scan(dry_run=False, queue_new=False)

        assert results['orphans_removed'] == 0
        assert any('commit conflict' in e['error'] for e in results['errors'])


# ---------------------------------------------------------------------------
# _priority_match: shared, data-model-agnostic priority matcher
# ---------------------------------------------------------------------------


class TestPriorityMatch:
    """Author/title match by substring; tags/collections by exact
    (case-insensitive) set membership — preserving the original Calibre rule
    and extending it to Zotero collections."""

    def test_author_substring(self):
        from src.archilles.watchdog import _priority_match
        assert _priority_match("Hannah Arendt", [], "", [], ["arendt"], [], [], []) is True
        assert _priority_match("Hannah Arendt", [], "", [], ["kant"], [], [], []) is False

    def test_tag_exact_membership(self):
        from src.archilles.watchdog import _priority_match
        assert _priority_match("", ["Prio"], "", [], [], ["prio"], [], []) is True
        # substring must NOT match — exact membership only
        assert _priority_match("", ["Priority"], "", [], [], ["prio"], [], []) is False

    def test_collection_exact_membership(self):
        from src.archilles.watchdog import _priority_match
        assert _priority_match("", [], "", ["Current Project"], [], [], [], ["current project"]) is True
        assert _priority_match("", [], "", ["Archive"], [], [], [], ["current project"]) is False

    def test_title_substring(self):
        from src.archilles.watchdog import _priority_match
        assert _priority_match("", [], "The Human Condition", [], [], [], ["human"], []) is True

    def test_no_filters_is_false(self):
        from src.archilles.watchdog import _priority_match
        assert _priority_match("Author", ["tag"], "Title", ["Coll"], [], [], [], []) is False
