"""Befund 1.22: read-only-SQLite-Verbindungen auf Treiberebene.

ARCHILLES verspricht, nie in Calibres ``metadata.db`` oder Zoteros
``zotero.sqlite`` zu schreiben. Bisher war das nur Konvention -- alle
Verbindungen wurden read-write geoeffnet. ``connect_readonly`` erzwingt die
Grenze ueber einen ``mode=ro``-URI: ein versehentliches INSERT/UPDATE/DELETE
schlaegt dann mit ``sqlite3.OperationalError`` fehl statt die Quell-DB zu
veraendern.

Verwendung:
    conn = connect_readonly(library / "metadata.db", row_factory=sqlite3.Row)

``immutable=True`` setzt zusaetzlich ``immutable=1`` -- nur fuer DBs sinnvoll,
die sich waehrend der Verbindung garantiert nicht aendern (z. B. eine kopierte
Snapshot-DB). Bei Live-DBs (Calibre/Zotero laufen evtl. parallel) ``immutable``
weglassen, damit SQLite Aenderungen anderer Prozesse weiterhin sieht.
"""
import sqlite3
from pathlib import Path
from typing import Callable, Optional, Union


def connect_readonly(
    db_path: Union[str, Path],
    *,
    immutable: bool = False,
    busy_timeout_ms: int = 60_000,
    row_factory: Optional[Callable] = None,
) -> sqlite3.Connection:
    """Oeffnet ``db_path`` schreibgeschuetzt (``mode=ro``).

    Args:
        db_path: Pfad zur SQLite-Datei.
        immutable: zusaetzlich ``immutable=1`` (nur fuer unveraenderliche DBs;
            bei Live-DBs weglassen -- sonst ignoriert SQLite das WAL und liest
            veraltete oder halb-geschriebene Seiten, Befund 4.4).
        busy_timeout_ms: Wartezeit auf einen Lock, bevor ``SQLITE_BUSY``
            geworfen wird. 0 schaltet das Warten ab.

            60 s, nicht 5 (Befund 1.16): 5 s war auf "einen kurzzeitigen
            Schreib-Lock" ausgelegt, aber ein Zotero-Sync haelt laenger. Am
            2026-09-04 starb ein Routinelauf genau daran -- nach 21 Minuten in
            *einem* Buch (1160 s Embedding auf der T1000), also mit sehr langer
            Exposition gegenueber einer Datenbank, die der Nutzer parallel
            benutzt. Warten kostet hier nichts: der Aufrufer laeuft ohnehin
            minutenlang, waehrend ein Abbruch den ganzen Lauf verliert.
            Interaktive Pfade, die schnell scheitern wollen, setzen den Wert
            explizit herunter.
        row_factory: optionale ``row_factory`` (z. B. ``sqlite3.Row``).

    Returns:
        Eine read-only ``sqlite3.Connection``. Schreibversuche werfen
        ``sqlite3.OperationalError``.
    """
    # as_posix() liefert Forward-Slashes inkl. Windows-Laufwerksbuchstaben
    # (D:/...), die als file:-URI verlaesslich parsen.
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    if immutable:
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    # Set explicitly (even 0) — Python's sqlite3.connect already defaults
    # timeout=5.0s, so 0 must override that to genuinely disable waiting.
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn
