"""Phase-1 stubs must chunk Calibre comments like phase 2 does.

Before this, _index_book_phase1 appended the WHOLE comment to the stub's
searchable text: one vector for title/author/publisher plus a comment of up
to 20k+ words. Long comments were therefore unsearchable in any granular
way until phase 2 ran — and phase 2 is deliberately deferred here.

The stub also carried no metadata_hash, so get_hashes_for_indexed_books()
reported '' for the book and the watchdog's
``meta_changed = bool(stored_meta_hash) and ...`` was always False: a comment
written AFTER the stub was created never reached the index. Worse, the
refresh path itself runs through _index_book_phase1, so a stub that did get
refreshed lost its hash and fell out of the diff permanently.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from src.archilles.constants import ChunkType
from src.archilles.engine.indexing import Indexer
from src.archilles.hashing import compute_metadata_hash


def _fake_encode(texts, **kwargs):
    if isinstance(texts, str):
        return np.zeros(1024, dtype=np.float32)
    return np.zeros((len(texts), 1024), dtype=np.float32)


class _RecordingStore:
    def __init__(self):
        self.chunks = []
        self.embedding_rows = 0

    def add_chunks(self, chunks, embeddings):
        self.chunks.extend(chunks)
        self.embedding_rows += len(embeddings)
        return len(chunks)

    def count(self):
        return len(self.chunks)


def _make_indexer(store):
    rag = SimpleNamespace(
        store=store,
        embedding_model=SimpleNamespace(encode=_fake_encode),
        _format_tags=lambda t: ", ".join(t) if isinstance(t, list) else t,
        _adapter=None,
    )
    return Indexer(rag)


def _dummy_book(tmp_path):
    book = tmp_path / "book.pdf"
    book.write_bytes(b"%PDF-1.4 dummy")
    return book


def _index(idx, tmp_path, metadata):
    with patch(
        "src.archilles.engine.indexing.get_combined_annotations",
        return_value={"annotations": []},
    ):
        return idx._index_book_phase1(_dummy_book(tmp_path), "42", metadata)


# A comment well past the 400-word split threshold used by _build_comment_chunks.
LONG_COMMENT = " ".join(f"Satz Nummer {i} mit etwas Inhalt." for i in range(400))


class TestPhase1ChunksComments:
    def test_long_comment_is_split_into_multiple_chunks(self, tmp_path):
        store = _RecordingStore()
        result = _index(_make_indexer(store), tmp_path,
                        {"title": "T", "author": "A", "comments": LONG_COMMENT})

        comment_chunks = [c for c in store.chunks
                          if c["chunk_type"] == ChunkType.CALIBRE_COMMENT]
        assert len(comment_chunks) > 1, (
            "a comment far beyond the 400-word threshold must not end up in one chunk"
        )
        assert result["chunks_indexed"] == 1 + len(comment_chunks)
        assert store.embedding_rows == len(store.chunks)

    def test_comment_is_not_inlined_into_the_stub(self, tmp_path):
        store = _RecordingStore()
        _index(_make_indexer(store), tmp_path,
               {"title": "T", "author": "A", "comments": LONG_COMMENT})

        stub = next(c for c in store.chunks
                    if c["chunk_type"] == ChunkType.PHASE1_METADATA)
        assert "Satz Nummer 399" not in stub["text"], (
            "the comment must live in calibre_comment chunks, not in the stub vector"
        )
        assert "Title: T" in stub["text"]

    def test_comment_text_survives_in_the_comment_chunks(self, tmp_path):
        """Nothing may be dropped in the move out of the stub."""
        store = _RecordingStore()
        _index(_make_indexer(store), tmp_path,
               {"title": "T", "comments": LONG_COMMENT})

        blob = " ".join(c["text"] for c in store.chunks
                        if c["chunk_type"] == ChunkType.CALIBRE_COMMENT)
        assert "Satz Nummer 0 " in blob
        assert "Satz Nummer 399" in blob

    def test_html_comment_is_split_at_headlines(self, tmp_path):
        store = _RecordingStore()
        html = ("<h2>Kritik</h2><p>Erster Abschnitt.</p>"
                "<h2>Uebersetzte Passage</h2><p>Zweiter Abschnitt.</p>")
        _index(_make_indexer(store), tmp_path,
               {"title": "T", "comments": "Erster Abschnitt. Zweiter Abschnitt.",
                "comments_html": html})

        comment_chunks = [c for c in store.chunks
                          if c["chunk_type"] == ChunkType.CALIBRE_COMMENT]
        assert len(comment_chunks) == 2
        assert {c.get("section_title") for c in comment_chunks} == {
            "Kritik", "Uebersetzte Passage"}

    def test_short_comment_stays_one_chunk(self, tmp_path):
        store = _RecordingStore()
        _index(_make_indexer(store), tmp_path,
               {"title": "T", "comments": "Kurze Notiz zum Buch."})

        assert len([c for c in store.chunks
                    if c["chunk_type"] == ChunkType.CALIBRE_COMMENT]) == 1

    def test_no_comment_writes_stub_only(self, tmp_path):
        store = _RecordingStore()
        result = _index(_make_indexer(store), tmp_path, {"title": "T"})

        assert [c["chunk_type"] for c in store.chunks] == [ChunkType.PHASE1_METADATA]
        assert result["chunks_indexed"] == 1


class TestPhase1WritesMetadataHash:
    def test_stub_and_comment_chunks_carry_the_scanner_hash(self, tmp_path):
        """The watchdog compares this value against compute_metadata_hash() of
        the freshly scanned Calibre metadata. A mismatch means either a frozen
        stub (empty hash) or a nightly re-index storm (wrong hash)."""
        store = _RecordingStore()
        meta = {"title": "T", "author": "A", "publisher": "P",
                "tags": ["prio", "essay"], "comments": LONG_COMMENT}
        _index(_make_indexer(store), tmp_path, meta)

        expected = compute_metadata_hash(meta)
        assert expected, "test fixture must produce a non-empty hash"
        for chunk in store.chunks:
            assert chunk.get("metadata_hash") == expected, (
                f"{chunk['chunk_type']} carries {chunk.get('metadata_hash')!r}"
            )

    def test_hash_is_written_even_without_a_comment(self, tmp_path):
        """A comment-less stub must take part in the diff too — otherwise the
        first comment ever written for that title is never picked up."""
        store = _RecordingStore()
        meta = {"title": "T", "author": "A"}
        _index(_make_indexer(store), tmp_path, meta)

        stub = store.chunks[0]
        assert stub["metadata_hash"] == compute_metadata_hash(meta)
        assert stub["metadata_hash"]
