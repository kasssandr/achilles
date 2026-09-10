"""Scriptor bundles in the library: where they lie, and how a book's text comes from one.

A bundle is the prepared text of a Calibre book, kept in the library's extension
zone at ``<library>/.archilles/scriptor/<key>/`` (key = the book's prepared-JSONL
name). It is not another format of the book: the book file keeps the book's
identity -- Calibre metadata, viewer annotations (keyed by the file's path) and
links follow it -- and only the text is read from the bundle, wherever indexing
extracts (index_book, prepare_book).
"""

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.archilles.book_files import bundle_master, discover_formats

MASTER = "---\nformat_version: 0.3.0\nchunking_strategy: basic\n---\n\n[p. 1] Ein Satz.\n"


def _library(tmp_path):
    """A Calibre library with one book (id 10593), a PDF and an EPUB."""
    (tmp_path / "metadata.db").touch()
    book_dir = tmp_path / "Bauer" / "Aneignung (10593)"
    book_dir.mkdir(parents=True)
    (book_dir / "book.pdf").write_bytes(b"%PDF-1.4")
    (book_dir / "book.epub").write_bytes(b"PK")
    return tmp_path, book_dir


def _bundle(library, key="10593", name="book.md", text=MASTER):
    folder = library / ".archilles" / "scriptor" / key
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return folder / name


# discover_formats: one function for batch_index and the watchdog ---------------

def test_formats_come_in_preferred_order_and_nothing_else_counts(tmp_path):
    _, book_dir = _library(tmp_path)
    (book_dir / "cover.jpg").touch()
    (book_dir / "metadata.opf").touch()
    (book_dir / "data.pdf").mkdir()              # a folder is not a format
    assert discover_formats(book_dir) == [
        {"format": "PDF", "path": str(book_dir / "book.pdf")},
        {"format": "EPUB", "path": str(book_dir / "book.epub")},
    ]


def test_a_missing_book_folder_has_no_formats(tmp_path):
    assert discover_formats(tmp_path / "gone") == []


# bundle_master ------------------------------------------------------------------

def test_the_master_is_found_under_the_key_of_its_book(tmp_path):
    library, _ = _library(tmp_path)
    master = _bundle(library)
    _bundle(library, name="book.review.md")              # the review copy is not the master
    (master.parent / "book.md.pagination.json").write_text("{}", encoding="utf-8")
    assert bundle_master(library / ".archilles", "10593") == master


def test_the_key_is_the_prepared_name_of_the_book_id(tmp_path):
    # Zotero keys and folder ids are sanitised exactly as the prepared JSONL is.
    from src.archilles.book_files import prepared_jsonl_name
    key = prepared_jsonl_name("folder:ab/cd")[:-len(".jsonl")]
    master = _bundle(tmp_path, key=key)
    assert bundle_master(tmp_path / ".archilles", "folder:ab/cd") == master


def test_markdown_without_a_format_version_is_no_master(tmp_path):
    _bundle(tmp_path, text="# Notizen\n\nNur ein Text.\n")
    assert bundle_master(tmp_path / ".archilles", "10593") is None


def test_two_masters_are_refused_and_both_named(tmp_path, caplog):
    _bundle(tmp_path, name="book.md")
    _bundle(tmp_path, name="other.md")
    with caplog.at_level(logging.WARNING):
        assert bundle_master(tmp_path / ".archilles", "10593") is None
    assert "book.md" in caplog.text and "other.md" in caplog.text


def test_no_bundle_folder_means_no_bundle(tmp_path):
    assert bundle_master(tmp_path / ".archilles", "10593") is None


# the text comes from the bundle, the identity from the book file ----------------

def _extraction(path):
    return SimpleNamespace(
        chunks=[{"text": "ein Satz", "metadata": {}}],
        metadata=SimpleNamespace(detected_format="scriptor", file_path=Path(path),
                                 total_pages=None, total_words=2),
    )


class _Store:
    def get_book_state(self, book_id):
        return {'total': 0, 'has_content': False, 'content_count': 0,
                'metadata_hash': '', 'annotation_hash': ''}

    def add_chunks(self, chunks, embeddings):
        return len(chunks)

    def count(self):
        return 0


def _indexer(extracted_from, metadata_from):
    from src.archilles.engine.core import ArchillesRAG
    from src.archilles.engine.indexing import Indexer

    def extract(path):
        extracted_from.append(Path(path))
        return _extraction(path)

    rag = SimpleNamespace(
        _CHUNK_META_KEYS=ArchillesRAG._CHUNK_META_KEYS, store=_Store(), _adapter=None,
        extractor=SimpleNamespace(extract=extract), use_modular_pipeline=False,
        hierarchical=False, batch_size=8, device="cpu", _prepare_chunk_size=512,
        _prepare_overlap=64, languages=None,
        embedding_model=SimpleNamespace(
            encode=lambda texts, **kw: np.zeros((len(texts), 8), dtype=np.float32)),
    )
    idx = Indexer(rag)
    idx._extract_metadata = lambda p: metadata_from.append(Path(p)) or {"title": "T", "author": "A"}
    return idx


def test_index_book_reads_the_text_from_the_bundle_and_the_rest_from_the_book_file(tmp_path):
    library, book_dir = _library(tmp_path)
    master = _bundle(library)
    extracted_from, metadata_from = [], []
    _indexer(extracted_from, metadata_from).index_book(
        str(book_dir / "book.pdf"), book_id="10593", force=True)
    assert extracted_from == [master]
    assert metadata_from == [book_dir / "book.pdf"]


def test_without_a_bundle_the_book_file_is_read(tmp_path):
    library, book_dir = _library(tmp_path)
    extracted_from, metadata_from = [], []
    _indexer(extracted_from, metadata_from).index_book(
        str(book_dir / "book.pdf"), book_id="10593", force=True)
    assert extracted_from == [book_dir / "book.pdf"]


def test_prepare_book_reads_the_bundle_too(tmp_path):
    library, book_dir = _library(tmp_path)
    master = _bundle(library)
    extracted_from, metadata_from = [], []
    idx = _indexer(extracted_from, metadata_from)
    idx._override_extractor_chunking = lambda size, overlap: _nothing()
    idx.prepare_book(str(book_dir / "book.pdf"), "10593", output_dir=str(tmp_path / "out"))
    assert extracted_from == [master]
    assert metadata_from == [book_dir / "book.pdf"]


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_prepare_book_cuts_a_bundle_at_the_prepare_chunk_size():
    """Phase-1 chunks are larger than live ones; the override has to reach
    every sub-extractor, the Scriptor one included."""
    from src.archilles.engine.indexing import Indexer
    from src.extractors.universal_extractor import UniversalExtractor

    rag = SimpleNamespace(extractor=UniversalExtractor(chunk_size=512, overlap=64))
    with Indexer(rag)._override_extractor_chunking(2048, 256):
        assert (rag.extractor.scriptor_extractor.chunk_size,
                rag.extractor.scriptor_extractor.overlap) == (2048, 256)
    assert rag.extractor.scriptor_extractor.chunk_size == 512


# batch_index: the bundle is shown, and wins without comparison ------------------

def _book_entry(library):
    from scripts.batch_index import _build_book_entry
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT 10593 AS id, 'Aneignung' AS title, 'Bauer' AS author, "
                     "'Bauer/Aneignung (10593)' AS path, 0 AS rating").fetchone()
    return _build_book_entry(row, library)


def test_a_book_entry_names_its_bundle(tmp_path):
    library, _ = _library(tmp_path)
    master = _bundle(library)
    assert _book_entry(library)["bundle"] == str(master)


def test_a_book_entry_without_a_bundle_says_so(tmp_path):
    library, _ = _library(tmp_path)
    assert _book_entry(library)["bundle"] is None


def test_dry_run_shows_the_bundle_as_the_format(tmp_path, capsys):
    from scripts.batch_index import batch_index
    library, book_dir = _library(tmp_path)
    _bundle(library)
    batch_index([_book_entry(library)], rag=None, dry_run=True)
    out = capsys.readouterr().out
    assert "Format: SCRIPTOR" in out
    assert str(book_dir / "book.pdf") in out     # still handed the book file


def test_quality_selection_does_not_compare_formats_of_a_book_with_a_bundle(tmp_path):
    """Both formats would be read from the same bundle; the bundle passed its
    checks when it was made, as the watchdog assumes too."""
    from scripts.batch_index import batch_index
    library, book_dir = _library(tmp_path)
    _bundle(library)

    # Recorded, not raised: the comparison swallows a failing candidate.
    compared, indexed = [], []
    rag = SimpleNamespace(
        prepare_book=lambda path, *a, **kw: compared.append(path) or {},
        index_book=lambda path, book_id, force, phase: indexed.append(path) or {
            'chunks_indexed': 1},
    )
    batch_index([_book_entry(library)], rag=rag, quality_select=True)
    assert compared == []
    assert indexed == [str(book_dir / "book.pdf")]
