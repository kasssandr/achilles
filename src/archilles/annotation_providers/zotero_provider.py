"""
Zotero Annotation Provider.

Reads annotations and notes stored in Zotero's SQLite database.

Two annotation types are extracted:
  itemAnnotations  — PDF highlights/underlines/notes made in the Zotero PDF viewer
  itemNotes        — standalone notes attached to a Zotero item

CRITICAL: The database is opened in read-only mode.
"""

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.archilles.html_text import strip_html as _strip_html
from src.archilles.sqlite_ro import connect_readonly
from .base import Annotation, AnnotationProvider

logger = logging.getLogger(__name__)

# Zotero annotation type → unified type.
#
# Zotero stores this column as an INTEGER (measured: every row in the live
# library carries `type=1`), while some versions and exports use the name. The
# map holds both, because a string-only lookup silently degrades every
# annotation to "highlight" — which is what happened here until the adapter
# path went live and made the raw `1` visible in the chunks.
_ANNOT_TYPE_MAP = {
    "highlight": "highlight",
    "underline": "highlight",
    "note": "note",
    "image": "bookmark",
    "ink": "bookmark",
    1: "highlight",
    2: "note",
    3: "bookmark",   # image
    4: "bookmark",   # ink
    5: "highlight",  # underline
}

# sortIndex format: "PPPPP|CCCCC|LLLLL"
_SORT_INDEX_RE = re.compile(r"^(\d+)\|")


def zotero_annotation_type(raw) -> str:
    """Map Zotero's annotation type — integer or name — onto a unified name.

    Unknown values fall back to ``"highlight"``, the overwhelmingly common
    case and the behaviour every caller had before.
    """
    if isinstance(raw, str):
        raw = raw.strip().lower()
    return _ANNOT_TYPE_MAP.get(raw, "highlight")


def zotero_page_number(sort_index: str, page_label: str = "") -> Optional[int]:
    """The page a reader would cite, or ``None`` when Zotero knows neither.

    ``pageLabel`` is the *printed* page number and wins: on real data the two
    diverge (label ``159`` at sortIndex ``00000``), and a citation that names
    the physical page of a PDF is wrong for anything with front matter. A
    non-numeric label (roman numerals, "Cover") is not a number we can use, so
    the physical page stands in.
    """
    label = (page_label or "").strip()
    if label.isdigit():
        return int(label)
    physical = _parse_sort_index_page(sort_index)
    return physical + 1 if physical is not None else None


