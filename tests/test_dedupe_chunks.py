"""Tests for scripts/dedupe_chunks.py.

The cleanup removes rows LanceDB cannot remove on its own: copies sharing a
chunk id (written before the finding-8.7 fix deleted old rows first) and rows
left over from an earlier run of the same book, whose positional ids the
current run no longer writes.

Both operations delete from the live index, so the three properties that keep
them safe are pinned here: the newest row wins, a row of a different
chunk_type sharing an id survives, and a book indexed only once is untouched.
"""

import numpy as np
import pytest

from scripts.dedupe_chunks import (
    dedupe,
    drop_stale,
    load_rows,
    plan_dedupe,
    plan_stale,
)


@pytest.fixture
def table(tmp_path):
    import lancedb

    db = lancedb.connect(str(tmp_path / "db"))
    return db.create_table("chunks", data=[_row("seed_0", "seed", "2026-01-01T00:00:00", "seed")])


def _row(chunk_id, text, indexed_at, book_id, chunk_type="calibre_comment"):
    return {
        "id": chunk_id,
        "text": text,
        "vector": np.random.rand(4).astype("float32").tolist(),
        "book_id": book_id,
        "chunk_type": chunk_type,
        "indexed_at": indexed_at,
    }


def _texts(table, chunk_type=None):
    rows = table.search().select(["id", "text", "chunk_type"]).limit(0).to_list()
    return {
        (r["id"], r["text"])
        for r in rows
        if chunk_type is None or r["chunk_type"] == chunk_type
    }


def test_keeps_the_newest_row_per_id(table, tmp_path):
    table.add([
        _row("1_comment_0", "old", "2026-02-01T00:00:00", "1"),
        _row("1_comment_0", "newest", "2026-05-01T00:00:00", "1"),
        _row("1_comment_0", "middle", "2026-03-01T00:00:00", "1"),
        _row("1_comment_1", "only copy", "2026-02-01T00:00:00", "1"),
    ])

    plan = plan_dedupe(load_rows(table, "calibre_comment"))
    assert plan["duplicate_ids"] == ["1_comment_0"]
    assert plan["excess_rows"] == 2

    dedupe(table, plan["duplicate_ids"], 200, tmp_path / "cp.json", "calibre_comment")

    assert ("1_comment_0", "newest") in _texts(table)
    assert ("1_comment_1", "only copy") in _texts(table)
    assert len([r for r in load_rows(table, "calibre_comment") if r["id"] == "1_comment_0"]) == 1


def test_row_of_another_chunk_type_sharing_an_id_survives(table, tmp_path):
    """Ids are unique per type, so the delete must be pinned to the type.

    Without that filter the `id IN (...)` delete takes the content row with
    it and the re-insert, which only holds the annotation row, never brings
    it back.
    """
    table.add([
        _row("7_x", "annot old", "2026-02-01T00:00:00", "7", "annotation"),
        _row("7_x", "annot new", "2026-05-01T00:00:00", "7", "annotation"),
        _row("7_x", "content row", "2026-03-01T00:00:00", "7", "content"),
    ])

    plan = plan_dedupe(load_rows(table, "annotation"))
    dedupe(table, plan["duplicate_ids"], 200, tmp_path / "cp.json", "annotation")

    assert ("7_x", "content row") in _texts(table, "content")
    assert _texts(table, "annotation") == {("7_x", "annot new")}


def test_stale_rows_of_a_shortened_comment_are_found_and_dropped(table, tmp_path):
    """A February run wrote five chunks, the May run only three.

    `_comment_3` and `_comment_4` are not duplicates - no id repeats - but
    they hold text the comment no longer contains.
    """
    for n in range(5):
        table.add([_row(f"A_comment_{n}", f"old {n}", f"2026-02-01T10:0{n}:00", "A")])
    for n in range(3):
        table.add([_row(f"A_comment_{n}", f"new {n}", f"2026-05-01T10:0{n}:00", "A")])

    rows = load_rows(table, "calibre_comment")
    plan = plan_dedupe(rows)
    stale = plan_stale(rows, gap_minutes=15)

    assert stale["stale_ids"] == ["A_comment_3", "A_comment_4"]

    dedupe(table, plan["duplicate_ids"], 200, tmp_path / "cp.json", "calibre_comment")
    drop_stale(table, stale["stale_ids"], 200, "calibre_comment", tmp_path / "stale.parquet")

    assert _texts(table, "calibre_comment") == {
        ("A_comment_0", "new 0"),
        ("A_comment_1", "new 1"),
        ("A_comment_2", "new 2"),
        ("seed_0", "seed"),
    }


def test_timestamps_minutes_apart_are_one_run(table):
    """Writing a few thousand chunks spans minutes; that is not two runs."""
    for n in range(4):
        table.add([_row(f"B_comment_{n}", f"b {n}", f"2026-04-01T10:0{n}:00", "B")])

    assert plan_stale(load_rows(table, "calibre_comment"), gap_minutes=15)["stale_ids"] == []


def test_stale_backup_holds_every_deleted_row(table, tmp_path):
    import pyarrow.parquet as pq

    table.add([
        _row("C_comment_0", "kept", "2026-05-01T10:00:00", "C"),
        _row("C_comment_1", "stale", "2026-02-01T10:00:00", "C"),
    ])
    stale = plan_stale(load_rows(table, "calibre_comment"), gap_minutes=15)
    backup = tmp_path / "stale.parquet"

    deleted = drop_stale(table, stale["stale_ids"], 200, "calibre_comment", backup)

    assert deleted == 1
    saved = pq.read_table(backup).to_pylist()
    assert [(r["id"], r["text"]) for r in saved] == [("C_comment_1", "stale")]
    assert "vector" in pq.read_table(backup).schema.names
