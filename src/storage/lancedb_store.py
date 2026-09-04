"""
LanceDB Storage Backend for ARCHILLES RAG System.

Native hybrid search support (vector + full-text).
Designed for scalability to 1M+ chunks with IVF-PQ indexing.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import lancedb
import numpy as np

from src.archilles.constants import ChunkType, SectionType
from src.archilles.pipeline_version import PIPELINE_VERSION

try:
    from lancedb.rerankers import RRFReranker
except ImportError:
    RRFReranker = None

logger = logging.getLogger(__name__)


def _sql_quote(value: str) -> str:
    """Escape a string for use inside a single-quoted LanceDB SQL literal.

    book_id values are built from author + title, so apostrophes
    ("O'Brien_Ulysses_42") are realistic and would otherwise break every
    filter, delete and update expression (code review finding 1.1).
    """
    return str(value).replace("'", "''")


class LanceDBStore:
    """
    LanceDB storage backend for ARCHILLES.

    Features:
    - Native hybrid search (vector + FTS in single query)
    - IVF-PQ index for large corpora (>100k chunks)
    - SQL-like metadata filtering
    - Memory-mapped files for efficient I/O
    """

    # Schema definition for chunks table
    SCHEMA = {
        "id": str,
        "text": str,
        "vector": list,  # Will be converted to Vector type

        # Book metadata (from Calibre)
        "book_id": str,
        "book_title": str,
        "author": str,
        "publisher": str,
        "year": int,
        "calibre_id": int,
        "source_id": str,       # Adapter-agnostic document ID (replaces calibre_id)
        "tags": str,
        "language": str,

        # Position metadata
        "chunk_index": int,
        "chunk_type": str,    # See ChunkType constants
        "page_number": int,   # Physical PDF page (for navigation)
        "page_label": str,    # Printed page label (for citations, e.g. "xiv", "62")
        "chapter": str,

        # Section metadata (EPUB)
        "section": str,
        "section_title": str,
        "section_type": str,

        # Context expansion (Small-to-Big Retrieval)
        "char_start": int,    # Character offset in extracted full text
        "char_end": int,      # Character offset end in extracted full text
        "window_text": str,   # Chunk + ~500 chars context (for expanded retrieval)

        # Parent-Child hierarchy
        "parent_id": str,     # Empty for parents, references parent chunk ID for children

        # Annotation metadata
        "annotation_type": str,    # "highlight", "note", "bookmark" (for chunk_type='annotation')
        "annotation_source": str,  # "calibre_viewer" or "pdf"
        "annotation_hash": str,    # Hash of all annotations for change detection

        # Technical metadata
        "source_file": str,
        "format": str,
        "indexed_at": str,
        "metadata_hash": str,  # Hash of Calibre metadata for change detection

        # Generation of the pipeline that wrote this row. '' = written before
        # the marker existed; see src/archilles/pipeline_version.py.
        "pipeline_version": str,

        # Hardware-Tiers-V2 §12: 1 = provisionally light (mode=full-external,
        # waiting for an external hierarchical re-embed), 0 = final. Lets the
        # discovery path tell "provisional light" from "deliberately light".
        "pending_external": int,
    }

    def __init__(self, db_path: str, table_name: str = "chunks"):
        """
        Initialize LanceDB connection.

        Args:
            db_path: Path to LanceDB database directory
            table_name: Name of the chunks table
        """
        self.db_path = Path(db_path)
        self.table_name = table_name
        self.db = lancedb.connect(str(self.db_path))
        self.table = None
        self._vector_dim = 1024  # BGE-M3 embedding dimension
        self._ensure_table()

    # Columns that may be missing from tables created before a feature was added.
    # Mapping: column_name -> SQL default expression used by LanceDB add_columns().
    _MIGRATABLE_COLUMNS = {
        "page_label": "''",
        "char_start": "0",
        "char_end": "0",
        "window_text": "''",
        "parent_id": "''",
        "metadata_hash": "''",
        "annotation_type": "''",
        "annotation_source": "''",
        "annotation_hash": "''",
        "source_id": "''",
        "pending_external": "0",
        # Rows written before the marker existed keep '' — that is the signal,
        # not a gap: '' plus indexed_at locates the pre-marker generations.
        "pipeline_version": "''",
    }

    def _ensure_table(self):
        """Open existing table (with schema migration) or prepare for first add."""
        if self.table_name in self.db.table_names():
            self.table = self.db.open_table(self.table_name)
            self._migrate_schema()
        else:
            # Table will be created on first add
            self.table = None

    def _migrate_schema(self):
        """Add any missing columns to an existing table."""
        if self.table is None:
            return
        try:
            existing = set(self.table.schema.names)
        except Exception:
            return

        for col, default_expr in self._MIGRATABLE_COLUMNS.items():
            if col not in existing:
                try:
                    self.table.add_columns({col: default_expr})
                    logger.info(f"Schema migration: added column '{col}' to table")
                except Exception as e:
                    logger.warning(f"Schema migration: failed to add column '{col}': {e}")

    def _create_table_with_data(self, records: list[dict[str, Any]]):
        """Create table with initial data (LanceDB requires data to infer schema)."""
        self.table = self.db.create_table(
            self.table_name,
            data=records,
            mode="overwrite"
        )

    def create_indexes(self, num_chunks: int = None):
        """
        Create IVF-PQ and FTS indexes for optimal search performance.

        Should be called after bulk indexing is complete.

        Args:
            num_chunks: Total number of chunks (used to calculate partitions)
        """
        if self.table is None:
            logger.warning("No table exists, skipping index creation")
            return

        chunk_count = num_chunks or self.count()

        # Create IVF-PQ vector index only if we have enough data
        if chunk_count >= 256:
            # Calculate optimal partitions (sqrt(n) is a good heuristic)
            num_partitions = min(256, max(16, int(np.sqrt(chunk_count))))

            try:
                logger.info(f"Creating IVF-PQ index with {num_partitions} partitions...")
                self.table.create_index(
                    metric="cosine",
                    num_partitions=num_partitions,
                    num_sub_vectors=32,
                    index_type="IVF_PQ"
                )
                logger.info("Vector index created successfully")
            except Exception as e:
                logger.warning(f"Could not create vector index: {e}")
        else:
            logger.info(f"Skipping IVF-PQ index (need 256+ chunks, have {chunk_count})")

        # Always create FTS index for hybrid search
        self.create_fts_index()

    def ensure_vector_index(self, num_chunks: int = None) -> bool:
        """Create the IVF-PQ vector index if the table does not have one yet.

        Unlike :meth:`create_indexes` this is cheap when the index already
        exists (a ``list_indices()`` check), so indexing paths can call it
        after every run. Without a vector index, semantic/hybrid search
        brute-force-scans the vector column — minutes instead of seconds at
        million-chunk scale.

        Returns True when a vector index exists afterwards.
        """
        if self.table is None:
            return False

        try:
            for idx in self.table.list_indices():
                if "vector" in list(getattr(idx, "columns", [])):
                    return True
        except Exception as e:
            logger.warning(f"Could not list indices: {e}")
            return False

        chunk_count = num_chunks or self.count()
        if chunk_count < 256:
            logger.info(
                f"Skipping IVF-PQ index (need 256+ chunks, have {chunk_count})"
            )
            return False

        num_partitions = min(256, max(16, int(np.sqrt(chunk_count))))
        try:
            logger.info(f"Creating IVF-PQ index with {num_partitions} partitions...")
            self.table.create_index(
                metric="cosine",
                num_partitions=num_partitions,
                num_sub_vectors=32,
                index_type="IVF_PQ",
            )
            logger.info("Vector index created successfully")
            return True
        except Exception as e:
            logger.warning(f"Could not create vector index: {e}")
            return False

    def optimize_indexes(self, retain_days: int = 2) -> bool:
        """Merge rows added since the last index build into existing indexes.

        Incremental (unlike the full rebuilds in create_indexes/
        create_fts_index): LanceDB scans rows not yet covered by an index on
        every query, so paths that add data without refreshing indexes
        degrade search from milliseconds to minutes as the backlog grows.

        Merging rewrites the vector index in full, and LanceDB keeps the
        superseded copy until an explicit cleanup drops it. Callers run this
        after every routine, so without ``cleanup_older_than`` the daily
        rewrites accumulate (a 1.5M-chunk IVF-PQ index is ~16 GB per copy).
        Versions younger than ``retain_days`` survive so that a concurrent
        reader holding an older manifest is not pulled out from under.

        Two days, not seven: the retention window costs one index copy per
        routine run, and at this corpus size the seven-day window kept the
        database at 133 GB with nothing old enough to collect. A single
        machine running one routine at a time does not need a week of
        manifests; two days still covers a reader that started yesterday.

        Returns True on success.
        """
        if self.table is None:
            return False
        try:
            self.table.optimize(cleanup_older_than=timedelta(days=retain_days))
            return True
        except Exception as e:
            logger.warning(f"Index optimize failed: {e}")
            return False

    def create_fts_index(self):
        """Create full-text search index on text column."""
        if self.table is None:
            logger.warning("No table exists, skipping FTS index creation")
            return

        try:
            logger.info("Creating FTS index on text column...")
            self.table.create_fts_index("text", replace=True, with_position=True)
            logger.info("FTS index created successfully")
        except Exception as e:
            logger.warning(f"Could not create FTS index: {e}")

    def add_chunks(self, chunks: list[dict[str, Any]], embeddings: np.ndarray) -> int:
        """
        Add chunks with their embeddings to the database.

        Args:
            chunks: List of chunk dictionaries with metadata
            embeddings: Numpy array of embeddings (shape: [n_chunks, embedding_dim])

        Returns:
            Number of chunks added
        """
        if len(chunks) == 0:
            return 0

        records = []
        for i, chunk in enumerate(chunks):
            # Use `or` instead of default arg to also catch explicit None values
            record = {
                "id": chunk.get("id") or f"chunk_{i}",
                "text": chunk.get("text") or "",
                "vector": embeddings[i].tolist(),

                # Book metadata
                "book_id": chunk.get("book_id") or "",
                "book_title": chunk.get("book_title") or chunk.get("title") or "",
                "author": chunk.get("author") or "",
                "publisher": chunk.get("publisher") or "",
                "year": chunk.get("year") or 0,
                "calibre_id": chunk.get("calibre_id") or 0,
                "source_id": chunk.get("source_id") or str(chunk.get("calibre_id") or ""),
                "tags": chunk.get("tags") or "",
                "language": chunk.get("language") or "",

                # Position metadata
                "chunk_index": chunk["chunk_index"] if "chunk_index" in chunk else i,
                "chunk_type": chunk.get("chunk_type") or ChunkType.CONTENT,
                "page_number": chunk.get("page_number") or chunk.get("page") or 0,
                "chapter": chunk.get("chapter") or "",

                # Section metadata (EPUB)
                "section": chunk.get("section") or "",
                "section_title": chunk.get("section_title") or "",
                "section_type": chunk.get("section_type") or "",

                # Context expansion (Small-to-Big Retrieval)
                "char_start": chunk.get("char_start") or 0,
                "char_end": chunk.get("char_end") or 0,
                "window_text": chunk.get("window_text") or "",

                # Parent-Child hierarchy
                "parent_id": chunk.get("parent_id") or "",

                # Printed page label (for citations)
                "page_label": chunk.get("page_label") or "",

                # Annotation metadata
                "annotation_type": chunk.get("annotation_type") or "",
                "annotation_source": chunk.get("annotation_source") or "",
                "annotation_hash": chunk.get("annotation_hash") or "",

                # Technical metadata
                "source_file": chunk.get("source_file") or "",
                "format": chunk.get("format") or "",
                "indexed_at": chunk.get("indexed_at") or datetime.now().isoformat(),
                "metadata_hash": chunk.get("metadata_hash") or "",

                # Hardware-Tiers-V2 §12: provisional-light marker (default: final)
                "pending_external": chunk.get("pending_external") or 0,

                # Which generation of the pipeline composed this row (1.7a).
                # Not taken from the chunk dict: a caller that could pass its
                # own value could claim a generation it did not produce.
                "pipeline_version": PIPELINE_VERSION,
            }

            records.append(record)

        if self.table is None:
            self._create_table_with_data(records)
        else:
            # Finding 1.11: LanceDB enforces no ID uniqueness — re-indexing
            # without a prior delete silently duplicated chunks. Upsert on
            # `id` so the newest write wins instead of accumulating copies.
            (
                self.table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(records)
            )

        return len(records)

    def add_processed_documents(
        self,
        processed_docs,
        book_metadata: dict[str, Any] | None = None,
        calibre_id: int | None = None,
        source_id: str | None = None,
    ) -> int:
        """
        Add documents processed by the ModularPipeline to the database.

        Bridges between ProcessedDocument (from src.archilles.pipeline)
        and the LanceDB storage format.

        Args:
            processed_docs: Single ProcessedDocument or list of them
                (from ModularPipeline.process())
            book_metadata: Optional dict with book_id, tags, language, publisher, year
            calibre_id: Optional Calibre book ID

        Returns:
            Total number of chunks added
        """
        # Handle single document
        if not isinstance(processed_docs, list):
            processed_docs = [processed_docs]

        total_added = 0
        book_meta = book_metadata or {}

        for doc in processed_docs:
            if not doc.chunks or not doc.embeddings:
                continue

            # Build book_id from metadata or filename
            book_id = book_meta.get("book_id", "")
            if not book_id:
                # Generate book_id from author + title or filename
                author_part = doc.authors[0] if doc.authors else "Unknown"
                title_part = doc.title or Path(doc.file_path).stem
                book_id = f"{author_part}_{title_part}_{calibre_id or 0}"

            # Convert TextChunk objects to the dict format expected by add_chunks().
            # All structural metadata is read from chunk.metadata — TextChunk has
            # no .chapter/.section_title attributes (Befund 3.1), and char offsets,
            # window_text, parent_id and page_label are wired through from the
            # parser instead of being dropped (Befund 2.17/1.28).
            chunks = []
            for i, chunk in enumerate(doc.chunks):
                meta = chunk.metadata or {}
                section = meta.get("section")
                chunk_dict = {
                    "id": f"{book_id}_chunk_{i}",
                    "text": chunk.text,

                    # Book metadata
                    "book_id": book_id,
                    "book_title": doc.title or Path(doc.file_path).stem,
                    "author": " & ".join(doc.authors) if doc.authors else book_meta.get("author", ""),
                    "publisher": book_meta.get("publisher", ""),
                    "year": book_meta.get("year", 0),
                    "calibre_id": calibre_id or book_meta.get("calibre_id", 0),
                    "source_id": source_id or str(calibre_id or book_meta.get("calibre_id", "") or ""),
                    "tags": book_meta.get("tags", ""),
                    "language": book_meta.get("language", ""),

                    # Position metadata
                    "chunk_index": chunk.chunk_index,
                    "chunk_type": meta.get("chunk_type", ChunkType.CONTENT),
                    "page_number": chunk.page_start or 0,
                    "page_label": meta.get("page_label") or (str(chunk.page_start) if chunk.page_start else ""),
                    "chapter": meta.get("chapter", ""),

                    # Section metadata (if EPUB)
                    "section": str(section) if section not in (None, "") else "",
                    "section_title": meta.get("section_title", ""),
                    "section_type": meta.get("section_type", ""),

                    # Context expansion (Small-to-Big retrieval)
                    "char_start": chunk.start_char or meta.get("char_start") or 0,
                    "char_end": chunk.end_char or meta.get("char_end") or 0,
                    "window_text": meta.get("window_text", ""),

                    # Parent-Child hierarchy
                    "parent_id": meta.get("parent_id", ""),

                    # Technical metadata
                    "source_file": doc.file_path,
                    "format": Path(doc.file_path).suffix.lstrip(".").upper(),
                }
                chunks.append(chunk_dict)

            # Convert embeddings list to numpy array
            embeddings = np.array(doc.embeddings, dtype=np.float32)

            # Use existing add_chunks method
            added = self.add_chunks(chunks, embeddings)
            total_added += added

            logger.info(
                f"Pipeline: Added {added} chunks from '{doc.file_name}' "
                f"(parse: {doc.parse_time:.1f}s, chunk: {doc.chunk_time:.1f}s, "
                f"embed: {doc.embed_time:.1f}s)"
            )

        return total_added

    def hybrid_search(
        self,
        query_text: str,
        query_vector: np.ndarray,
        top_k: int = 10,
        book_id: str | None = None,
        calibre_id: int | None = None,
        section_type: str | None = None,
        chunk_type: str | None = None,
        language: str | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Hybrid search combining vector similarity and full-text search.

        Uses LanceDB's native hybrid search with reranking.

        Args:
            query_text: Search query text (for FTS)
            query_vector: Query embedding vector
            top_k: Number of results to return
            book_id: Filter by book_id
            calibre_id: Filter by Calibre ID
            section_type: Filter by section type ("main", "front_matter", "back_matter")
            chunk_type: Filter by chunk type ("content", "phase1_metadata", etc.)
            language: Filter by language code

        Returns:
            List of result dictionaries with metadata and scores
        """
        if self.table is None:
            return []

        # Build filter string
        filters = self._build_filter(
            book_id=book_id,
            calibre_id=calibre_id,
            section_type=section_type,
            chunk_type=chunk_type,
            language=language,
            source_id=source_id,
        )

        try:
            # RRF (Reciprocal Rank Fusion) combines by rank position, not raw
            # scores, handling the scale mismatch between cosine (0-1) and BM25.
            search = self.table.search(query_type="hybrid") \
                .vector(query_vector.tolist()) \
                .text(query_text)

            if RRFReranker is not None:
                search = search.rerank(reranker=RRFReranker())

            if filters:
                search = search.where(filters)

            results = search.limit(top_k).to_pandas()
        except Exception as e:
            # Fallback to vector-only search if hybrid fails
            logger.warning(f"Hybrid search failed, falling back to vector: {e}")
            results = self.vector_search(
                query_vector=query_vector,
                top_k=top_k,
                book_id=book_id,
                calibre_id=calibre_id,
                section_type=section_type,
                chunk_type=chunk_type,
                language=language,
                source_id=source_id,
            )
            return results

        return self._results_to_dicts(results)

    def vector_search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        book_id: str | None = None,
        calibre_id: int | None = None,
        section_type: str | None = None,
        chunk_type: str | None = None,
        language: str | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Pure vector similarity search.

        Args:
            query_vector: Query embedding vector
            top_k: Number of results to return
            book_id: Filter by book_id
            calibre_id: Filter by Calibre ID
            section_type: Filter by section type
            chunk_type: Filter by chunk type
            language: Filter by language code
            source_id: Filter by adapter-agnostic source ID

        Returns:
            List of result dictionaries with metadata and scores
        """
        if self.table is None:
            return []

        filters = self._build_filter(
            book_id=book_id,
            calibre_id=calibre_id,
            section_type=section_type,
            chunk_type=chunk_type,
            language=language,
            source_id=source_id,
        )

        search = self.table.search(query_vector.tolist())

        if filters:
            search = search.where(filters)

        results = search.limit(top_k).to_pandas()
        return self._results_to_dicts(results)

    def fts_search(
        self,
        query_text: str,
        top_k: int = 10,
        book_id: str | None = None,
        calibre_id: int | None = None,
        section_type: str | None = None,
        chunk_type: str | None = None,
        language: str | None = None,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Pure full-text search (good for proper nouns, dates, exact phrases).

        Args:
            query_text: Search query text
            top_k: Number of results to return
            book_id: Filter by book_id
            calibre_id: Filter by Calibre ID
            section_type: Filter by section type
            chunk_type: Filter by chunk type
            language: Filter by language code
            source_id: Filter by adapter-agnostic source ID

        Returns:
            List of result dictionaries with metadata and scores
        """
        if self.table is None:
            return []

        filters = self._build_filter(
            book_id=book_id,
            calibre_id=calibre_id,
            section_type=section_type,
            chunk_type=chunk_type,
            language=language,
            source_id=source_id,
        )

        search = self.table.search(query_text, query_type="fts")

        if filters:
            search = search.where(filters)

        results = search.limit(top_k).to_pandas()
        return self._results_to_dicts(results)

    def _build_filter(
        self,
        book_id: str | None = None,
        calibre_id: int | None = None,
        section_type: str | None = None,
        chunk_type: str | None = None,
        language: str | None = None,
        source_id: str | None = None,
    ) -> str | None:
        """Build SQL-like filter string for LanceDB queries."""
        conditions = []

        if book_id:
            conditions.append(f"book_id = '{_sql_quote(book_id)}'")

        if source_id:
            # Prefer source_id; fall back to calibre_id for old data
            conditions.append(
                f"(source_id = '{source_id}' OR calibre_id = {source_id})"
                if source_id.isdigit()
                else f"source_id = '{_sql_quote(source_id)}'"
            )
        elif calibre_id:
            conditions.append(f"calibre_id = {calibre_id}")

        if section_type:
            if section_type == SectionType.MAIN:
                # Exclude front_matter and back_matter
                conditions.append(f"(section_type = '{SectionType.MAIN_CONTENT}' OR section_type = '')")
            else:
                conditions.append(f"section_type = '{section_type}'")

        if chunk_type:
            if chunk_type == ChunkType.CONTENT:
                # Include both flat chunks ("content") and hierarchical children ("child")
                # Parents are excluded — they serve as context, not search targets
                conditions.append(f"(chunk_type = '{ChunkType.CONTENT}' OR chunk_type = '{ChunkType.CHILD}')")
            elif chunk_type == ChunkType.ANNOTATIONS_AND_COMMENTS:
                conditions.append(f"(chunk_type = '{ChunkType.ANNOTATION}' OR chunk_type = '{ChunkType.CALIBRE_COMMENT}')")
            else:
                conditions.append(f"chunk_type = '{chunk_type}'")

        if language:
            conditions.append(f"language = '{_sql_quote(language)}'")

        return " AND ".join(conditions) if conditions else None

    # LanceDB score columns mapped to (unified_key, transform).
    # _distance is cosine distance (lower = better), so we invert it.
    # _relevance_score and _score are already higher-is-better.
    _SCORE_COLUMNS = {
        "_distance": lambda d: 1.0 - d,
        "_relevance_score": lambda s: s,
        "_score": lambda s: s,
    }

    def _results_to_dicts(self, df) -> list[dict[str, Any]]:
        """Convert pandas DataFrame results to list of dictionaries."""
        if df is None or len(df) == 0:
            return []

        results = []
        for _, row in df.iterrows():
            result = row.to_dict()

            # Normalise search-type-specific score into a unified 'score' field
            result['score'] = 0.0
            for col, transform in self._SCORE_COLUMNS.items():
                if col in result:
                    result['score'] = transform(result.pop(col))
                    break

            # Remove vector from results (too large for downstream consumers)
            result.pop('vector', None)

            results.append(result)

        return results

    def _delete_where(self, condition: str) -> int:
        """Delete rows matching a SQL condition and return the count removed."""
        if self.table is None:
            return 0
        count_before = self.count()
        self.table.delete(condition)
        return count_before - self.count()

    def delete_by_book_id(self, book_id: str) -> int:
        """Delete all chunks for a specific book."""
        return self._delete_where(f"book_id = '{_sql_quote(book_id)}'")

    def delete_by_book_id_and_type(self, book_id: str, chunk_type: str) -> int:
        """Delete chunks for a specific book filtered by chunk_type."""
        return self._delete_where(
            f"book_id = '{_sql_quote(book_id)}' AND chunk_type = '{_sql_quote(chunk_type)}'"
        )

    def delete_by_book_id_except_annotations(self, book_id: str) -> int:
        """Delete all chunks for a book except annotation chunks.

        Used by embed_prepared's replace path (finding 2.2): the prepared JSONL
        carries only freshly parsed content/comment chunks, never user
        annotations (highlights are imported separately and may be weeks newer
        than the prepare files). Deleting everything would silently drop those
        annotations, so the replace keeps ChunkType.ANNOTATION rows intact.
        Every stored row has a concrete chunk_type (add_chunks defaults it to
        CONTENT), so the ``!=`` filter never leaves NULL-typed rows behind.
        """
        return self._delete_where(
            f"book_id = '{_sql_quote(book_id)}' "
            f"AND chunk_type != '{_sql_quote(ChunkType.ANNOTATION)}'"
        )

    def update_metadata_fields(self, book_id: str, updates: dict) -> int:
        """
        Update metadata fields in all chunks of a book WITHOUT re-computing embeddings.
        Useful for updating tags, author, title, etc. after Calibre edits.

        Only updates columns that already exist in the table schema.
        New columns (like metadata_hash) will be added when new chunks are inserted
        via add_chunks(), but cannot be added via update() alone.

        ``pipeline_version`` is refused for the same reason in a different
        direction: it is a claim about who wrote a row, and a marker that any
        caller may overwrite is a docstring again (finding 1.7a). It is set by
        ``add_chunks`` and by nothing else.

        ``text`` and ``vector`` are refused (finding 1.7b). Both are columns in
        the schema, so the filter below would have written them like any other
        field — the only thing preventing a text update without a matching
        embedding was that no caller had written that line yet. The result
        would be a row whose vector describes text it no longer contains: no
        query can detect it, and no scan repairs it, because every hash the
        watchdog compares stays unchanged.

        Args:
            book_id: The book_id to update
            updates: Dict of field names to new values (e.g. {'tags': 'new,tags', 'metadata_hash': 'abc123'})

        Returns:
            Number of chunks updated (approximate)

        Raises:
            ValueError: if ``updates`` contains ``text``, ``vector`` or
                ``pipeline_version``.
        """
        forbidden = {'text', 'vector'} & set(updates)
        if forbidden:
            raise ValueError(
                f"update_metadata_fields refuses {sorted(forbidden)}: changing "
                f"chunk text or its vector here would leave the two describing "
                f"different things. Re-embed the book instead "
                f"(index_book(force=True) or the prepare/embed path)."
            )
        if 'pipeline_version' in updates:
            raise ValueError(
                "update_metadata_fields refuses 'pipeline_version': it records "
                "which generation of the pipeline composed a row, and a run "
                "that did not compose it cannot say. add_chunks sets it."
            )

        if self.table is None:
            return 0

        # Filter updates to only include columns that exist in the current table schema
        existing_columns = set(self.table.schema.names)
        safe_updates = {k: v for k, v in updates.items() if k in existing_columns}
        skipped = set(updates.keys()) - set(safe_updates.keys())
        if skipped:
            logger.warning(f"Skipping columns not yet in schema: {skipped}")

        if not safe_updates:
            return 0

        # LanceDB update: set columns where condition matches
        self.table.update(where=f"book_id = '{_sql_quote(book_id)}'", values=safe_updates)
        # We can't easily count updates, so return total chunks for this book.
        # Projected count (pattern: has_parent_chunks) — no row/vector data
        # materialised, unlike the old search().to_list() (finding 5.6).
        try:
            lance_dataset = self.table.to_lance()
            return lance_dataset.count_rows(
                filter=f"book_id = '{_sql_quote(book_id)}'"
            )
        except Exception:
            return 0

    def mark_pending_external(self, book_id: str) -> int:
        """Mark all chunks of a book as provisionally light (Hardware-Tiers-V2 §12).

        Set after a full-external library indexes a new title provisionally light
        (flat, local) so it is immediately searchable; the marker records that the
        chunks still await an external hierarchical re-embed. Returns the number
        of chunks marked.
        """
        return self.update_metadata_fields(book_id, {"pending_external": 1})

    def clear_pending_external(self, book_id: str) -> int:
        """Clear the pending_external marker for a book (Hardware-Tiers-V2 §12).

        Replacing the provisional chunks with externally embedded ones clears the
        marker implicitly (fresh chunks default to 0); this is the explicit path.
        """
        return self.update_metadata_fields(book_id, {"pending_external": 0})

    def get_pending_external_book_ids(self) -> set[str]:
        """Return the set of book_ids whose chunks are marked pending_external (§12).

        Uses column projection — only reads book_id + pending_external, never the
        text/vector columns. Books with no marker column yet (old DB before the
        migration ran) yield an empty set.
        """
        if self.table is None:
            return set()

        columns = ['book_id', 'pending_external']
        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            if 'pending_external' not in existing:
                return set()
            rows = lance_dataset.to_table(
                columns=[c for c in columns if c in existing]
            ).to_pylist()
        except Exception:
            try:
                df = self.table.search().select(columns).limit(10_000_000).to_pandas()
                if 'pending_external' not in df.columns:
                    return set()
                rows = df.to_dict(orient='records')
            except Exception:
                return set()

        result: set[str] = set()
        for row in rows:
            val = row.get('pending_external')
            if val is None or (isinstance(val, float) and val != val):  # None / NaN
                continue
            if int(val) == 1:
                bid = row.get('book_id')
                if bid:
                    result.add(str(bid))
        return result

    def has_parent_chunks(self) -> bool:
        """True if the index holds any hierarchical PARENT chunk (finding 1.1).

        A parent chunk only exists in a hierarchical (full-local / externally
        embedded) index, so this distinguishes such a database from a flat one.
        Column-projected count — reads only ``chunk_type``. Empty/missing table
        or no ``chunk_type`` column → False.
        """
        if self.table is None:
            return False
        try:
            lance_dataset = self.table.to_lance()
            if 'chunk_type' not in lance_dataset.schema.names:
                return False
            return lance_dataset.count_rows(
                filter=f"chunk_type = '{_sql_quote(ChunkType.PARENT)}'"
            ) > 0
        except Exception:
            return False

    def get_book_ids_without_parent_chunks(self) -> set[str]:
        """Return book_ids that have flat content chunks but no PARENT/CHILD.

        The catch-up counterpart to :meth:`get_pending_external_book_ids`
        (finding 1.2).  The ``pending_external`` marker is only written under
        ``mode: full-external``; a book indexed under ``light`` carries no
        marker at all, yet still waits for a hierarchical re-embed.  What the
        index does record either way is the shape of its chunks, so a book
        with ``content`` chunks and no hierarchical ones is exactly such a
        candidate.

        Books whose only chunks are metadata stubs or annotations are not
        returned — they hold no fulltext to re-embed.  Callers must gate this
        on :meth:`has_parent_chunks`, or a deliberately flat library nominates
        its entire corpus.

        Uses column projection — reads only ``book_id`` + ``chunk_type``,
        never the text/vector columns.
        """
        if self.table is None:
            return set()

        columns = ['book_id', 'chunk_type']
        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            if not {'book_id', 'chunk_type'} <= existing:
                return set()
            rows = lance_dataset.to_table(columns=columns).to_pylist()
        except Exception:
            try:
                df = self.table.search().select(columns).limit(10_000_000).to_pandas()
                if 'chunk_type' not in df.columns or 'book_id' not in df.columns:
                    return set()
                rows = df.to_dict(orient='records')
            except Exception:
                return set()

        flat: set[str] = set()
        hierarchical: set[str] = set()
        for row in rows:
            bid = row.get('book_id')
            if not bid:
                continue
            chunk_type = row.get('chunk_type')
            if chunk_type == ChunkType.CONTENT:
                flat.add(str(bid))
            elif chunk_type in (ChunkType.PARENT, ChunkType.CHILD):
                hierarchical.add(str(bid))

        return flat - hierarchical

    def delete_by_calibre_id(self, calibre_id: int) -> int:
        """Delete all chunks for a specific Calibre ID."""
        return self._delete_where(f"calibre_id = {calibre_id}")

    def _query_where(self, condition: str, limit: int = 100) -> list[dict[str, Any]]:
        """Run a filtered query and return results as dicts."""
        if self.table is None:
            return []
        df = self.table.search().where(condition).limit(limit).to_pandas()
        return self._results_to_dicts(df)

    def get_by_book_id(self, book_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get all chunks for a specific book."""
        return self._query_where(f"book_id = '{_sql_quote(book_id)}'", limit)

    def get_by_calibre_id(self, calibre_id: int, limit: int = 100) -> list[dict[str, Any]]:
        """Get all chunks for a specific Calibre ID."""
        return self._query_where(f"calibre_id = {calibre_id}", limit)

    def get_book_state(self, book_id: str) -> dict[str, Any]:
        """Targeted single-book state for smart-update decisions (finding 8.7).

        Reads only small columns, but for ALL chunks of the book — unlike
        get_by_book_id(limit=...), whose arbitrary row window missed the
        annotation/metadata hashes for books with many chunks and thereby
        triggered pointless re-embedding on every scan.

        Returns:
            Dict with 'total', 'has_content', 'content_count' (counting
            HIERARCHICAL_TYPES), 'metadata_hash' (from content chunks,
            comment chunks as fallback), 'annotation_hash' (from annotation
            chunks) and 'format'.
        """
        state: dict[str, Any] = {
            'total': 0, 'has_content': False, 'content_count': 0,
            'metadata_hash': '', 'annotation_hash': '', 'format': '',
        }
        if self.table is None:
            return state

        condition = f"book_id = '{_sql_quote(book_id)}'"
        columns = ['chunk_type', 'metadata_hash', 'annotation_hash', 'format']
        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            projection = [c for c in columns if c in existing]
            rows = lance_dataset.to_table(columns=projection, filter=condition).to_pylist()
        except Exception as e:
            logger.warning(f"Column projection failed in get_book_state, using search fallback: {e}")
            df = self.table.search().where(condition).limit(10_000_000).to_pandas()
            rows = df.to_dict(orient='records')

        def _clean(value: Any) -> str:
            if value is None:
                return ''
            if isinstance(value, float) and value != value:  # NaN check
                return ''
            s = str(value)
            return '' if s == 'nan' else s

        meta_from_content = ''
        meta_from_comment = ''
        state['total'] = len(rows)
        for row in rows:
            ctype = row.get('chunk_type')
            # CONTENT_TYPES, not HIERARCHICAL_TYPES (finding 1.13). The two
            # differ on EXCHANGE, and this method and
            # get_hashes_for_indexed_books used one each — so a dialogue-chunked
            # document had fulltext according to the watchdog and no content
            # according to index_book, which would then replace its structured
            # chunks with generic extraction. CONTENT_TYPES is authoritative for
            # "this book has fulltext"; both queries now use it.
            if ctype in ChunkType.CONTENT_TYPES:
                state['content_count'] += 1
                if not meta_from_content:
                    meta_from_content = _clean(row.get('metadata_hash'))
                if not state['format']:
                    state['format'] = _clean(row.get('format'))
            elif ctype == ChunkType.CALIBRE_COMMENT and not meta_from_comment:
                meta_from_comment = _clean(row.get('metadata_hash'))
            elif ctype == ChunkType.ANNOTATION and not state['annotation_hash']:
                state['annotation_hash'] = _clean(row.get('annotation_hash'))

        state['metadata_hash'] = meta_from_content or meta_from_comment
        state['has_content'] = state['content_count'] > 0
        if not state['format']:
            for row in rows:
                fmt = _clean(row.get('format'))
                if fmt:
                    state['format'] = fmt
                    break
        return state

    def get_by_source_id(self, source_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get all chunks for a source ID (with calibre_id fallback)."""
        if source_id.isdigit():
            return self._query_where(
                f"(source_id = '{source_id}' OR calibre_id = {source_id})", limit
            )
        return self._query_where(f"source_id = '{_sql_quote(source_id)}'", limit)

    def delete_by_source_id(self, source_id: str) -> int:
        """Delete all chunks for a source ID (with calibre_id fallback)."""
        if source_id.isdigit():
            return self._delete_where(
                f"(source_id = '{source_id}' OR calibre_id = {source_id})"
            )
        return self._delete_where(f"source_id = '{_sql_quote(source_id)}'")

    def get_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """Get a single chunk by its ID (used for parent lookup)."""
        results = self._query_where(f"id = '{_sql_quote(chunk_id)}'", limit=1)
        return results[0] if results else None

    def get_book_ids_for_skip_check(self) -> list[dict[str, Any]]:
        """
        Efficiently retrieve the minimal columns needed to build the set of already-indexed
        book IDs for --skip-existing checks.

        Only loads 'book_id', 'chunk_type', and 'indexed_at' — skipping the large 'text'
        and 'vector' columns.  For a 100 k-chunk database this is typically 10-50x faster
        than loading all columns.

        Returns:
            List of dicts with keys 'book_id', 'chunk_type', 'indexed_at'.
        """
        if self.table is None:
            return []

        columns = ['book_id', 'chunk_type', 'indexed_at']

        try:
            # Primary path: PyArrow column projection via the underlying Lance dataset.
            # This is the most efficient approach — only the requested columns are read
            # from disk; 'text' and 'vector' are never decoded.
            lance_dataset = self.table.to_lance()
            # Filter to only columns that actually exist in the schema
            existing = set(lance_dataset.schema.names)
            projection = [c for c in columns if c in existing]
            arrow_table = lance_dataset.to_table(columns=projection)
            return arrow_table.to_pandas().to_dict(orient='records')
        except Exception as e:
            logger.warning(f"Fast column projection failed, using search fallback: {e}")

        try:
            # Fallback: LanceDB search-builder with .select() (available in recent versions)
            df = (
                self.table.search()
                .select(columns)
                .limit(10_000_000)  # Large enough to cover any realistic library
                .to_pandas()
            )
            available = [c for c in columns if c in df.columns]
            return df[available].to_dict(orient='records')
        except Exception as e:
            logger.warning(f"Search-based column projection failed, loading full table: {e}")

        # Last-resort fallback: full table scan (original behaviour)
        df = self.table.to_pandas()
        available = [c for c in columns if c in df.columns]
        return df[available].to_dict(orient='records')

    def get_hashes_for_indexed_books(self) -> dict[int, dict[str, str]]:
        """Return {calibre_id: {metadata_hash, annotation_hash, book_id}} for all indexed books.

        Uses efficient column projection — only reads the hash columns, not text/vector.
        One representative row per calibre_id (the first content chunk found).
        """
        if self.table is None:
            return {}

        columns = ['calibre_id', 'book_id', 'metadata_hash', 'annotation_hash', 'chunk_type']

        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            projection = [c for c in columns if c in existing]
            arrow_table = lance_dataset.to_table(columns=projection)
            rows = arrow_table.to_pandas().to_dict(orient='records')
        except Exception:
            try:
                df = self.table.search().select(columns).limit(10_000_000).to_pandas()
                available = [c for c in columns if c in df.columns]
                rows = df[available].to_dict(orient='records')
            except Exception:
                df = self.table.to_pandas()
                available = [c for c in columns if c in df.columns]
                rows = df[available].to_dict(orient='records')

        def _clean(value: Any) -> str:
            """Coerce LanceDB value to a string, treating NULL / NaN as empty.

            Pandas loads NULL hash columns as ``float('nan')`` in older
            databases, and ``nan or ''`` returns ``nan`` (truthy) — so a naive
            ``str(value or '')`` would yield ``'nan'`` and produce false
            positives on every subsequent hash comparison. Guard explicitly.
            """
            if value is None:
                return ''
            if isinstance(value, float) and value != value:  # NaN check
                return ''
            s = str(value)
            return '' if s == 'nan' else s

        result: dict[int, dict[str, str]] = {}
        content_seen: set[int] = set()
        for row in rows:
            cid = row.get('calibre_id')
            if cid is None or (isinstance(cid, float) and cid != cid):
                continue
            cid = int(cid)
            chunk_type = row.get('chunk_type')
            new_meta = _clean(row.get('metadata_hash'))
            new_annot = _clean(row.get('annotation_hash'))
            new_book = _clean(row.get('book_id'))

            if chunk_type in ChunkType.CONTENT_TYPES:
                content_seen.add(cid)

            if cid in result:
                # Prefer content chunks (flat or hierarchical) over annotation
                # chunks for metadata_hash — CHILD/PARENT never won here, which
                # worked only because update_metadata_fields syncs all rows of
                # a book; any per-type write path could quietly break it.
                if (chunk_type in ChunkType.HIERARCHICAL_TYPES
                        or chunk_type == ChunkType.CALIBRE_COMMENT) and new_meta:
                    result[cid]['metadata_hash'] = new_meta
            else:
                result[cid] = {
                    'book_id': new_book,
                    'metadata_hash': new_meta,
                    'annotation_hash': new_annot,
                }
            # Capture annotation hash from annotation chunks
            if chunk_type == ChunkType.ANNOTATION and new_annot:
                result[cid]['annotation_hash'] = new_annot

        for cid in result:
            result[cid]['has_content'] = cid in content_seen

        return result

    def get_hashes_by_book_id(self) -> dict[str, dict[str, str]]:
        """Return {book_id: {metadata_hash, annotation_hash}} keyed by string book_id.

        Adapter-agnostic counterpart to get_hashes_for_indexed_books(), which
        uses calibre_id (int) and therefore skips non-Calibre items (Zotero,
        Folder) where calibre_id is NULL.
        """
        if self.table is None:
            return {}

        columns = ['book_id', 'metadata_hash', 'annotation_hash', 'chunk_type']

        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            projection = [c for c in columns if c in existing]
            arrow_table = lance_dataset.to_table(columns=projection)
            rows = arrow_table.to_pandas().to_dict(orient='records')
        except Exception:
            try:
                df = self.table.search().select(columns).limit(10_000_000).to_pandas()
                available = [c for c in columns if c in df.columns]
                rows = df[available].to_dict(orient='records')
            except Exception:
                df = self.table.to_pandas()
                available = [c for c in columns if c in df.columns]
                rows = df[available].to_dict(orient='records')

        def _clean(value: Any) -> str:
            if value is None:
                return ''
            if isinstance(value, float) and value != value:
                return ''
            s = str(value)
            return '' if s == 'nan' else s

        result: dict[str, dict[str, str]] = {}
        for row in rows:
            bid = _clean(row.get('book_id'))
            if not bid:
                continue
            chunk_type = row.get('chunk_type')
            new_meta = _clean(row.get('metadata_hash'))
            new_annot = _clean(row.get('annotation_hash'))

            if bid in result:
                if (chunk_type in ChunkType.HIERARCHICAL_TYPES
                        or chunk_type == ChunkType.CALIBRE_COMMENT) and new_meta:
                    result[bid]['metadata_hash'] = new_meta
            else:
                result[bid] = {'metadata_hash': new_meta, 'annotation_hash': new_annot}

            if chunk_type == ChunkType.ANNOTATION and new_annot:
                result[bid]['annotation_hash'] = new_annot

        return result

    def get_indexed_books(self) -> list[dict[str, Any]]:
        """
        Get list of all indexed books with statistics.

        Returns:
            List of dictionaries with book_id, title, author, chunk_count, etc.
        """
        if self.table is None:
            return []

        # Only metadata columns are needed; never decode 'text'/'vector'/
        # 'window_text', which dominate row size. Column projection keeps this
        # cheap even on libraries with millions of chunks (see the projection
        # pattern used by get_*_hashes_* above).
        columns = [
            "book_id", "book_title", "author", "calibre_id",
            "source_id", "year", "format", "tags", "id", "indexed_at",
        ]
        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            projection = [c for c in columns if c in existing]
            df = lance_dataset.to_table(columns=projection).to_pandas()
        except Exception as e:
            logger.warning(f"Fast column projection failed, loading full table: {e}")
            df = self.table.to_pandas()

        # Group by book_id and aggregate
        agg_dict = {
            "book_title": "first",
            "author": "first",
            "calibre_id": "first",
            "year": "first",
            "format": "first",
            "tags": "first",
            "id": "count",  # Count chunks
            "indexed_at": "max",  # Latest indexing time
        }
        col_names = [
            "book_id", "title", "author", "calibre_id",
            "year", "format", "tags", "chunks", "indexed_at",
        ]
        if "source_id" in df.columns:
            # Appended LAST to match agg_dict insertion order: the aggregated
            # frame lists source_id as its final column. Inserting it earlier
            # (the previous behaviour) shifted every following column by one,
            # so tags ended up holding the int chunk count, year held the
            # format, etc. — the real root cause of the web UI tag crash.
            agg_dict["source_id"] = "first"
            col_names.append("source_id")

        books = df.groupby("book_id").agg(agg_dict).reset_index()
        books.columns = col_names

        return books.to_dict(orient="records")

    def count(self) -> int:
        """Get total number of chunks in the database."""
        if self.table is None:
            return 0
        return self.table.count_rows()

    def get_stats(self) -> dict[str, Any]:
        """
        Get database statistics.

        Returns:
            Dictionary with total_chunks, total_books, file_types, etc.
        """
        if self.table is None:
            return {
                "total_chunks": 0,
                "total_books": 0,
                "avg_chunks_per_book": 0,
                "file_types": {},
                "section_types": {},
            }

        # Project only the columns the stats need — avoids decoding text/vector/
        # window_text for every chunk on each page load.
        columns = ["book_id", "chunk_type", "format", "section_type", "language"]
        try:
            lance_dataset = self.table.to_lance()
            existing = set(lance_dataset.schema.names)
            projection = [c for c in columns if c in existing]
            df = lance_dataset.to_table(columns=projection).to_pandas()
        except Exception as e:
            logger.warning(f"Fast column projection failed, loading full table: {e}")
            df = self.table.to_pandas()

        return {
            "total_chunks": len(df),
            "total_books": df["book_id"].nunique(),
            "avg_chunks_per_book": len(df) / max(1, df["book_id"].nunique()),
            "chunk_types": df["chunk_type"].value_counts().to_dict() if "chunk_type" in df else {},
            "file_types": df["format"].value_counts().to_dict() if "format" in df else {},
            "section_types": df["section_type"].value_counts().to_dict() if "section_type" in df else {},
            "languages": df["language"].value_counts().to_dict() if "language" in df else {},
        }

    def close(self):
        """Close the database connection."""
        # LanceDB doesn't require explicit close, but we reset references
        self.table = None
        self.db = None