def _parse_sort_index_page(sort_index: str) -> Optional[int]:
    """Extract the 0-based page number from Zotero's sortIndex."""
    m = _SORT_INDEX_RE.match(sort_index or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _parse_zotero_date(date_str: str) -> Optional[datetime]:
    """Parse Zotero's ISO-ish timestamp (e.g. '2024-03-15 14:22:33')."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(date_str[:19], fmt)
        except ValueError:
            continue
    return None


class ZoteroAnnotationProvider(AnnotationProvider):
    """Extract annotations and notes from a Zotero SQLite database.

    Parameters
    ----------
    path:
        Path to ``zotero.sqlite``. Passed either via constructor or as the
        ``path`` argument to :meth:`extract`.
    """

    def __init__(self, path: Optional[str] = None):
        self._default_path = path

    @property
    def name(self) -> str:
        return "zotero"

    def can_handle(self, path: str) -> bool:
        return Path(path).name == "zotero.sqlite" and Path(path).is_file()

    def extract(self, path: str, **kwargs) -> list[Annotation]:
        db_path = Path(path or self._default_path or "")
        if not db_path.is_file():
            logger.error("zotero.sqlite not found: %s", db_path)
            return []

        # Live DB: no immutable (Zotero may be writing) — mode=ro reads a
        # consistent WAL snapshot, busy_timeout absorbs a brief lock (4.4).
        conn = connect_readonly(db_path, row_factory=sqlite3.Row)
        try:
            return self._extract_all(conn)
        finally:
            conn.close()

    # ── Internal ────────────────────────────────────────────────────

    def _item_title_author(
        self, conn: sqlite3.Connection, item_id: int
    ) -> tuple[str, str]:
        """Return (title, first_author) for an item."""
        title_row = conn.execute(
            """
            SELECT idv.value FROM itemData id
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            JOIN fields f ON id.fieldID = f.fieldID
            WHERE id.itemID = ? AND f.fieldName = 'title'
            """,
            (item_id,),
        ).fetchone()
        title = title_row[0] if title_row else ""

        author_row = conn.execute(
            """
            SELECT c.firstName, c.lastName FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID = ? AND ct.creatorType = 'author'
            ORDER BY ic.orderIndex LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        if author_row:
            first, last = author_row[0] or "", author_row[1] or ""
            author = f"{first} {last}".strip() if first else last
        else:
            author = ""

        return title, author

    def _item_key(self, conn: sqlite3.Connection, item_id: int) -> str:
        row = conn.execute("SELECT key FROM items WHERE itemID = ?", (item_id,)).fetchone()
        return row[0] if row else ""

    def _item_info(
        self, conn: sqlite3.Connection, item_id: int,
        cache: dict[int, tuple[str, str, str]],
    ) -> tuple[str, str, str]:
        """Return (title, author, key), memoized per extraction run.

        Hundreds of annotations typically share a handful of parent items;
        without the cache every annotation row costs three point queries.
        """
        if item_id not in cache:
            title, author = self._item_title_author(conn, item_id)
            cache[item_id] = (title, author, self._item_key(conn, item_id))
        return cache[item_id]

    def _extract_all(self, conn: sqlite3.Connection) -> list[Annotation]:
        item_cache: dict[int, tuple[str, str, str]] = {}
        annotations: list[Annotation] = []
        annotations.extend(self._extract_pdf_annotations(conn, item_cache))
        annotations.extend(self._extract_notes(conn, item_cache))
        return annotations

    def _extract_pdf_annotations(
        self, conn: sqlite3.Connection,
        item_cache: dict[int, tuple[str, str, str]],
    ) -> list[Annotation]:
        """Read itemAnnotations (highlights/notes in Zotero PDF viewer)."""
        rows = conn.execute(
            """
            SELECT
                ia.itemID,
                ia.parentItemID    AS attachment_id,
                ia.type,
                ia.text,
                ia.comment,
                ia.sortIndex,
                ia.pageLabel,
                ia.dateModified,
                att.parentItemID   AS parent_item_id
            FROM itemAnnotations ia
            JOIN itemAttachments att ON ia.parentItemID = att.itemID
            WHERE ia.itemID     NOT IN (SELECT itemID FROM deletedItems)
            AND   att.itemID    NOT IN (SELECT itemID FROM deletedItems)
            AND   att.parentItemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        annotations = []
        for row in rows:
            unified_type = zotero_annotation_type(row["type"])
            text = row["text"] or ""
            comment = row["comment"] or ""
            if not text and not comment:
                continue

            page_label = row["pageLabel"] or ""
            page_number = zotero_page_number(row["sortIndex"] or "", page_label)

            parent_item_id = row["parent_item_id"]
            if parent_item_id:
                title, author, doc_id = self._item_info(conn, parent_item_id, item_cache)
            else:
                title, author, doc_id = "", "", ""

            annotations.append(
                Annotation(
                    source="zotero",
                    type=unified_type,
                    text=text,
                    note=comment or None,
                    location=f"page:{page_label or page_number or ''}",
                    page_number=page_number,
                    created_at=_parse_zotero_date(row["dateModified"]),
                    book_title=title or None,
                    book_author=author or None,
                    source_doc_id=doc_id or None,
                )
            )
        return annotations

    def _extract_notes(
        self, conn: sqlite3.Connection,
        item_cache: dict[int, tuple[str, str, str]],
    ) -> list[Annotation]:
        """Read itemNotes (standalone HTML notes attached to Zotero items)."""
        rows = conn.execute(
            """
            SELECT n.itemID, n.parentItemID, n.note, i.dateModified
            FROM itemNotes n
            JOIN items i ON n.itemID = i.itemID
            WHERE n.parentItemID IS NOT NULL
            AND n.itemID NOT IN (SELECT itemID FROM deletedItems)
            """
        ).fetchall()

        annotations = []
        for row in rows:
            text = _strip_html(row["note"] or "")
            if not text:
                continue

            parent_item_id = row["parentItemID"]
            title, author, doc_id = self._item_info(conn, parent_item_id, item_cache)

            annotations.append(
                Annotation(
                    source="zotero",
                    type="note",
                    text=text,
                    created_at=_parse_zotero_date(row["dateModified"]),
                    book_title=title or None,
                    book_author=author or None,
                    source_doc_id=doc_id or None,
                )
            )
        return annotations
