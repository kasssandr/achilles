"""Delete only once the replacement exists (review 1.5), and delete enough (1.6).

Two defects in one region of ``Indexer.index_book``:

**1.5 — deletion on speculation.** The old order was delete → extract metadata →
extract text → embed → add. Both extraction steps can raise, the exception is
caught by the watchdog's Phase 4 loop, logged, and the loop continues — leaving
the book with zero chunks. Phase 4 is the fulltext backlog, about to run over
4 680 stubs. Content extraction self-heals within about a day (the book drops
out of ``indexed_hashes`` and is re-stubbed on the next Phase A); *metadata*
extraction failing does not.

**1.6(a) — force deleted less than non-force.** The branch read
``if state['has_content']: … elif state['total'] and not force:``. A stub-only
book (has_content False, total > 0) with force=True matched neither arm: nothing
was deleted and a full index was written on top of the stub.

**1.6(b) — upsert is not replacement.** ``add_chunks`` merges on ``id``, which
guarantees "no duplicate id", not "no stale row". Every row whose id is not
regenerated survives with its old text *and its old vector*: the
``{book_id}_metadata`` stub, ``{book_id}_comment_3..7`` when the comment now
splits into three sections, ``{book_id}_annot_12..40`` when highlights were
removed. Unreferenced by any state query, and returned by search — the
mechanism behind the leftovers `cec290c` cleaned by hand.
"""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.archilles.constants import ChunkType


class _RecordingStore:
    """Records the order of store operations, so ordering can be asserted."""

    def __init__(self, state=None):
        self.events = []
        self._state = state or {
            'total': 12, 'has_content': True, 'content_count': 10,
            'metadata_hash': 'old', 'annotation_hash': 'old',
        }

    def get_book_state(self, book_id):
        self.events.append(("get_book_state", book_id))
        return dict(self._state)

    def delete_by_book_id(self, book_id):
        self.events.append(("delete_all", book_id))
        return 12

    def delete_by_book_id_except_annotations(self, book_id):
        self.events.append(("delete_except_annotations", book_id))
        return 10

    def delete_by_book_id_and_type(self, book_id, chunk_type):
        self.events.append(("delete_type", book_id, chunk_type))
        return 2

    def add_chunks(self, chunks, embeddings):
        self.events.append(("add_chunks", len(chunks)))
        return len(chunks)

    def count(self):
        return 0

    @property
    def deletions(self):
        return [e for e in self.events if e[0].startswith("delete")]


def _indexer(store, extractor=None, metadata=None):
    """An Indexer over a stub RAG: recording store, controllable extractor.

    Constants come from the real ArchillesRAG class so the stub cannot drift
    away from what the indexer expects to find on it.
    """
    from src.archilles.engine.core import ArchillesRAG
    from src.archilles.engine.indexing import Indexer

    rag = SimpleNamespace(
        _CHUNK_META_KEYS=ArchillesRAG._CHUNK_META_KEYS,
        store=store,
        _adapter=None,
        extractor=extractor or SimpleNamespace(extract=lambda p: _extraction()),
        use_modular_pipeline=False,
        hierarchical=False,
        batch_size=8,
        device="cpu",
        _prepare_chunk_size=512,
        _prepare_overlap=64,
        embedding_model=SimpleNamespace(
            encode=lambda texts, **kw: np.zeros((len(texts), 8), dtype=np.float32)
        ),
        languages=None,
    )
    idx = Indexer(rag)
    if metadata is not None:
        idx._extract_metadata = lambda p: metadata
    return idx


def _extraction(n_chunks=3):
    """Mirrors what index_book actually reads off an extraction result."""
    return SimpleNamespace(
        chunks=[
            {"text": f"chunk {i}", "metadata": {}, "chunk_index": i}
            for i in range(n_chunks)
        ],
        metadata=SimpleNamespace(
            detected_format="epub",
            file_path=Path("book.epub"),
            total_pages=10,
            total_words=100,
        ),
    )


@pytest.fixture
def book(tmp_path):
    p = tmp_path / "book.epub"
    p.write_bytes(b"not really an epub")
    return p


class TestNothingIsDeletedBeforeTheReplacementExists:
    """1.5: a failing extractor must leave the book as it was."""

    def test_text_extraction_failure_deletes_nothing(self, book):
        store = _RecordingStore()

        def boom(path):
            raise RuntimeError("PDF parser blew up")

        idx = _indexer(store, extractor=SimpleNamespace(extract=boom),
                       metadata={"title": "T", "author": "A"})

        with pytest.raises(RuntimeError):
            idx.index_book(str(book), book_id="42", force=True)

        assert store.deletions == [], "the book still has its chunks"

    def test_metadata_extraction_failure_deletes_nothing(self, book):
        """The half that does *not* self-heal on the next scan."""
        store = _RecordingStore()
        idx = _indexer(store)

        def boom(path):
            raise OSError("metadata.db unreadable")

        idx._extract_metadata = boom

        with pytest.raises(OSError):
            idx.index_book(str(book), book_id="42", force=True)

        assert store.deletions == []

    def test_delete_happens_after_extraction_not_before(self, book):
        store = _RecordingStore()
        idx = _indexer(store, metadata={"title": "T", "author": "A"})

        idx.index_book(str(book), book_id="42", force=True)

        names = [e[0] for e in store.events]
        assert "add_chunks" in names
        delete_at = next(i for i, n in enumerate(names) if n.startswith("delete"))
        add_at = names.index("add_chunks")
        assert delete_at < add_at, "delete must still precede the write"


