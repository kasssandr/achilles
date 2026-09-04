"""Finding 1.1 startup hint: warn when a `light` plan runs over an index that
already holds hierarchical (parent) chunks — new titles would be flat/unmarked.
"""

from types import SimpleNamespace

import numpy as np

from src.archilles.constants import ChunkType
from src.archilles.execution import warn_if_light_plan_hides_hierarchy
from src.storage.lancedb_store import LanceDBStore


def _emb(n, dim=1024):
    v = np.random.randn(n, dim).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _chunk(i, chunk_type):
    return {
        "id": f"c{i}", "text": f"text {i}", "book_id": "bk",
        "chunk_index": i, "chunk_type": chunk_type,
    }


class TestHasParentChunks:
    def test_empty_store_is_false(self, tmp_path):
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        assert store.has_parent_chunks() is False

    def test_flat_index_is_false(self, tmp_path):
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks([_chunk(0, ChunkType.CONTENT)], _emb(1))
        assert store.has_parent_chunks() is False

    def test_hierarchical_index_is_true(self, tmp_path):
        store = LanceDBStore(db_path=str(tmp_path / "db"))
        store.add_chunks(
            [_chunk(0, ChunkType.PARENT), _chunk(1, ChunkType.CHILD)], _emb(2)
        )
        assert store.has_parent_chunks() is True


class TestWarnLightPlanHidesHierarchy:
    def test_warns_on_light_over_hierarchical(self, capsys, caplog):
        """Logged, not printed (review 1.3): this fires on every scan, and an
        MCP-driven scan writes JSON-RPC on stdout."""
        store = SimpleNamespace(has_parent_chunks=lambda: True)
        with caplog.at_level("WARNING"):
            warned = warn_if_light_plan_hides_hierarchy(
                SimpleNamespace(mode="light"), store
            )
        assert warned is True
        assert "full-external" in caplog.text
        assert capsys.readouterr().out == ""

    def test_silent_when_plan_not_light(self, capsys):
        store = SimpleNamespace(has_parent_chunks=lambda: True)
        warned = warn_if_light_plan_hides_hierarchy(
            SimpleNamespace(mode="full-local"), store
        )
        assert warned is False
        assert capsys.readouterr().out == ""

    def test_silent_when_no_parent_chunks(self, capsys):
        store = SimpleNamespace(has_parent_chunks=lambda: False)
        warned = warn_if_light_plan_hides_hierarchy(SimpleNamespace(mode="light"), store)
        assert warned is False
        assert capsys.readouterr().out == ""

    def test_store_error_is_swallowed(self, capsys):
        def boom():
            raise RuntimeError("db down")

        store = SimpleNamespace(has_parent_chunks=boom)
        warned = warn_if_light_plan_hides_hierarchy(SimpleNamespace(mode="light"), store)
        assert warned is False
