"""
Zotero adapter — read-only access to Zotero libraries via zotero.sqlite.

CRITICAL: The database is opened in read-only mode. Zotero uses a caching
layer that bypasses normal SQLite locking — writing while Zotero is running
would corrupt the database.
"""

import logging
import re
import sqlite3
from pathlib import Path

from src.adapters.base import (
    DocumentAnnotation,
    DocumentMetadata,
    DocumentTimestamps,
    SourceAdapter,
)
from src.archilles.html_text import strip_html as _strip_html
from src.archilles.annotation_providers.zotero_provider import (
    zotero_annotation_type,
    zotero_page_number,
)
from src.archilles.sqlite_ro import connect_readonly

logger = logging.getLogger(__name__)

# Zotero itemTypeIDs to exclude (not user-facing documents)
_EXCLUDED_TYPE_IDS = {1, 3, 27}  # annotation, attachment, note

# Content-type to file format mapping
_CONTENT_TYPE_MAP = {
    "application/pdf": "pdf",
    "application/epub+zip": "epub",
    "text/html": "html",
    "text/plain": "txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}

# Preferred attachment formats (higher = better)
_FORMAT_PRIORITY = {"pdf": 10, "epub": 8, "html": 3, "txt": 2}


def _parse_year(date_str: str) -> int | None:
    """Extract a 4-digit year from Zotero's date field."""
    if not date_str:
        return None
    m = re.search(r'\b(\d{4})\b', date_str)
    return int(m.group(1)) if m else None


def _parse_extra(extra: str) -> dict[str, str | int]:
    """Parse Zotero's free-form 'extra' field into key-value pairs.

    Lines matching "Key: Value" are extracted; all others are ignored.
    Keys are lowercased and spaces replaced by underscores.
    'rating' is converted to int if the value is a plain digit 0-9.
    """
    result: dict[str, str | int] = {}
    for line in extra.splitlines():
        m = re.match(r'^([^:]+):\s*(.+)$', line.strip())
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        val: str | int = m.group(2).strip()
        if key == "rating" and isinstance(val, str) and val.isdigit():
            val = int(val)
        result[key] = val
    return result


class ZoteroAdapter(SourceAdapter):
    """Read-only adapter for Zotero libraries (zotero.sqlite).

    Parameters
    ----------
    library_path:
        Zotero Data Directory containing ``zotero.sqlite`` and ``storage/``.
    linked_attachment_base:
        Base directory for linked attachments (Zotero pref
        ``extensions.zotero.baseAttachmentPath``). Only needed if the
        library contains linked files (linkMode=2).
    """

    def __init__(
        self,
        library_path: Path,
        linked_attachment_base: Path | None = None,
    ):
        self._library_path = Path(library_path)
        self._db_path = self._library_path / "zotero.sqlite"
        self._storage_path = self._library_path / "storage"
        self._linked_base = Path(linked_attachment_base) if linked_attachment_base else None

        if not self._db_path.exists():
            raise FileNotFoundError(f"zotero.sqlite not found in {self._library_path}")

    @property
    def adapter_type(self) -> str:
        return "zotero"

    @property
    def library_path(self) -> Path:
        return self._library_path

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection. NEVER write to Zotero's database.

        No ``immutable`` (4.4): Zotero may be writing concurrently, so a
        consistent WAL snapshot via ``mode=ro`` is required — ``immutable=1``
        would ignore the WAL and read stale or torn pages. ``busy_timeout``
        (helper default) absorbs a brief writer lock.
        """
        return connect_readonly(self._db_path, row_factory=sqlite3.Row)

    # ── EAV helpers ──────────────────────────────────────────────

    @staticmethod
    def _get_field(conn: sqlite3.Connection, item_id: int, field_name: str) -> str:
        """Get a single EAV field value for an item."""
        row = conn.execute(
            """
            SELECT idv.value
            FROM itemData id
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            JOIN fields f ON id.fieldID = f.fieldID
            WHERE id.itemID = ? AND f.fieldName = ?
            """,
            (item_id, field_name),
        ).fetchone()
        return row[0] if row else ""

    @staticmethod
    def _get_fields(conn: sqlite3.Connection, item_id: int, field_names: list[str]) -> dict[str, str]:
        """Get multiple EAV field values in one query."""
        placeholders = ",".join("?" * len(field_names))
        rows = conn.execute(
            f"""
            SELECT f.fieldName, idv.value
            FROM itemData id
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            JOIN fields f ON id.fieldID = f.fieldID
            WHERE id.itemID = ? AND f.fieldName IN ({placeholders})
            """,
            (item_id, *field_names),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ── Creators ─────────────────────────────────────────────────

    @staticmethod
    def _get_creators(conn: sqlite3.Connection, item_id: int) -> list[str]:
        """Get authors (and editors as fallback) for an item."""
        rows = conn.execute(
            """
            SELECT c.firstName, c.lastName, ct.creatorType
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID = ?
            ORDER BY ic.orderIndex
            """,
            (item_id,),
        ).fetchall()

        authors = []
        editors = []
        for row in rows:
            first, last, ctype = row[0] or "", row[1] or "", row[2]
            name = f"{first} {last}".strip() if first else last
            if not name:
                continue
            if ctype == "author":
                authors.append(name)
            elif ctype == "editor":
                editors.append(name)

        return authors if authors else editors

    # ── Tags ─────────────────────────────────────────────────────

    @staticmethod
    def _get_collection_ids(conn: sqlite3.Connection, collection_name: str) -> set[int]:
        """Return all collectionIDs with the given name plus all their descendants."""
        roots = conn.execute(
            "SELECT collectionID FROM collections WHERE collectionName = ?",
            (collection_name,),
        ).fetchall()
        if not roots:
            return set()
        ids = {r[0] for r in roots}
        queue = list(ids)
        while queue:
            ph = ",".join("?" * len(queue))
            children = conn.execute(
                f"SELECT collectionID FROM collections WHERE parentCollectionID IN ({ph})",
                queue,
            ).fetchall()
            new_ids = {r[0] for r in children} - ids
            queue = list(new_ids)
            ids |= new_ids
        return ids

    @staticmethod
    def _get_tags(conn: sqlite3.Connection, item_id: int) -> list[str]:
        rows = conn.execute(
            """
            SELECT t.name FROM itemTags it
            JOIN tags t ON it.tagID = t.tagID
            WHERE it.itemID = ?
            ORDER BY t.name
            """,
            (item_id,),
        ).fetchall()
        return [r[0] for r in rows]

    # ── Identifiers ──────────────────────────────────────────────

    @staticmethod
    def _get_identifiers(conn: sqlite3.Connection, item_id: int) -> dict[str, str]:
        fields = ZoteroAdapter._get_fields(conn, item_id, ["ISBN", "DOI", "ISSN"])
        result = {}
        for key in ("ISBN", "DOI", "ISSN"):
            val = fields.get(key, "")
            if val:
                result[key.lower()] = val
        return result

    # ── Attachments ──────────────────────────────────────────────

    def _resolve_attachment_path(self, conn: sqlite3.Connection, attachment_row) -> Path | None:
        """Resolve a Zotero attachment path to an absolute filesystem path."""
        link_mode = attachment_row["linkMode"]
        raw_path = attachment_row["path"] or ""
        att_key = conn.execute(
            "SELECT key FROM items WHERE itemID = ?",
            (attachment_row["itemID"],),
        ).fetchone()
        att_key = att_key[0] if att_key else ""

        if link_mode in (0, 1):
            # Imported file: "storage:filename.pdf"
            if raw_path.startswith("storage:"):
                filename = raw_path[len("storage:"):]
                resolved = self._storage_path / att_key / filename
                return resolved if resolved.is_file() else None
            # Fallback: look for any file in the storage dir
            storage_dir = self._storage_path / att_key
            if storage_dir.is_dir():
                for f in storage_dir.iterdir():
                    if f.is_file():
                        return f
            return None

        if link_mode == 2:
            # Linked file
            if raw_path.startswith("attachments:"):
                rel = raw_path[len("attachments:"):]
                if self._linked_base:
                    resolved = self._linked_base / rel
                    return resolved if resolved.is_file() else None
                return None
            # Absolute path
            p = Path(raw_path)
            return p if p.is_file() else None

        # linkMode 3 = linked URL, linkMode 4 = embedded image — no local file
        return None

    def describe_unresolved(self, doc_id: str) -> tuple[str, str]:
        """Why this item has no indexable file: ``(category, detail)``.

        ``_resolve_attachment_path`` has seven silent ``return None`` paths, and
        Phase 3 turned every one of them into a bare ``continue`` (finding
        1.15). The reasons differ completely in what they ask of the user — an
        unset config key, a moved file, a web link with no local copy — so a
        count alone is not actionable.

        The category carries no path, so many items collapse into one line in a
        summary; the detail carries the path, for the per-item log entry.
        """
        conn = self._connect()
        try:
            item_row = conn.execute(
                "SELECT itemID FROM items WHERE key = ?", (doc_id,)
            ).fetchone()
            if not item_row:
                return "item not found in zotero.sqlite", doc_id

            rows = conn.execute(
                "SELECT itemID, linkMode, path, contentType FROM itemAttachments "
                "WHERE parentItemID = ?",
                (item_row["itemID"],),
            ).fetchall()
            if not rows:
                return "item has no attachment at all", ""

            categories: list[str] = []
            details: list[str] = []
            for row in rows:
                link_mode = row["linkMode"]
                raw_path = row["path"] or ""

                if link_mode == 3:
                    categories.append("attachment is a linked URL, no local file")
                elif link_mode == 4:
                    categories.append("attachment is an embedded image, no local file")
                elif link_mode == 2 and raw_path.startswith("attachments:"):
                    rel = raw_path[len("attachments:"):]
                    if self._linked_base is None:
                        categories.append(
                            "linked attachment is relative, but "
                            "linked_attachment_base is not set for this source "
                            "— set it in the source config"
                        )
                    else:
                        categories.append("linked file does not exist")
                    details.append(rel)
                elif link_mode == 2:
                    categories.append("linked file does not exist")
                    details.append(raw_path)
                elif raw_path.startswith("storage:"):
                    categories.append("stored file missing from storage/")
                    details.append(raw_path)
                else:
                    categories.append(f"unresolvable attachment (linkMode={link_mode})")
                    details.append(raw_path)

            # De-duplicate, order preserved: several attachments of one item
            # usually fail for the same reason.
            seen: set[str] = set()
            unique = [c for c in categories if not (c in seen or seen.add(c))]
            return "; ".join(unique), "; ".join(details)
        finally:
            conn.close()

    def _get_primary_attachment(self, conn: sqlite3.Connection, item_id: int) -> tuple[Path | None, str]:
        """Find the best attachment for an item. Returns (path, format)."""
        rows = conn.execute(
            """
            SELECT ia.itemID, ia.linkMode, ia.contentType, ia.path
            FROM itemAttachments ia
            JOIN items i ON ia.itemID = i.itemID
            WHERE ia.parentItemID = ?
            AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            """,
            (item_id,),
        ).fetchall()

        candidates = []
        for row in rows:
            content_type = row["contentType"] or ""
            fmt = _CONTENT_TYPE_MAP.get(content_type, "")
            path = self._resolve_attachment_path(conn, row)
            if path and fmt:
                priority = _FORMAT_PRIORITY.get(fmt, 1)
                candidates.append((priority, path, fmt))

        if not candidates:
            return None, ""

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1], candidates[0][2]

    # ── Build metadata ───────────────────────────────────────────

    def _build_metadata(self, conn: sqlite3.Connection, item_id: int, item_key: str) -> DocumentMetadata:
        """Build DocumentMetadata for a single Zotero item."""
        fields = self._get_fields(conn, item_id, [
            "title", "abstractNote", "date", "publisher", "language",
            "series", "shortTitle", "extra",
        ])

        file_path, file_format = self._get_primary_attachment(conn, item_id)

        # Timestamps from items table
        ts_row = conn.execute(
            "SELECT dateAdded, dateModified FROM items WHERE itemID = ?",
            (item_id,),
        ).fetchone()

        return DocumentMetadata(
            doc_id=item_key,
            title=fields.get("title", "") or fields.get("shortTitle", "") or f"[Untitled {item_key}]",
            authors=self._get_creators(conn, item_id),
            file_path=file_path or Path(""),
            file_format=file_format,
            tags=self._get_tags(conn, item_id),
            comments=fields.get("abstractNote", ""),
            language=fields.get("language", ""),
            year=_parse_year(fields.get("date", "")),
            publisher=fields.get("publisher", ""),
            series=fields.get("series", ""),
            identifiers=self._get_identifiers(conn, item_id),
            custom_fields=_parse_extra(fields.get("extra", "")),
            timestamps=DocumentTimestamps(
                created_at=ts_row["dateAdded"] if ts_row else None,
                modified_at=ts_row["dateModified"] if ts_row else None,
            ),
        )

    # ── SourceAdapter interface ──────────────────────────────────

    def list_documents(
        self,
        tag_filter: str | None = None,
        exclude_tag: str | None = None,
        collection_filter: str | None = None,
        item_type_filter: str | None = None,
    ) -> list[DocumentMetadata]:
        conn = self._connect()
        try:
            query = """
                SELECT i.itemID, i.key
                FROM items i
                WHERE i.itemTypeID NOT IN ({excluded})
                AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
            """.format(excluded=",".join(str(t) for t in _EXCLUDED_TYPE_IDS))

            params: list = []

            if tag_filter:
                query += """
                    AND i.itemID IN (
                        SELECT it.itemID FROM itemTags it
                        JOIN tags t ON it.tagID = t.tagID
                        WHERE t.name = ?
                    )
                """
                params.append(tag_filter)

            if exclude_tag:
                query += """
                    AND i.itemID NOT IN (
                        SELECT it.itemID FROM itemTags it
                        JOIN tags t ON it.tagID = t.tagID
                        WHERE t.name = ?
                    )
                """
                params.append(exclude_tag)

            if collection_filter:
                col_ids = self._get_collection_ids(conn, collection_filter)
                if not col_ids:
                    logger.warning("Zotero collection %r not found — no items returned", collection_filter)
                    return []
                ph = ",".join("?" * len(col_ids))
                query += f"""
                    AND i.itemID IN (
                        SELECT itemID FROM collectionItems WHERE collectionID IN ({ph})
                    )
                """
                params.extend(col_ids)

            if item_type_filter:
                type_row = conn.execute(
                    "SELECT itemTypeID FROM itemTypes WHERE LOWER(typeName) = LOWER(?)",
                    (item_type_filter,),
                ).fetchone()
                if not type_row:
                    logger.warning("Zotero item type %r not found — no items returned", item_type_filter)
                    return []
                query += " AND i.itemTypeID = ?"
                params.append(type_row[0])

            query += " ORDER BY i.itemID"
            rows = conn.execute(query, params).fetchall()

            docs = []
            for row in rows:
                try:
                    docs.append(self._build_metadata(conn, row["itemID"], row["key"]))
                except Exception as e:
                    logger.warning("Failed to build metadata for item %s: %s", row["key"], e)
            return docs
        finally:
            conn.close()

    def get_metadata(self, doc_id: str) -> DocumentMetadata | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT itemID, key FROM items
                WHERE key = ?
                AND itemTypeID NOT IN ({excluded})
                AND itemID NOT IN (SELECT itemID FROM deletedItems)
                """.format(excluded=",".join(str(t) for t in _EXCLUDED_TYPE_IDS)),
                (doc_id,),
            ).fetchone()
            if not row:
                return None
            return self._build_metadata(conn, row["itemID"], row["key"])
        finally:
            conn.close()

    def get_file_path(self, doc_id: str) -> Path | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT itemID FROM items WHERE key = ?", (doc_id,)).fetchone()
            if not row:
                return None
            path, _ = self._get_primary_attachment(conn, row["itemID"])
            return path
        finally:
            conn.close()

    def get_annotations(self, doc_id: str) -> list[DocumentAnnotation]:
        """Highlights and notes for one item, as the indexer consumes them.

        This is the *only* route by which Zotero highlights reach the index
        (finding 1.4) — the Calibre-viewer reader the indexer used before knows
        nothing about ``zotero.sqlite``. Type and page therefore have to be
        usable here, not merely present: they become ``annotation_type`` and
        ``page_number`` on the chunk, and the page is what a citation names.
        The mapping lives in ``annotation_providers.zotero_provider`` so this
        adapter and the import path cannot drift apart.
        """
        conn = self._connect()
        try:
            item_row = conn.execute("SELECT itemID FROM items WHERE key = ?", (doc_id,)).fetchone()
            if not item_row:
                return []
            item_id = item_row["itemID"]

            annotations = []

            # Older Zotero schemas — and the minimal fixtures built against
            # them — have no sortIndex/pageLabel. Select what exists rather
            # than raising "no such column" on the whole item.
            available = {
                r["name"] for r in conn.execute("PRAGMA table_info(itemAnnotations)")
            }
            page_columns = [c for c in ("sortIndex", "pageLabel") if c in available]
            columns = ", ".join(["type", "text", "comment", *page_columns])

            # 1. PDF annotations (itemAnnotations via attachment)
            att_rows = conn.execute(
                "SELECT itemID FROM itemAttachments WHERE parentItemID = ?",
                (item_id,),
            ).fetchall()
            for att in att_rows:
                ann_rows = conn.execute(
                    f"SELECT {columns} FROM itemAnnotations WHERE parentItemID = ?",
                    (att["itemID"],),
                ).fetchall()
                for ar in ann_rows:
                    text = ar["text"] or ""
                    comment = ar["comment"] or ""
                    if not text and not comment:
                        continue
                    keys = ar.keys()
                    annotations.append(DocumentAnnotation(
                        text=text,
                        note=comment,
                        annotation_type=zotero_annotation_type(ar["type"]),
                        page=zotero_page_number(
                            ar["sortIndex"] if "sortIndex" in keys else "",
                            ar["pageLabel"] if "pageLabel" in keys else "",
                        ),
                    ))

            # 2. Standalone notes (itemNotes)
            note_rows = conn.execute(
                "SELECT note, title FROM itemNotes WHERE parentItemID = ?",
                (item_id,),
            ).fetchall()
            for nr in note_rows:
                text = _strip_html(nr["note"] or "")
                if text:
                    annotations.append(DocumentAnnotation(
                        text=text,
                        note="",
                        annotation_type="note",
                    ))

            return annotations
        finally:
            conn.close()

    def get_comments(self, doc_id: str) -> str:
        conn = self._connect()
        try:
            item_row = conn.execute("SELECT itemID FROM items WHERE key = ?", (doc_id,)).fetchone()
            if not item_row:
                return ""
            return self._get_field(conn, item_row["itemID"], "abstractNote")
        finally:
            conn.close()

    def compute_metadata_hash(self, doc_id: str) -> str:
        """Hash over title/authors/tags/abstract/date for change detection.

        Authors are sorted alphabetically so that reordering authors in
        Zotero's UI does not trigger a false-positive watchdog update.
        Use ``_get_creators()`` directly if insertion order matters.
        """
        conn = self._connect()
        try:
            row = conn.execute("SELECT itemID FROM items WHERE key = ?", (doc_id,)).fetchone()
            if not row:
                return ""
            item_id = row["itemID"]

            fields = self._get_fields(conn, item_id, ["title", "abstractNote", "date"])
            authors = self._get_creators(conn, item_id)
            tags = self._get_tags(conn, item_id)

            from src.archilles.hashing import compute_zotero_metadata_hash
            return compute_zotero_metadata_hash({
                "title": fields.get("title", ""),
                "authors": authors,
                "tags": tags,
                "abstract": fields.get("abstractNote", ""),
                "date": fields.get("date", ""),
            })
        finally:
            conn.close()

    def compute_orphan_ids(self, lancedb_ids: set[str]) -> set[str]:
        """Diff against ``items.key`` — same filters as ``list_documents()``.

        Excluded item types (annotation/attachment/note) and trashed items
        must match list_documents() exactly, otherwise children of an item
        would be flagged as orphans on every cleanup pass.
        """
        excluded = ",".join(str(t) for t in _EXCLUDED_TYPE_IDS)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT key FROM items
                WHERE itemTypeID NOT IN ({excluded})
                AND itemID NOT IN (SELECT itemID FROM deletedItems)
                """
            ).fetchall()
            current = {r[0] for r in rows}
        finally:
            conn.close()
        return {str(x) for x in lancedb_ids} - current
