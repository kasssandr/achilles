"""Tests for the --prepare-pending-external discovery path (Hardware-Tiers-V2 §12).

The discovery half of the trickle lifecycle: find books marked pending_external
in LanceDB, resolve them to Calibre book dicts, and (in main) re-prepare them
hierarchically so the existing ``embed`` step can replace the provisional flat
chunks with externally embedded ones.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from scripts.batch_index import build_parser, discover_pending_external_books


class TestParserFlag:
    def test_flag_defaults_false(self):
        args = build_parser().parse_args(["--all"])
        assert args.prepare_pending_external is False

    def test_flag_can_be_set_standalone(self):
        args = build_parser().parse_args(["--prepare-pending-external"])
        assert args.prepare_pending_external is True


class TestDiscovery:
    def test_resolves_numeric_pending_ids_sorted(self, monkeypatch):
        # Flat index: the marker is the only source of truth here, which is
        # what this test is about (the union is covered by finding 1.2's own
        # tests in test_external_catchup_discovery.py).
        store = SimpleNamespace(
            get_pending_external_book_ids=lambda: {"7", "3", "folder:9"},
            has_parent_chunks=lambda: False,
            get_book_ids_without_parent_chunks=lambda: set(),
        )
        rag = SimpleNamespace(store=store)
        seen = {}

        def fake_get_books_by_ids(library_path, ids):
            seen["ids"] = ids
            return [{"id": i} for i in ids]

        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids", fake_get_books_by_ids
        )
        books = discover_pending_external_books(rag, Path("/lib"))

        # Non-numeric (folder:9) skipped; numeric ids sorted ascending
        assert seen["ids"] == [3, 7]
        assert len(books) == 2

    def test_empty_pending_returns_empty_without_db_call(self, monkeypatch):
        rag = SimpleNamespace(
            store=SimpleNamespace(
                get_pending_external_book_ids=lambda: set(),
                has_parent_chunks=lambda: False,
                get_book_ids_without_parent_chunks=lambda: set(),
            )
        )
        called = []
        monkeypatch.setattr(
            "scripts.batch_index.get_books_by_ids",
            lambda library_path, ids: called.append(ids) or [],
        )
        assert discover_pending_external_books(rag, Path("/lib")) == []
        assert called == []

    def test_non_numeric_ids_resolved_via_adapter(self, monkeypatch):
        """Finding 4.2: with a non-Calibre adapter, non-numeric pending ids
        (Zotero keys) are resolved via the adapter instead of being dropped."""
        rag = SimpleNamespace(
            store=SimpleNamespace(
                get_pending_external_book_ids=lambda: {"ZK1", "ZK3"},
                has_parent_chunks=lambda: False,
                get_book_ids_without_parent_chunks=lambda: set(),
            )
        )
        adapter = SimpleNamespace(adapter_type="zotero")
        fake_books = [{"id": "ZK1"}, {"id": "ZK2"}, {"id": "ZK3"}]
        monkeypatch.setattr(
            "scripts.batch_index._adapter_list_books", lambda a: fake_books
        )
        books = discover_pending_external_books(rag, Path("/lib"), adapter=adapter)
        assert sorted(b["id"] for b in books) == ["ZK1", "ZK3"]


def _write_jsonl(tmp_path, book_id, calibre_id, n=2, chunk_type="child"):
    header = {"_header": True, "calibre_id": calibre_id, "book_id": book_id,
              "book_metadata": {}, "chunk_count": n, "prepared_at": "x"}
    jsonl = tmp_path / f"{calibre_id}.jsonl"
    with open(jsonl, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for i in range(n):
            f.write(json.dumps({
                "id": f"{book_id}_chunk_{i}", "text": f"text {i}",
                "book_id": book_id, "chunk_index": i, "chunk_type": chunk_type,
            }) + "\n")
    return jsonl


def _fake_encode(texts, show_progress_bar=False, convert_to_numpy=True):
    n = len(texts) if isinstance(texts, list) else 1
    return np.zeros((n, 1024), dtype=np.float32)


class _RecordingStore:
    """Minimal store stand-in recording delete/add calls for embed_prepared."""

    def __init__(self, *, pending, has_content):
        self._pending = pending
        self._has_content = has_content
        self.deleted: list[str] = []
        self.added: list[int] = []
        self.fts_refreshed = 0

    def get_pending_external_book_ids(self):
        return set(self._pending)

    def get_book_state(self, book_id):
        return {"total": 1 if self._has_content else 0,
                "has_content": self._has_content,
                "content_count": 1 if self._has_content else 0,
                "metadata_hash": "", "annotation_hash": "", "format": ""}

    def add_chunks(self, chunks, embeddings):
        self.added.append(len(chunks))
        return len(chunks)

    def delete_by_book_id_except_annotations(self, book_id):
        self.deleted.append(book_id)
        return 2

    def clear_pending_external(self, book_id):
        return 0

    def create_fts_index(self):
        self.fts_refreshed += 1


def _embed(tmp_path, store):
    from src.archilles.engine.indexing import Indexer

    rag = SimpleNamespace(
        embedding_model=SimpleNamespace(encode=_fake_encode),
        store=store, device="cpu", batch_size=16,
    )
    return Indexer(rag).embed_prepared(str(tmp_path), mode="local", force=False)


class TestEmbedAutoReplacesPending:
    """Chosen UX (§12): embed auto-force-replaces books currently marked
    pending_external — the new hierarchical chunks carry no marker, so the
    replacement clears it. The user runs the normal `embed` step, no --force."""

    def test_pending_book_replaced_without_force(self, tmp_path):
        book_id = "Prov_Book_7"
        _write_jsonl(tmp_path, book_id, 7)
        store = _RecordingStore(pending={book_id}, has_content=True)

        result = _embed(tmp_path, store)

        assert result["total_books"] == 1   # NOT skipped despite existing content
        assert store.deleted == [book_id]    # provisional flat chunks replaced
        assert store.added == [2]

    def test_non_pending_content_book_still_skipped(self, tmp_path):
        book_id = "Final_Book_8"
        _write_jsonl(tmp_path, book_id, 8)
        store = _RecordingStore(pending=set(), has_content=True)

        result = _embed(tmp_path, store)

        assert result["total_books"] == 0   # already final content → skipped
        assert store.deleted == []
