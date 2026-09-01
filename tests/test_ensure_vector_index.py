"""LanceDBStore.ensure_vector_index — create the ANN index when missing.

Production indexing paths (batch_index, embed_prepared) used to refresh
only the FTS index; the IVF-PQ vector index was never built outside
``rag_demo.py create-index``. Without it every semantic/hybrid search
brute-force-scans the vector column (~5 min at 1.5M chunks on CPU), which
is what broke MCP clients with 60 s tool-call timeouts.
"""

from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from src.storage.lancedb_store import LanceDBStore


@pytest.fixture
def store(tmp_path):
    return LanceDBStore(db_path=str(tmp_path / "test_db"))


def _make_chunks(n):
    return [
        {
            "id": f"book_chunk_{i}",
            "text": f"This is test chunk {i} about interesting topics.",
            "book_id": "book",
            "chunk_index": i,
            "chunk_type": "content",
            "language": "eng",
        }
        for i in range(n)
    ]


def _random_embeddings(n, dim=64):
    vecs = np.random.randn(n, dim).astype(np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def _vector_indices(store):
    return [
        idx for idx in store.table.list_indices()
        if "vector" in list(getattr(idx, "columns", []))
    ]


class TestEnsureVectorIndex:
    def test_creates_index_when_missing(self, store):
        store.add_chunks(_make_chunks(1000), _random_embeddings(1000))
        assert _vector_indices(store) == []

        assert store.ensure_vector_index() is True
        assert len(_vector_indices(store)) == 1

    def test_noop_when_index_exists(self, store):
        store.add_chunks(_make_chunks(1000), _random_embeddings(1000))
        assert store.ensure_vector_index() is True

        assert store.ensure_vector_index() is True
        assert len(_vector_indices(store)) == 1

    def test_skips_small_tables(self, store):
        store.add_chunks(_make_chunks(10), _random_embeddings(10))

        assert store.ensure_vector_index() is False
        assert _vector_indices(store) == []

    def test_no_table_returns_false(self, store):
        assert store.ensure_vector_index() is False


class TestOptimizeIndexes:
    def test_merges_unindexed_rows_into_fts_index(self, store):
        store.add_chunks(_make_chunks(300), _random_embeddings(300))
        store.create_fts_index()
        # Rows added after the index build are unindexed until optimized —
        # every FTS/hybrid query brute-force-scans them.
        more = [
            {**c, "id": c["id"] + "_late"} for c in _make_chunks(300)
        ]
        store.add_chunks(more, _random_embeddings(300))
        assert store.table.index_stats("text_idx").num_unindexed_rows > 0

        assert store.optimize_indexes() is True
        assert store.table.index_stats("text_idx").num_unindexed_rows == 0

    def test_no_table_returns_false(self, store):
        assert store.optimize_indexes() is False

    def test_requests_cleanup_of_superseded_versions(self, store):
        # Merging rewrites the vector index in full and LanceDB keeps the
        # superseded copy until an explicit cleanup drops it. Routines call
        # optimize after every run, so an optimize() without cleanup_older_than
        # grows _indices by a full index copy per day — the production DB hit
        # 208 GB of _indices (123 dirs, ~16 GB each) behind a single live index.
        calls = []
        store.table = SimpleNamespace(optimize=lambda **kw: calls.append(kw))

        assert store.optimize_indexes(retain_days=7) is True

        assert calls == [{"cleanup_older_than": timedelta(days=7)}]

    def test_default_retention_is_two_days(self, store):
        # watchdog.py calls optimize_indexes() without arguments after every
        # run, so the default is what the production database actually gets.
        # Seven days kept a week of ~16 GB index copies: the live DB sat at
        # 133 GB with its oldest version six days old, nothing collectable.
        calls = []
        store.table = SimpleNamespace(optimize=lambda **kw: calls.append(kw))

        assert store.optimize_indexes() is True

        assert calls == [{"cleanup_older_than": timedelta(days=2)}]