class TestForceDeletesAtLeastAsMuchAsNonForce:
    """1.6(a): the stub-only + force combination fell through both arms."""

    def test_stub_only_with_force_still_replaces(self, book):
        store = _RecordingStore(state={
            'total': 1, 'has_content': False, 'content_count': 0,
            'metadata_hash': 'old', 'annotation_hash': '',
        })
        idx = _indexer(store, metadata={"title": "T", "author": "A"})

        idx.index_book(str(book), book_id="42", force=True)

        assert store.deletions, "the stub must not survive under the new content"

    def test_stub_only_without_force_replaces_too(self, book):
        store = _RecordingStore(state={
            'total': 1, 'has_content': False, 'content_count': 0,
            'metadata_hash': 'old', 'annotation_hash': '',
        })
        idx = _indexer(store, metadata={"title": "T", "author": "A"})

        idx.index_book(str(book), book_id="42", force=False)

        assert store.deletions

    def test_nothing_indexed_yet_deletes_nothing(self, book):
        store = _RecordingStore(state={
            'total': 0, 'has_content': False, 'content_count': 0,
            'metadata_hash': '', 'annotation_hash': '',
        })
        idx = _indexer(store, metadata={"title": "T", "author": "A"})

        idx.index_book(str(book), book_id="42")

        assert store.deletions == [], "no rows, nothing to replace"


class TestAnnotationsSurviveAReplacement:
    """July's 2.2: annotations are imported separately and can be weeks newer
    than the text, so a full re-index must not take them with it."""

    def test_replacement_spares_annotation_chunks(self, book):
        store = _RecordingStore()
        idx = _indexer(store, metadata={"title": "T", "author": "A"})

        idx.index_book(str(book), book_id="42", force=True)

        kinds = [e[0] for e in store.deletions]
        assert "delete_all" not in kinds, "a blanket delete would drop annotations"
        assert "delete_except_annotations" in kinds


# ── the property a mock cannot show: stale rows actually disappear ───

class TestStaleRowsAreGoneAgainstARealStore:
    """1.6(b) against a real temporary LanceDB: re-indexing with fewer chunks
    must leave no rows from the previous generation."""

    def _chunks(self, book_id, n, generation):
        return [
            {
                "id": f"{book_id}_chunk_{i}",
                "text": f"generation {generation} chunk {i}",
                "book_id": book_id,
                "calibre_id": 0,
                "chunk_index": i,
                "chunk_type": ChunkType.CONTENT,
            }
            for i in range(n)
        ]

    def _emb(self, n, dim=1024):
        v = np.random.randn(n, dim).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def test_upsert_alone_leaves_stale_rows(self, tmp_path):
        """The defect itself, pinned so the fix is measured against it."""
        from src.storage.lancedb_store import LanceDBStore

        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(self._chunks("b", 5, "old"), self._emb(5))
        store.add_chunks(self._chunks("b", 2, "new"), self._emb(2))

        rows = store.get_by_book_id("b", limit=100)
        assert len(rows) == 5, "upsert replaced 2 ids and left 3 behind"
        assert any("generation old" in r["text"] for r in rows)

    def test_explicit_replacement_removes_them(self, tmp_path):
        from src.storage.lancedb_store import LanceDBStore

        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(self._chunks("b", 5, "old"), self._emb(5))

        store.delete_by_book_id_except_annotations("b")
        store.add_chunks(self._chunks("b", 2, "new"), self._emb(2))

        rows = store.get_by_book_id("b", limit=100)
        assert len(rows) == 2
        assert all("generation new" in r["text"] for r in rows)

    def test_annotations_survive_that_replacement(self, tmp_path):
        from src.storage.lancedb_store import LanceDBStore

        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(self._chunks("b", 3, "old"), self._emb(3))
        store.add_chunks([{
            "id": "b_annot_0", "text": "[ANNOTATION] kept",
            "book_id": "b", "calibre_id": 0, "chunk_index": -10,
            "chunk_type": ChunkType.ANNOTATION,
        }], self._emb(1))

        store.delete_by_book_id_except_annotations("b")
        store.add_chunks(self._chunks("b", 1, "new"), self._emb(1))

        rows = store.get_by_book_id("b", limit=100)
        texts = {r["text"] for r in rows}
        assert "[ANNOTATION] kept" in texts
        assert len(rows) == 2
