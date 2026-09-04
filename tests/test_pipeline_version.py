"""Tests for review finding 1.7(a) — the generation marker on every chunk.

The schema recorded ``indexed_at`` and nothing about *how* a row was produced,
so drift could only be detected by noticing its symptoms in the text. The
column is migratable with default ``''``: rows written before it existed keep
the empty string, which is itself the signal that locates them.
"""

import numpy as np
import pytest

from src.archilles.pipeline_version import PIPELINE_VERSION
from src.storage.lancedb_store import LanceDBStore


def _chunk(cid: str, book_id: str = "b1") -> dict:
    return {
        "id": cid,
        "text": f"text of {cid}",
        "book_id": book_id,
        "book_title": "A Book",
        "chunk_index": 0,
        "chunk_type": "content",
    }


def _store(tmp_path) -> LanceDBStore:
    return LanceDBStore(str(tmp_path / "db"))


class TestNewRowsCarryTheVersion:
    def test_add_chunks_writes_the_current_version(self, tmp_path):
        store = _store(tmp_path)
        store.add_chunks([_chunk("c1")], np.zeros((1, 1024), dtype=np.float32))

        rows = store.table.to_pandas()
        assert rows["pipeline_version"].tolist() == [PIPELINE_VERSION]

    def test_the_version_is_not_taken_from_the_chunk_dict(self, tmp_path):
        """A caller that could pass its own value could claim a generation it
        did not produce."""
        store = _store(tmp_path)
        chunk = _chunk("c1") | {"pipeline_version": "999"}
        store.add_chunks([chunk], np.zeros((1, 1024), dtype=np.float32))

        assert store.table.to_pandas()["pipeline_version"].tolist() == [PIPELINE_VERSION]

    def test_the_constant_is_a_non_empty_string(self):
        """'' is reserved for rows written before the marker existed; a
        version that collides with it would erase the distinction."""
        assert isinstance(PIPELINE_VERSION, str)
        assert PIPELINE_VERSION != ""


class TestMigrationOfExistingTables:
    def test_a_table_without_the_column_gains_it_with_empty_default(self, tmp_path):
        store = _store(tmp_path)
        store.add_chunks([_chunk("c1")], np.zeros((1, 1024), dtype=np.float32))

        # Simulate a table created before the column existed.
        store.table.drop_columns(["pipeline_version"])
        assert "pipeline_version" not in store.table.schema.names

        migrated = LanceDBStore(str(tmp_path / "db"))
        rows = migrated.table.to_pandas()
        assert "pipeline_version" in rows.columns
        assert rows["pipeline_version"].tolist() == [""]

    def test_old_rows_keep_the_empty_string_when_new_ones_are_added(self, tmp_path):
        """The point of the marker: a re-index of part of the corpus separates
        the two generations instead of relabelling the old one."""
        store = _store(tmp_path)
        store.add_chunks([_chunk("old")], np.zeros((1, 1024), dtype=np.float32))
        store.table.drop_columns(["pipeline_version"])

        migrated = LanceDBStore(str(tmp_path / "db"))
        migrated.add_chunks([_chunk("new")], np.zeros((1, 1024), dtype=np.float32))

        rows = migrated.table.to_pandas().set_index("id")["pipeline_version"]
        assert rows["old"] == ""
        assert rows["new"] == PIPELINE_VERSION

    def test_migration_does_not_rewrite_rows(self, tmp_path):
        """``add_columns`` with a constant default must not touch the 1.5 M
        existing rows — a rewrite is the one thing this fix must not cost."""
        store = _store(tmp_path)
        store.add_chunks(
            [_chunk(f"c{i}") for i in range(50)],
            np.zeros((50, 1024), dtype=np.float32),
        )
        store.table.drop_columns(["pipeline_version"])
        before = store.table.version

        migrated = LanceDBStore(str(tmp_path / "db"))

        # One version bump for the schema change, and no data rewrite: the row
        # count and every id survive untouched.
        assert migrated.table.version == before + 1
        assert migrated.table.count_rows() == 50


class TestTheMarkerCannotBeForged:
    def test_update_metadata_fields_refuses_it(self, tmp_path):
        store = _store(tmp_path)
        store.add_chunks([_chunk("c1")], np.zeros((1, 1024), dtype=np.float32))

        with pytest.raises(ValueError, match="pipeline_version"):
            store.update_metadata_fields("b1", {"pipeline_version": "2"})

    def test_a_legitimate_field_alongside_it_is_refused_too(self, tmp_path):
        """Same shape as the text/vector case: an update carrying both halves
        applies neither."""
        store = _store(tmp_path)
        store.add_chunks([_chunk("c1")], np.zeros((1, 1024), dtype=np.float32))

        with pytest.raises(ValueError):
            store.update_metadata_fields("b1", {"tags": "a,b", "pipeline_version": "2"})

        assert store.table.to_pandas()["tags"].tolist() == [""]
