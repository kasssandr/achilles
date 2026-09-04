"""
Calibre adapter — wraps the existing ``calibre_db.CalibreDB`` behind SourceAdapter.

This is a **wrapper**, not a replacement.  ``calibre_db.py`` stays untouched
and continues to do the actual SQLite work.
"""

import logging
import os
from pathlib import Path

from src.adapters.base import (
    DocumentAnnotation,
    DocumentMetadata,
    DocumentTimestamps,
    SourceAdapter,
)
from src.archilles.sqlite_ro import connect_readonly
from src.calibre_db import CalibreDB
from src.calibre_mcp.annotations import (
    get_combined_annotations as _calibre_get_combined_annotations,
)

logger = logging.getLogger(__name__)

# Format priority when a book has multiple files in Calibre
_FORMAT_PRIORITY = ("pdf", "epub", "mobi", "azw3", "djvu", "txt", "md", "html")


class CalibreAdapter(SourceAdapter):
    """Adapter for Calibre libraries (``metadata.db``)."""

    def __init__(self, library_path: Path):
        self._library_path = Path(library_path)
        if not (self._library_path / "metadata.db").exists():
            raise FileNotFoundError(
                f"No metadata.db in {self._library_path} — not a Calibre library"
            )

    # ── Properties ──────────────────────────────────────────────

    @property
    def adapter_type(self) -> str:
        return "calibre"

    @property
    def library_path(self) -> Path:
        return self._library_path

    # ── Core interface ──────────────────────────────────────────

    def list_documents(
        self,
        tag_filter: str | None = None,
        exclude_tag: str | None = None,
        collection_filter: str | None = None,
        item_type_filter: str | None = None,
    ) -> list[DocumentMetadata]:
        import sqlite3

        db_path = self._library_path / "metadata.db"
        conn = connect_readonly(db_path, row_factory=sqlite3.Row)

        # One batch query per relation instead of ~8 queries per book (1.23).
        # Each is a single table scan; the whole listing is O(1) in queries.
        try:
            books = conn.execute(
                "SELECT id, title, path, pubdate, last_modified "
                "FROM books ORDER BY id"
            ).fetchall()
            tags_by_book = self._group_multi(conn.execute(
                "SELECT btl.book AS book, t.name AS name "
                "FROM books_tags_link btl JOIN tags t ON t.id = btl.tag"
            ))
            authors_by_book = self._group_multi(conn.execute(
                "SELECT bal.book AS book, a.name AS name "
                "FROM books_authors_link bal JOIN authors a ON a.id = bal.author "
                "ORDER BY bal.book, bal.id"
            ))
            publisher_by_book = self._first_value(conn.execute(
                "SELECT bpl.book AS book, p.name AS val "
                "FROM books_publishers_link bpl "
                "JOIN publishers p ON p.id = bpl.publisher"
            ))
            language_by_book = self._first_value(conn.execute(
                "SELECT bll.book AS book, l.lang_code AS val "
                "FROM books_languages_link bll "
                "JOIN languages l ON l.id = bll.lang_code "
                "ORDER BY bll.book, bll.id"
            ))
            isbn_by_book = self._first_value(conn.execute(
                "SELECT book, val FROM identifiers WHERE type = 'isbn'"
            ))
            comments_by_book = self._first_value(conn.execute(
                "SELECT book, text AS val FROM comments"
            ))
        finally:
            conn.close()

        results: list[DocumentMetadata] = []
        for row in books:
            book_id = row["id"]
            tags = tags_by_book.get(book_id, [])

            if tag_filter and tag_filter not in tags:
                continue
            if exclude_tag and exclude_tag in tags:
                continue

            file_path = self._find_primary_file(row["path"])
            if file_path is None:
                continue

            results.append(self._assemble_metadata(
                book_id, row["title"], file_path,
                authors=authors_by_book.get(book_id, []),
                tags=tags,
                publisher=publisher_by_book.get(book_id, "") or "",
                language=language_by_book.get(book_id, "") or "",
                isbn=isbn_by_book.get(book_id),
                comments_html=comments_by_book.get(book_id),
                pubdate=row["pubdate"],
                last_modified=row["last_modified"],
            ))

        return results

    @staticmethod
    def _group_multi(cursor) -> dict:
        """book_id -> [name, ...], preserving the cursor's row order."""
        out: dict = {}
        for r in cursor:
            out.setdefault(r["book"], []).append(r["name"])
        return out

    @staticmethod
    def _first_value(cursor) -> dict:
        """book_id -> first value seen (mirrors the old per-book LIMIT 1)."""
        out: dict = {}
        for r in cursor:
            if r["book"] not in out:
                out[r["book"]] = r["val"]
        return out

    def get_metadata(self, doc_id: str) -> DocumentMetadata | None:
        import sqlite3

        conn = connect_readonly(
            self._library_path / "metadata.db", row_factory=sqlite3.Row
        )
        try:
            row = conn.execute(
                "SELECT id, title, path FROM books WHERE id = ?",
                (int(doc_id),),
            ).fetchone()
            if not row:
                return None

            tag_rows = conn.execute(
                """
                SELECT tags.name FROM tags
                INNER JOIN books_tags_link ON tags.id = books_tags_link.tag
                WHERE books_tags_link.book = ?
                """,
                (int(doc_id),),
            ).fetchall()
            tags = [t["name"] for t in tag_rows]

            file_path = self._find_primary_file(row["path"])
            if file_path is None:
                return None

            return self._row_to_metadata(conn, int(doc_id), row["title"], row["path"], tags, file_path)
        finally:
            conn.close()

    def get_file_path(self, doc_id: str) -> Path | None:
        import sqlite3

        conn = connect_readonly(
            self._library_path / "metadata.db", row_factory=sqlite3.Row
        )
        row = conn.execute(
            "SELECT path FROM books WHERE id = ?", (int(doc_id),)
        ).fetchone()
        conn.close()
        if not row:
            return None
        return self._find_primary_file(row["path"])

    def get_annotations(self, doc_id: str) -> list[DocumentAnnotation]:
        """Calibre-viewer highlights plus PDF-embedded annotations.

        The filter arguments are not defaults and must not become defaults:
        ``min_length`` and ``exclude_toc_markers`` decide which annotations
        exist at all, and therefore the book's annotation hash. They match what
        the indexer passed when it still called ``get_combined_annotations``
        directly, which is what makes this route equivalent to that one — the
        reason the indexer no longer needs a Calibre special case (finding 1.4).
        Change them and every Calibre book re-indexes.
        """
        file_path = self.get_file_path(doc_id)
        if not file_path:
            return []

        try:
            result = _calibre_get_combined_annotations(
                book_path=str(file_path),
                include_pdf=True,
                exclude_toc_markers=True,
                min_length=20,
            )
            annotations = result.get("annotations", [])
            return [
                DocumentAnnotation(
                    text=a.get("highlighted_text", ""),
                    note=a.get("notes", ""),
                    annotation_type=a.get("type", "highlight"),
                    page=a.get("page"),
                    created=a.get("timestamp", ""),
                    source=a.get("source", ""),
                )
                for a in annotations
            ]
        except Exception as e:
            logger.debug(f"Could not read annotations for {doc_id}: {e}")
            return []

    def get_comments(self, doc_id: str) -> str:
        import sqlite3

        conn = connect_readonly(
            self._library_path / "metadata.db", row_factory=sqlite3.Row
        )
        try:
            row = conn.execute(
                "SELECT text FROM comments WHERE book = ?", (int(doc_id),)
            ).fetchone()
            if row and row["text"]:
                return CalibreDB.clean_html(row["text"])
            return ""
        finally:
            conn.close()

    def get_metadata_by_path(self, file_path: Path) -> DocumentMetadata | None:
        """Efficient path-based lookup via CalibreDB."""
        file_path = Path(file_path).resolve()
        library_path = CalibreDB.find_library_path(file_path)
        if not library_path or library_path.resolve() != self._library_path.resolve():
            return None

        with CalibreDB(self._library_path) as db:
            book_data = db.get_book_by_path(file_path)
            if not book_data:
                return None
            return self._book_data_to_metadata(book_data, file_path)

    # ── Internal helpers ────────────────────────────────────────

    def _find_primary_file(self, calibre_book_path: str) -> Path | None:
        """Find the best available file for a Calibre book folder."""
        book_dir = self._library_path / calibre_book_path
        if not book_dir.is_dir():
            return None

        files_by_ext: dict[str, Path] = {}
        for f in book_dir.iterdir():
            if f.is_file() and f.suffix:
                files_by_ext[f.suffix.lower().lstrip(".")] = f

        for fmt in _FORMAT_PRIORITY:
            if fmt in files_by_ext:
                return files_by_ext[fmt]

        # Fallback: any file
        for f in files_by_ext.values():
            return f
        return None

    def _row_to_metadata(
        self,
        conn,
        book_id: int,
        title: str,
        book_path: str,
        tags: list[str],
        file_path: Path,
    ) -> DocumentMetadata:
        """Build DocumentMetadata for a single book (used by get_metadata)."""
        author_rows = conn.execute(
            """
            SELECT authors.name FROM authors
            INNER JOIN books_authors_link ON authors.id = books_authors_link.author
            WHERE books_authors_link.book = ?
            ORDER BY books_authors_link.id
            """,
            (book_id,),
        ).fetchall()
        authors = [r["name"] for r in author_rows] if author_rows else []

        pub_row = conn.execute(
            """
            SELECT publishers.name FROM publishers
            INNER JOIN books_publishers_link ON publishers.id = books_publishers_link.publisher
            WHERE books_publishers_link.book = ?
            """,
            (book_id,),
        ).fetchone()
        publisher = pub_row["name"] if pub_row else ""

        lang_row = conn.execute(
            """
            SELECT languages.lang_code FROM languages
            INNER JOIN books_languages_link ON languages.id = books_languages_link.lang_code
            WHERE books_languages_link.book = ?
            """,
            (book_id,),
        ).fetchone()
        language = lang_row["lang_code"] if lang_row else ""

        isbn_row = conn.execute(
            "SELECT val FROM identifiers WHERE book = ? AND type = 'isbn' LIMIT 1",
            (book_id,),
        ).fetchone()
        isbn = isbn_row["val"] if isbn_row else None

        comment_row = conn.execute(
            "SELECT text FROM comments WHERE book = ?", (book_id,)
        ).fetchone()
        comments_html = comment_row["text"] if comment_row else None

        ts_row = conn.execute(
            "SELECT pubdate, last_modified FROM books WHERE id = ?",
            (book_id,),
        ).fetchone()
        pubdate = ts_row["pubdate"] if ts_row else None
        last_modified = ts_row["last_modified"] if ts_row else None

        return self._assemble_metadata(
            book_id, title, file_path,
            authors=authors, tags=tags, publisher=publisher,
            language=language, isbn=isbn, comments_html=comments_html,
            pubdate=pubdate, last_modified=last_modified,
        )

    @staticmethod
    def _assemble_metadata(
        book_id: int,
        title: str,
        file_path: Path,
        *,
        authors: list[str],
        tags: list[str],
        publisher: str,
        language: str,
        isbn: str | None,
        comments_html: str | None,
        pubdate,
        last_modified,
    ) -> DocumentMetadata:
        """Turn already-loaded values into DocumentMetadata.

        Shared by the per-book path (``_row_to_metadata``) and the batch path
        (``list_documents``) so the field logic lives in exactly one place.
        """
        comments = CalibreDB.clean_html(comments_html) if comments_html else ""
        comments_html = comments_html or ""

        timestamps = DocumentTimestamps()
        pub_year = None
        if pubdate:
            timestamps.created_at = str(pubdate)
            try:
                pub_year = int(str(pubdate)[:4])
            except (ValueError, TypeError):
                pass
        if last_modified:
            timestamps.modified_at = str(last_modified)

        identifiers = {}
        if isbn:
            identifiers["isbn"] = isbn

        return DocumentMetadata(
            doc_id=str(book_id),
            title=title,
            authors=authors,
            file_path=file_path,
            file_format=file_path.suffix.lower().lstrip("."),
            tags=tags,
            comments=comments,
            comments_html=comments_html,
            language=language,
            year=pub_year,
            publisher=publisher,
            identifiers=identifiers,
            timestamps=timestamps,
        )

    def compute_metadata_hash(self, doc_id: str) -> str:
        """Hash over title/author/tags/comments/publisher — identical to watchdog._compute_metadata_hash.

        Producing the same hash guarantees that LanceDB entries written by the
        watchdog can be compared directly against adapter-computed hashes.
        """
        import sqlite3

        db_path = self._library_path / "metadata.db"
        conn = connect_readonly(db_path, row_factory=sqlite3.Row)
        try:
            row = conn.execute(
                "SELECT title FROM books WHERE id = ?", (int(doc_id),)
            ).fetchone()
            if not row:
                return ""

            author_rows = conn.execute(
                """
                SELECT a.name FROM authors a
                INNER JOIN books_authors_link bal ON a.id = bal.author
                WHERE bal.book = ? ORDER BY bal.id
                """,
                (int(doc_id),),
            ).fetchall()
            author = " & ".join(r["name"] for r in author_rows)

            tag_rows = conn.execute(
                """
                SELECT t.name FROM tags t
                INNER JOIN books_tags_link btl ON t.id = btl.tag
                WHERE btl.book = ?
                """,
                (int(doc_id),),
            ).fetchall()
            tags = sorted(r["name"] for r in tag_rows)

            comment_row = conn.execute(
                "SELECT text FROM comments WHERE book = ?", (int(doc_id),)
            ).fetchone()
            comments = (
                CalibreDB.clean_html(comment_row["text"] or "")
                if comment_row and comment_row["text"]
                else ""
            )

            pub_row = conn.execute(
                """
                SELECT p.name FROM publishers p
                INNER JOIN books_publishers_link bpl ON p.id = bpl.publisher
                WHERE bpl.book = ?
                """,
                (int(doc_id),),
            ).fetchone()
            publisher = pub_row["name"] if pub_row else ""

            from src.archilles.hashing import compute_metadata_hash
            return compute_metadata_hash({
                "comments": comments,
                "tags": tags,
                "title": row["title"] or "",
                "author": author,
                "publisher": publisher,
            })
        finally:
            conn.close()

    def compute_orphan_ids(self, lancedb_ids: set[str]) -> set[str]:
        """Diff against ``SELECT id FROM books`` — narrow query, no joins.

        ``list_documents()`` would build full DocumentMetadata for every book
        (tags, authors, comments) which is wasteful for an ID-only diff.
        IDs from LanceDB are normalised to ``str`` because legacy entries can
        be stored as ints.
        """
        conn = connect_readonly(self._library_path / "metadata.db")
        try:
            rows = conn.execute("SELECT id FROM books").fetchall()
            current = {str(r[0]) for r in rows}
        finally:
            conn.close()
        return {str(x) for x in lancedb_ids} - current

    def _book_data_to_metadata(
        self, book_data: dict, file_path: Path
    ) -> DocumentMetadata:
        """Convert a ``CalibreDB.get_book_by_path()`` dict to DocumentMetadata."""
        # Split author string back to list
        author_str = book_data.get("author", "")
        authors = [a.strip() for a in author_str.split("&")] if author_str else []

        identifiers = {}
        if book_data.get("isbn"):
            identifiers["isbn"] = book_data["isbn"]

        return DocumentMetadata(
            doc_id=str(book_data["calibre_id"]),
            title=book_data.get("title", ""),
            authors=authors,
            file_path=file_path,
            file_format=file_path.suffix.lower().lstrip("."),
            tags=book_data.get("tags", []),
            comments=book_data.get("comments", "") or "",
            comments_html=book_data.get("comments_html", "") or "",
            language=book_data.get("language", "") or "",
            publisher=book_data.get("publisher", "") or "",
            identifiers=identifiers,
            custom_fields=book_data.get("custom_fields", {}),
        )
