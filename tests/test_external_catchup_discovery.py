"""Finding 1.2: derive the external catch-up list from the index, not the marker.

``pending_external`` is written only under ``mode: full-external``.  Both
libraries on this machine resolve ``auto`` → ``light``, so every title indexed
since 2026-07-12 sits in *neither* list: not in the frozen ``prepared_chunks``
and not in the pending set.  After an external run those books would be
indistinguishable from the embedded ones.

The index itself still knows: a flat book has ``content`` chunks and no
PARENT/CHILD.  Discovery therefore returns ``pending_external`` **∪** "content
chunks but no parents", the second term only for indexes that hold hierarchical
chunks at all — so a deliberately flat library does not nominate its whole
corpus.
"""

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.archilles.constants import ChunkType
from src.storage.lancedb_store import LanceDBStore
from scripts.batch_index import discover_pending_external_books


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(db_path=str(tmp_path / "test_db"))


def _chunks(n, book_id, chunk_type=ChunkType.CONTENT, calibre_id=0):
    return [
        {
            "id": f"{book_id}_{chunk_type}_{i}",
            "text": f"{chunk_type} chunk {i} of {book_id}",
            "book_id": book_id,
            "calibre_id": calibre_id,
            "chunk_index": i,
            "chunk_type": chunk_type,
        }
        for i in range(n)
    ]


def _emb(n, dim=1024):
    v = np.random.randn(n, dim).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


# ── The store-level query ───────────────────────────────────────────────


class TestBookIdsWithoutParentChunks:
    def test_finds_flat_book_and_ignores_hierarchical_one(self, store):
        store.add_chunks(_chunks(2, "flat"), _emb(2))
        store.add_chunks(_chunks(2, "hier", ChunkType.PARENT), _emb(2))
        store.add_chunks(_chunks(3, "hier", ChunkType.CHILD), _emb(3))

        assert store.get_book_ids_without_parent_chunks() == {"flat"}

    def test_child_chunks_alone_also_count_as_hierarchical(self, store):
        """An externally embedded book may land its CHILD chunks first; it
        must not be nominated for a re-run in that window."""
        store.add_chunks(_chunks(2, "childonly", ChunkType.CHILD), _emb(2))

        assert store.get_book_ids_without_parent_chunks() == set()

    def test_metadata_stub_is_not_nominated(self, store):
        """A phase-1 stub has no fulltext to re-embed."""
        store.add_chunks(_chunks(1, "stub", ChunkType.PHASE1_METADATA), _emb(1))

        assert store.get_book_ids_without_parent_chunks() == set()

    def test_book_with_both_flat_and_parent_chunks_is_settled(self, store):
        """Content + parents means the hierarchical version already landed."""
        store.add_chunks(_chunks(2, "mixed"), _emb(2))
        store.add_chunks(_chunks(1, "mixed", ChunkType.PARENT), _emb(1))

        assert store.get_book_ids_without_parent_chunks() == set()

    def test_empty_store_returns_empty_set(self, store):
        assert store.get_book_ids_without_parent_chunks() == set()


# ── Discovery: the union, and the flat-library guard ────────────────────


def _fake_rag(pending, without_parents, has_parents):
    return SimpleNamespace(store=SimpleNamespace(
        get_pending_external_book_ids=lambda: set(pending),
        get_book_ids_without_parent_chunks=lambda: set(without_parents),
        has_parent_chunks=lambda: has_parents,
    ))


class TestDiscoveryUnion:
    def test_union_of_marked_and_parentless(self, monkeypatch):
        """The book indexed under `light` (no marker, no parents) must be
        discovered alongside the explicitly marked one."""
        seen = {}
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda lib, ids: seen.setdefault("ids", ids) or [{"id": i} for i in ids],
        )
        rag = _fake_rag(pending={"3"}, without_parents={"7"}, has_parents=True)

        books = discover_pending_external_books(rag, Path("/lib"))

        assert seen["ids"] == [3, 7]
        assert len(books) == 2

    def test_flat_library_nominates_only_marked_books(self, monkeypatch):
        """Without a single hierarchical chunk the index is deliberately flat.
        Nominating every book would queue the whole corpus for a metered run."""
        seen = {}
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda lib, ids: seen.setdefault("ids", ids) or [{"id": i} for i in ids],
        )
        rag = _fake_rag(pending={"3"}, without_parents={"7", "8"}, has_parents=False)

        discover_pending_external_books(rag, Path("/lib"))

        assert seen["ids"] == [3], "flat library must not nominate unmarked books"

    def test_flat_library_with_nothing_marked_returns_empty(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda lib, ids: called.append(ids) or [],
        )
        rag = _fake_rag(pending=set(), without_parents={"7"}, has_parents=False)

        assert discover_pending_external_books(rag, Path("/lib")) == []
        assert called == []

    def test_no_duplicate_when_book_is_both_marked_and_parentless(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda lib, ids: seen.setdefault("ids", ids) or [{"id": i} for i in ids],
        )
        rag = _fake_rag(pending={"5"}, without_parents={"5"}, has_parents=True)

        discover_pending_external_books(rag, Path("/lib"))

        assert seen["ids"] == [5]

    def test_adapter_path_also_sees_the_union(self, monkeypatch):
        """Zotero keys are non-numeric — the union must reach the adapter
        branch too, not just the Calibre one."""
        rag = _fake_rag(pending={"ZK1"}, without_parents={"ZK2"}, has_parents=True)
        adapter = SimpleNamespace(adapter_type="zotero")
        monkeypatch.setattr(
            "scripts.batch_index._adapter_list_books",
            lambda a: [{"id": "ZK1"}, {"id": "ZK2"}, {"id": "ZK9"}],
        )

        books = discover_pending_external_books(rag, Path("/lib"), adapter=adapter)

        assert {b["id"] for b in books} == {"ZK1", "ZK2"}


class TestProvenanceReporting:
    """The two halves have different provenance and the derived one can be
    large — the run must say so before anything expensive starts."""

    def test_reports_the_split(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda lib, ids: [{"id": i} for i in ids],
        )
        rag = _fake_rag(pending={"3"}, without_parents={"7", "8"}, has_parents=True)

        discover_pending_external_books(rag, Path("/lib"))

        out = capsys.readouterr().out
        assert "3 book(s) awaiting external embedding" in out
        assert "1 marked pending_external" in out
        assert "2 derived from the index" in out

    def test_stays_quiet_when_only_the_marker_contributes(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda lib, ids: [{"id": i} for i in ids],
        )
        rag = _fake_rag(pending={"3"}, without_parents=set(), has_parents=True)

        discover_pending_external_books(rag, Path("/lib"))

        assert "derived from the index" not in capsys.readouterr().out
