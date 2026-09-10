"""The three provenance columns: region, label_source, producer_version.

A Scriptor chunk carries the producer's signal beside Archilles' reading of it:
the region name next to section_type, the witness behind page_label, the spec
version of the master. Stored as columns, a later change of the mapping is an
update_metadata_fields run instead of a re-index. All three default to '' --
every row not written from a Scriptor bundle, and every row older than them.
"""

from types import SimpleNamespace

import numpy as np
import pytest

from src.storage.lancedb_store import LanceDBStore

COLUMNS = ["region", "label_source", "producer_version"]


def _chunk(cid, book_id="b1", **extra):
    return {"id": cid, "text": f"text of {cid}", "book_id": book_id,
            "book_title": "A Book", "chunk_index": 0, "chunk_type": "content", **extra}


def _vectors(n):
    return np.zeros((n, 1024), dtype=np.float32)


def _store(tmp_path):
    return LanceDBStore(str(tmp_path / "db"))


def test_a_scriptor_chunk_keeps_its_provenance_in_the_row(tmp_path):
    store = _store(tmp_path)
    store.add_chunks([_chunk("c1", region="bibliography", label_source="printed",
                             producer_version="0.3.0")], _vectors(1))
    row = store.get_by_id("c1")
    assert (row["region"], row["label_source"], row["producer_version"]) == (
        "bibliography", "printed", "0.3.0")


def test_any_other_chunk_carries_empty_strings(tmp_path):
    store = _store(tmp_path)
    store.add_chunks([_chunk("c1")], _vectors(1))
    row = store.get_by_id("c1")
    assert [row[c] for c in COLUMNS] == ["", "", ""]


def test_a_table_without_the_columns_gains_them_and_keeps_its_rows(tmp_path):
    store = _store(tmp_path)
    store.add_chunks([_chunk("c1"), _chunk("c2")], _vectors(2))
    store.table.drop_columns(COLUMNS)
    assert not set(COLUMNS) & set(store.table.schema.names)

    migrated = LanceDBStore(str(tmp_path / "db"))
    rows = migrated.table.to_pandas().set_index("id")
    assert rows.loc["c2", "text"] == "text of c2"
    assert [rows.loc["c1", c] for c in COLUMNS] == ["", "", ""]


def test_update_metadata_fields_refuses_producer_version(tmp_path):
    """Like pipeline_version: a claim about who wrote the row. Only the write
    that composed the row from the master can make it."""
    store = _store(tmp_path)
    store.add_chunks([_chunk("c1", producer_version="0.3.0")], _vectors(1))
    with pytest.raises(ValueError, match="producer_version"):
        store.update_metadata_fields("b1", {"producer_version": "0.4.0"})
    assert store.get_by_id("c1")["producer_version"] == "0.3.0"


def test_stats_show_the_population_by_producer_version(tmp_path):
    store = _store(tmp_path)
    store.add_chunks([_chunk("c1", producer_version="0.3.0"),
                      _chunk("c2", producer_version="0.3.0"),
                      _chunk("c3", book_id="b2")], _vectors(3))
    assert store.get_stats()["producer_versions"] == {"0.3.0": 2, "": 1}


def test_the_indexer_carries_them_from_the_extractor_to_the_row(tmp_path):
    """index_book and prepare_book both build their rows through
    _build_chunk_dicts; what it drops, the store never sees."""
    from src.archilles.engine.core import ArchillesRAG
    from src.archilles.engine.indexing import Indexer
    from src.extractors.scriptor_extractor import ScriptorExtractor

    master = tmp_path / "book.md"
    master.write_text("---\nformat_version: 0.3.0\nchunking_strategy: basic\n---\n\n"
                      "[region: bibliography]\n\n[p. 3] Müller, Hans: Ein Titel.\n",
                      encoding="utf-8")
    (tmp_path / "book.md.pagination.json").write_text(
        '{"version": 1, "pages": [{"pos": 9, "label": "3", "source": "printed"}]}',
        encoding="utf-8")
    extracted = ScriptorExtractor().extract(master)

    indexer = Indexer(SimpleNamespace(_CHUNK_META_KEYS=ArchillesRAG._CHUNK_META_KEYS))
    chunks = indexer._build_chunk_dicts(extracted, "b1", {}, "2026-09-11T00:00:00", "")
    store = _store(tmp_path)
    store.add_chunks(chunks, _vectors(len(chunks)))

    row = store.get_by_id(chunks[0]["id"])
    assert (row["region"], row["label_source"], row["producer_version"],
            row["page_label"], row["page_number"], row["format"]) == (
        "bibliography", "printed", "0.3.0", "3", 9, "scriptor")
