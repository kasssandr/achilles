"""Befund 1.22: read-only-SQLite-Helper erzwingt die Calibre/Zotero-Grenze.

Die Read-only-Grenze ("ARCHILLES schreibt nie in Calibre/Zotero") war bisher
nur Konvention. ``connect_readonly`` erzwingt sie auf Treiberebene via
``mode=ro``-URI, sodass jeder Schreibversuch hart fehlschlaegt.
"""
import sqlite3

import pytest

from src.archilles.sqlite_ro import connect_readonly


@pytest.fixture
def db(tmp_path):
    """Eine kleine echte SQLite-DB mit einer Zeile."""
    path = tmp_path / "metadata.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO books (id, title) VALUES (1, 'Sein und Zeit')")
    con.commit()
    con.close()
    return path


class TestConnectReadonly:
    def test_select_works(self, db):
        conn = connect_readonly(db)
        try:
            row = conn.execute("SELECT title FROM books WHERE id = 1").fetchone()
            assert row[0] == "Sein und Zeit"
        finally:
            conn.close()

    def test_write_is_rejected(self, db):
        conn = connect_readonly(db)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("UPDATE books SET title = 'x' WHERE id = 1")
        finally:
            conn.close()

    def test_insert_is_rejected(self, db):
        conn = connect_readonly(db)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO books (id, title) VALUES (2, 'y')")
        finally:
            conn.close()

    def test_row_factory_applied(self, db):
        conn = connect_readonly(db, row_factory=sqlite3.Row)
        try:
            row = conn.execute("SELECT title FROM books WHERE id = 1").fetchone()
            assert row["title"] == "Sein und Zeit"
        finally:
            conn.close()

    def test_accepts_path_and_str(self, db):
        # Sowohl pathlib.Path als auch str muessen funktionieren.
        for arg in (db, str(db)):
            conn = connect_readonly(arg)
            try:
                assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
            finally:
                conn.close()

    def test_immutable_still_reads(self, db):
        # immutable=1 ist fuer statische DBs gedacht; SELECT muss weiter klappen.
        conn = connect_readonly(db, immutable=True)
        try:
            assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
        finally:
            conn.close()


# ── Befund 4.4: Live-DBs ohne immutable, mit busy_timeout ────────────


class TestBusyTimeout:
    def test_default_busy_timeout_is_set(self, db):
        """60 s, not 5 (finding 1.16): a Zotero sync holds its write lock far
        longer than "a brief writer lock", and a run that waits loses nothing
        while a run that gives up loses everything."""
        conn = connect_readonly(db)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        finally:
            conn.close()

    def test_busy_timeout_can_be_overridden(self, db):
        conn = connect_readonly(db, busy_timeout_ms=1234)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        finally:
            conn.close()

    def test_busy_timeout_zero_leaves_it_off(self, db):
        conn = connect_readonly(db, busy_timeout_ms=0)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 0
        finally:
            conn.close()


class TestLiveWalReads:
    """4.4: dropping immutable=1 means a live (WAL) DB — as Zotero uses while
    running — is read as a consistent, up-to-date snapshot instead of ignoring
    the WAL and returning stale/torn pages."""

    def test_mode_ro_reflects_committed_writes_via_wal(self, tmp_path):
        path = tmp_path / "zotero.sqlite"
        writer = sqlite3.connect(path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO items VALUES (1)")
        writer.commit()

        # writer stays open (keeps the WAL live, as a running Zotero would)
        ro = connect_readonly(path)  # no immutable
        try:
            assert ro.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1

            # concurrent committed write lands in the WAL
            writer.execute("INSERT INTO items VALUES (2)")
            writer.commit()

            # mode=ro sees it; immutable=1 would ignore the WAL and miss it
            assert ro.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
        finally:
            ro.close()
            writer.close()
