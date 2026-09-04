"""Indexing component: book indexing (3 paths), phase-1 metadata stubs,
prepare/embed two-phase pipeline, smart updates, metadata extraction and
hashing. Extracted from the ArchillesRAG monolith (8.16)."""
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from tqdm import tqdm

from src.archilles.constants import ChunkType
from src.archilles.indexer import IndexingCheckpoint
from src.calibre_db import CalibreDB
from src.calibre_mcp.annotations import get_combined_annotations

# Characters that are invalid in Windows filenames (superset of POSIX).
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# PDFs shorter than this never trigger the needs-OCR warning. Reference
# managers routinely attach tiny scanned PDFs (Zotero stores publisher TOC /
# preview scans); flagging each one buries the real OCR candidates in noise.
OCR_WARN_MIN_PAGES = 6


def prepared_jsonl_name(book_id: str) -> str:
    """Filename for a book's prepared-chunks JSONL (one file per book).

    Keyed by the adapter-unique ``book_id``, not ``calibre_id`` — non-Calibre
    sources (Zotero keys, folder ids) have no Calibre id, so every item used
    to collide on ``0.jsonl`` and only one book per library ever got prepared
    (review 2026-07-03, finding 5.1). For Calibre books ``book_id`` is the
    numeric id as a string, so existing ``{calibre_id}.jsonl`` corpora keep
    matching.

    Filesystem-unsafe characters are replaced; whenever sanitisation changes
    the name (or empties it), a short hash of the original id is appended so
    distinct ids ("a/b" vs "a_b") cannot map to the same file.
    """
    original = str(book_id)
    safe = _UNSAFE_FILENAME_CHARS.sub('_', original).strip(' .')
    if not safe or safe != original:
        digest = hashlib.md5(original.encode('utf-8')).hexdigest()[:8]
        safe = f"{safe or 'book'}-{digest}"
    return f"{safe}.jsonl"


def read_prepared_header(path: Path) -> Optional[dict]:
    """Read and validate a prepared JSONL's header.

    A prepare interrupted mid-write (process killed) leaves a file with a
    valid header line but fewer chunk lines than ``header['chunk_count']``
    promises. Both skip checks (``prepare_book``'s own and
    ``batch_prepare``'s pre-check) used to accept such a file forever as
    "already prepared" because they only parsed the header. This helper
    validates header AND actual line count together, returning ``None`` on
    any read/parse failure or count mismatch so callers fall through to
    re-prepare instead of trusting a truncated file (review 2026-07-03,
    finding 2.3).
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            header = json.loads(f.readline())
            if not header.get('_header'):
                return None
            chunk_lines = sum(1 for line in f if line.strip())
    except (OSError, json.JSONDecodeError):
        return None

    expected = header.get('chunk_count')
    if expected is not None and chunk_lines != expected:
        return None

    return header


class Indexer:
    """Back-reference pattern — see Searcher."""

    def __init__(self, rag):
        self._rag = rag
        # Cached ModularPipeline — created on first use so batch runs do not
        # reload the embedding model per book.
        self._modular_pipeline = None

    def _apply_book_metadata_to_chunk(self, chunk_data: Dict[str, Any],
                                       book_metadata: Dict[str, Any]) -> None:
        """Copy standard book metadata fields into a chunk dict (in-place)."""
        if not book_metadata:
            return
        if book_metadata.get('author'):
            chunk_data['author'] = book_metadata['author']
        if book_metadata.get('title'):
            chunk_data['book_title'] = book_metadata['title']
        if book_metadata.get('year'):
            chunk_data['year'] = book_metadata['year']
        if book_metadata.get('publisher'):
            chunk_data['publisher'] = book_metadata['publisher']
        if book_metadata.get('calibre_id'):
            chunk_data['calibre_id'] = book_metadata['calibre_id']
        if book_metadata.get('source_id'):
            chunk_data['source_id'] = book_metadata['source_id']
        if book_metadata.get('tags'):
            chunk_data['tags'] = self._rag._format_tags(book_metadata['tags'])

    def _apply_hierarchical_chunking(self, extracted, book_id: str) -> None:
        """Replace extracted.chunks with hierarchical parent/child chunks (in-place).

        Builds the hierarchy from the already-extracted, structure-aware chunks
        so children inherit section/page metadata and offsets — keeping them
        citation-grade (see ``BaseExtractor._group_chunks_hierarchically``).
        Book-level metadata (title/author/tags) is applied downstream in
        ``_build_chunk_dicts`` for parent and child alike.
        """
        from src.extractors.base import BaseExtractor

        extracted.chunks = BaseExtractor._group_chunks_hierarchically(
            extracted.chunks,
            book_id=book_id,
            parent_size=self._rag.recipe.parent_size,
        )

    def _build_chunk_dicts(
        self,
        extracted,
        book_id: str,
        book_metadata: Dict[str, Any],
        indexed_at: str,
        meta_hash: str,
    ) -> List[Dict[str, Any]]:
        """Build LanceDB chunk dicts from extracted chunks.

        Shared by index_book and prepare_book to avoid duplication.
        """
        chunks = []
        for i, chunk in enumerate(extracted.chunks):
            chunk_id = chunk.get('chunk_id', f"{book_id}_chunk_{i}")
            chunk_type = chunk.get('metadata', {}).get('chunk_type', ChunkType.CONTENT)

            chunk_data = {
                'id': chunk_id,
                'text': chunk['text'],
                'book_id': book_id,
                'book_title': extracted.metadata.file_path.stem,
                'chunk_index': i,
                'chunk_type': chunk_type,
                'format': extracted.metadata.detected_format,
                'indexed_at': indexed_at,
            }

            chunk_meta = chunk.get('metadata', {})
            for field in ('char_start', 'char_end'):
                if chunk_meta.get(field) is not None:
                    chunk_data[field] = chunk_meta[field]
            if chunk.get('window_text'):
                chunk_data['window_text'] = chunk['window_text']

            if chunk.get('parent_id') is not None:
                chunk_data['parent_id'] = chunk['parent_id']

            self._apply_book_metadata_to_chunk(chunk_data, book_metadata)

            if meta_hash:
                chunk_data['metadata_hash'] = meta_hash

            chunk_data['source_file'] = str(extracted.metadata.file_path)

            for src_key, dst_key in self._rag._CHUNK_META_KEYS:
                if chunk_meta.get(src_key):
                    chunk_data[dst_key] = chunk_meta[src_key]

            chunks.append(chunk_data)
        return chunks

    def _detect_needs_ocr(self, extracted) -> bool:
        """Detect scanned/mostly-scanned PDFs from extraction statistics.

        Prints a warning and returns True when the text yield suggests the
        PDF is (mostly) scanned and should be re-indexed with --enable-ocr.
        Shared by index_book and prepare_book so the thresholds cannot drift.
        """
        if extracted.metadata.detected_format != 'pdf':
            return False

        total_pages = extracted.metadata.total_pages or 0
        total_words = extracted.metadata.total_words or 0

        # Tiny PDFs are not worth an OCR round-trip (see OCR_WARN_MIN_PAGES).
        # An unknown page count (0) stays eligible — it could be a big book.
        if 0 < total_pages < OCR_WARN_MIN_PAGES:
            return False

        if not extracted.chunks:
            print("  ⚠️  No text extracted — likely fully scanned. Re-index with --enable-ocr.")
            return True

        if total_pages >= 3 and total_words > 0 and (total_words / total_pages) < 150:
            # Also check page coverage: if most pages have chunks, it's front-matter, not scanned
            pages_with_text = len(set(
                c['metadata'].get('page', 0)
                for c in extracted.chunks
                if isinstance(c.get('metadata'), dict)
            ))
            page_coverage = pages_with_text / total_pages if total_pages > 0 else 1.0
            if page_coverage < 0.4:
                wpp = total_words // total_pages
                print(f"  ⚠️  Only {total_words}w across {total_pages}p ({wpp}w/p), text on {pages_with_text}/{total_pages} pages — likely mostly scanned. Re-index with --enable-ocr.")
                return True

        return False

    def _build_annotation_chunks(
        self,
        annotations: List[Dict[str, Any]],
        book_id: str,
        book_title: str,
        annotation_hash: str,
        book_format: str,
        metadata_hash: str,
        book_metadata: Optional[Dict[str, Any]],
        indexed_at: Optional[str] = None,
    ) -> tuple:
        """Build annotation chunk dicts plus their texts for batched embedding.

        Returns:
            (chunks, texts) — parallel lists; embedding is left to the caller
            so both call sites (index_book, _update_metadata_only) encode all
            annotation texts in one batch.
        """
        indexed_at = indexed_at or datetime.now().isoformat()
        chunks, texts = [], []
        for idx, annot in enumerate(annotations):
            annot_text = self._build_annotation_text(annot)
            if not annot_text:
                continue

            chunk = {
                'id': f"{book_id}_annot_{idx}",
                'text': annot_text,
                'book_id': book_id,
                'book_title': book_title,
                'chunk_index': -(idx + 10),  # Negative to distinguish from content
                'chunk_type': ChunkType.ANNOTATION,
                'annotation_type': annot.get('type', ''),
                'annotation_source': annot.get('source', ''),
                'annotation_hash': annotation_hash,
                'page_number': annot.get('page', 0) or 0,
                'format': book_format,
                'indexed_at': indexed_at,
                'metadata_hash': metadata_hash,
            }
            self._apply_book_metadata_to_chunk(chunk, book_metadata)
            chunks.append(chunk)
            texts.append(annot_text)
        return chunks, texts

    def _resolve_annotations(self, book_id: Optional[str],
                             book_path: Any) -> List[Dict[str, Any]]:
        """Canonical annotation source for a book (finding 1.4).

        ``get_combined_annotations`` reads Calibre-viewer sidecars and
        PDF-embedded annotations — nothing else. Zotero's own reader stores
        highlights in ``zotero.sqlite``, so for a Zotero item that reader finds
        nothing, while the scanner's change proxy (``attachment_modified_at``)
        still fires, triggers a re-index, counts a delta update and exits 0.
        The highlights the project's USP rests on were never indexed.

        **Every** source goes through ``adapter.get_annotations`` — Calibre
        included. That is deliberate and was a design decision (user,
        2026-09-04): a permanent "except for Calibre" in the adapter
        abstraction is the very shape that produced this finding, and ARCHILLES
        is meant to be used by people whose library is not this one.

        Routing Calibre through the adapter is free of a re-index because
        ``CalibreAdapter.get_annotations`` was made *equivalent* to the call
        this method used to make, rather than being routed around: it passes
        the same filter arguments, and ``DocumentAnnotation`` carries ``source``
        so the round trip loses nothing. ``tests/test_adapter_annotations.py``
        pins both halves; without them the annotation hashes shift and the
        whole library re-indexes.

        The direct reader remains only for the adapterless legacy path.

        A failing adapter yields no annotations rather than falling back:
        a silent fallback would hide the failure, and for a Zotero item the
        Calibre reader would return an empty list anyway.
        """
        adapter = getattr(self._rag, "_adapter", None)
        if adapter is not None and book_id:
            try:
                doc_annotations = adapter.get_annotations(str(book_id))
            except Exception as exc:
                print(f"  ⚠️  adapter annotations failed for {book_id}: {exc}")
                return []
            return self._map_adapter_annotations(
                doc_annotations, getattr(adapter, "adapter_type", ""),
            )

        try:
            result = get_combined_annotations(
                book_path=str(book_path),
                include_pdf=True,
                exclude_toc_markers=True,
                min_length=20,
            )
            return result.get('annotations', [])
        except Exception:
            return []

    @staticmethod
    def _map_adapter_annotations(doc_annotations, adapter_type: str) -> List[Dict[str, Any]]:
        """``DocumentAnnotation`` → the dict shape the chunk builders read.

        ``source`` is finer than the adapter type — one Calibre book can carry
        both ``calibre_viewer`` and ``pdf`` annotations — so the adapter's own
        value wins and the type only fills in when it is absent.
        """
        return [
            {
                'highlighted_text': a.text or '',
                'notes': a.note or '',
                'type': a.annotation_type or 'highlight',
                'page': a.page,
                'source': getattr(a, 'source', '') or adapter_type,
                'timestamp': a.created or '',
            }
            for a in doc_annotations
        ]

    @staticmethod
    def _build_annotation_text(annot: Dict[str, Any]) -> str:
        """
        Build searchable annotation text from a highlight/note annotation.
        Returns empty string if annotation has no meaningful content.
        """
        highlighted = annot.get('highlighted_text', '').strip()
        notes = annot.get('notes', '').strip()

        if highlighted and notes:
            return f"[ANNOTATION] {highlighted} | Note: {notes}"
        if highlighted:
            return f"[ANNOTATION] {highlighted}"
        if notes:
            return f"[ANNOTATION_NOTE] {notes}"
        return ''

    def _extract_metadata(self, file_path: Path) -> Dict[str, Any]:
        """
        Universal metadata extraction with Calibre integration.

        Priority: Calibre metadata (user-curated) wins over file-embedded
        metadata.  File metadata (PDF/EPUB) is only read when Calibre has
        no title/author — avoids reopening the file that the extractor
        will parse anyway.

        Args:
            file_path: Path to book file

        Returns:
            Dictionary with metadata + isbn_source tracking
        """
        # Try Calibre database first (cheap SQLite read)
        calibre_metadata = self._extract_calibre_metadata(file_path)

        # Only open the file for metadata if Calibre didn't provide title+author
        has_core = calibre_metadata.get('title') and calibre_metadata.get('author')
        if has_core:
            file_metadata = {}
        else:
            file_ext = file_path.suffix.lower()
            if file_ext == '.pdf':
                file_metadata = self._extract_pdf_metadata(file_path)
            elif file_ext == '.epub':
                file_metadata = self._extract_epub_metadata(file_path)
            else:
                file_metadata = {}

        # Merge: file metadata as fallback, Calibre overwrites
        merged = {}
        merged.update(file_metadata)
        merged.update(calibre_metadata)

        # Track ISBN source
        if merged.get('isbn'):
            if calibre_metadata.get('isbn'):
                merged['isbn_source'] = 'calibre'
            elif file_metadata.get('isbn'):
                merged['isbn_source'] = 'file'

        return merged

    @staticmethod
    def _compute_metadata_hash(book_metadata: Dict[str, Any]) -> str:
        """Delegiert an src.archilles.hashing (Befund 7.15).

        Bleibt als @staticmethod auf ArchillesRAG erhalten, weil Tests sie als
        callable/patch-Target nutzen (test_engine_move, test_watchdog).
        """
        from src.archilles.hashing import compute_metadata_hash
        return compute_metadata_hash(book_metadata)

    @staticmethod
    def _compute_annotation_hash(annotations: List[Dict[str, Any]]) -> str:
        """Delegiert an src.archilles.hashing (Befund 7.15)."""
        from src.archilles.hashing import compute_annotation_hash
        return compute_annotation_hash(annotations)

    def _resolve_metadata_hash(self, book_id: Optional[str],
                               book_metadata: Dict[str, Any]) -> str:
        """Canonical metadata hash for change detection (finding 4.1a).

        Adapter-backed sources own their hash: Zotero hashes a different field
        set (title/authors/tags/abstract/date) than the Calibre-style
        comments/tags/title/author/publisher. The value stored at index time
        must match what the watchdog scanner later computes, so prefer the
        adapter's source-specific hash. Fall back to the Calibre-style hash over
        the extracted metadata when there is no adapter or the adapter does not
        implement hashing (SourceAdapter base returns "").

        Reindex-storm invariant: for Calibre this must stay byte-identical to
        the old ``_compute_metadata_hash(extracted_metadata)`` — CalibreAdapter
        delegates to the same canonical function (guarded by
        tests/test_zotero_hash_change_detection.py + test_p0_regressions.py).
        """
        adapter = self._rag._adapter
        if adapter is not None and book_id:
            try:
                adapter_hash = adapter.compute_metadata_hash(str(book_id))
            except Exception as exc:
                print(f"  ⚠️  adapter metadata hash failed for {book_id}: {exc} — "
                      f"falling back to extracted-metadata hash.")
                adapter_hash = ""
            if adapter_hash:
                return adapter_hash
        return self._compute_metadata_hash(book_metadata) if book_metadata else ''

    def _extract_pdf_metadata(self, file_path: Path) -> Dict[str, Any]:
        """Extract metadata from PDF files."""
        metadata = {}

        try:
            import fitz  # PyMuPDF

            doc = fitz.open(str(file_path))
            pdf_info = doc.metadata

            # Extract standard PDF metadata
            if pdf_info:
                # Author
                if pdf_info.get('author'):
                    metadata['author'] = pdf_info['author'].strip()

                # Title
                if pdf_info.get('title'):
                    metadata['title'] = pdf_info['title'].strip()

                # Subject (often used for subtitle/description)
                if pdf_info.get('subject'):
                    metadata['subject'] = pdf_info['subject'].strip()

                # Keywords
                if pdf_info.get('keywords'):
                    metadata['keywords'] = pdf_info['keywords'].strip()

                # Creation/Modification date (extract year)
                if pdf_info.get('creationDate'):
                    try:
                        # Format: D:20191203... (YYYYMMDD)
                        date_str = pdf_info['creationDate']
                        if date_str.startswith('D:') and len(date_str) >= 6:
                            year_str = date_str[2:6]
                            metadata['year'] = int(year_str)
                    except (ValueError, IndexError):
                        pass

                # Producer/Creator (often contains software used)
                if pdf_info.get('creator'):
                    metadata['creator'] = pdf_info['creator'].strip()

            doc.close()

        except Exception as e:
            # Silently fail if metadata extraction doesn't work
            pass

        return metadata

    def _extract_epub_metadata(self, file_path: Path) -> Dict[str, Any]:
        """Extract metadata from EPUB files using Dublin Core standards."""
        metadata = {}

        try:
            from ebooklib import epub

            book = epub.read_epub(str(file_path))

            # Dublin Core metadata (standard for EPUBs)
            # Author
            author = book.get_metadata('DC', 'creator')
            if author and author[0]:
                metadata['author'] = str(author[0][0]).strip()

            # Title
            title = book.get_metadata('DC', 'title')
            if title and title[0]:
                metadata['title'] = str(title[0][0]).strip()

            # Publisher
            publisher = book.get_metadata('DC', 'publisher')
            if publisher and publisher[0]:
                metadata['publisher'] = str(publisher[0][0]).strip()

            # Language
            language = book.get_metadata('DC', 'language')
            if language and language[0]:
                metadata['language'] = str(language[0][0]).strip()

            # Date/Year
            date = book.get_metadata('DC', 'date')
            if date and date[0]:
                try:
                    date_str = str(date[0][0])
                    # Try to extract year (formats: YYYY, YYYY-MM-DD, etc.)
                    year_match = re.search(r'(\d{4})', date_str)
                    if year_match:
                        metadata['year'] = int(year_match.group(1))
                except (ValueError, AttributeError):
                    pass

            # ISBN
            identifier = book.get_metadata('DC', 'identifier')
            if identifier:
                for id_tuple in identifier:
                    id_str = str(id_tuple[0]).strip()
                    # Check if it's an ISBN (formats: "isbn:123", "ISBN 123", "123" with 10/13 digits)
                    if 'isbn' in id_str.lower():
                        # Extract just the ISBN number (remove "isbn:" prefix)
                        isbn_clean = id_str.lower().replace('isbn:', '').replace('isbn', '').strip()
                        metadata['isbn'] = isbn_clean
                        break
                    elif id_str.replace('-', '').replace(' ', '').isdigit() and len(id_str.replace('-', '').replace(' ', '')) in [10, 13]:
                        # Pure numeric ISBN without prefix
                        metadata['isbn'] = id_str
                        break

            # Subject/Keywords
            subject = book.get_metadata('DC', 'subject')
            if subject and subject[0]:
                # Can have multiple subjects
                subjects = [str(s[0]).strip() for s in subject if s]
                metadata['subject'] = ', '.join(subjects)

            # Description (often contains book summary)
            description = book.get_metadata('DC', 'description')
            if description and description[0]:
                metadata['description'] = str(description[0][0]).strip()

        except Exception as e:
            # Silently fail if metadata extraction doesn't work
            pass

        return metadata

    def _extract_calibre_metadata(self, file_path: Path) -> Dict[str, Any]:
        """
        Extract metadata from source adapter or Calibre database (read-only).

        Uses the SourceAdapter if available; falls back to direct CalibreDB
        access for backward compatibility.

        Args:
            file_path: Path to book file

        Returns:
            Dictionary with metadata (empty if not in any known library)
        """
        # ── New path: use SourceAdapter ─────────────────────────
        if self._rag._adapter is not None:
            try:
                doc_meta = self._rag._adapter.get_metadata_by_path(file_path)
                if doc_meta is None:
                    return {}
                metadata = {}
                # Map DocumentMetadata fields to the dict format the rest
                # of rag_demo.py expects (matching _CALIBRE_FIELDS keys).
                if doc_meta.authors:
                    metadata['author'] = ' & '.join(doc_meta.authors)
                if doc_meta.title:
                    metadata['title'] = doc_meta.title
                if doc_meta.year:
                    metadata['year'] = doc_meta.year
                if doc_meta.publisher:
                    metadata['publisher'] = doc_meta.publisher
                if doc_meta.language:
                    metadata['language'] = doc_meta.language
                if doc_meta.identifiers.get('isbn'):
                    metadata['isbn'] = doc_meta.identifiers['isbn']
                if doc_meta.tags:
                    metadata['tags'] = doc_meta.tags
                if doc_meta.comments:
                    metadata['comments'] = doc_meta.comments
                if doc_meta.comments_html:
                    metadata['comments_html'] = doc_meta.comments_html
                if doc_meta.custom_fields:
                    metadata['custom_fields'] = doc_meta.custom_fields
                # Adapter-agnostic ID  →  both source_id and calibre_id
                metadata['source_id'] = doc_meta.doc_id
                if doc_meta.doc_id.isdigit():
                    metadata['calibre_id'] = int(doc_meta.doc_id)
                return metadata
            except Exception:
                pass
            return {}

        # ── Legacy path: direct CalibreDB access ───────────────
        metadata = {}
        try:
            library_path = CalibreDB.find_library_path(file_path)
            if not library_path:
                return metadata

            with CalibreDB(library_path) as calibre:
                book_data = calibre.get_book_by_path(file_path)

                if book_data:
                    for field in self._rag._CALIBRE_FIELDS:
                        if book_data.get(field):
                            metadata[field] = book_data[field]
                    # Backfill source_id from calibre_id for consistency
                    if book_data.get('calibre_id'):
                        metadata['source_id'] = str(book_data['calibre_id'])
                    if book_data.get('comments_html'):
                        metadata['comments_html'] = book_data['comments_html']
        except Exception:
            pass

        return metadata

    def _index_book_modular_pipeline(self, book_path: Path, book_id: str, book_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Index a book using the ModularPipeline architecture.

        Uses the new parser → chunker → embedder pipeline instead of
        UniversalExtractor + manual embedding. Activated by --use-modular-pipeline flag.

        Args:
            book_path: Path to book file
            book_id: Book identifier
            book_metadata: Metadata from _extract_metadata()

        Returns:
            Dictionary with indexing statistics
        """
        from src.archilles.pipeline import ModularPipeline

        profile_name = self._rag.profile_name or 'minimal'
        print(f"  Using ModularPipeline (profile: {profile_name})")

        try:
            # Reuse one pipeline for the whole run — building it per book
            # would reload the embedding model each time.
            pipeline = self._modular_pipeline
            if pipeline is None:
                pipeline = ModularPipeline.from_profile(profile_name)
                self._modular_pipeline = pipeline
            processed = pipeline.process(book_path)

            print(f"  Parsed: {processed.page_count or 'N/A'} pages in {processed.parse_time:.1f}s")
            print(f"  Chunked: {processed.chunk_count} chunks in {processed.chunk_time:.1f}s")
            print(f"  Embedded: {processed.chunk_count} vectors in {processed.embed_time:.1f}s")

            # Store via the LanceDB adapter
            calibre_id = book_metadata.get('calibre_id', 0)
            source_id = book_metadata.get('source_id') or (str(calibre_id) if calibre_id else None)
            chunks_added = self._rag.store.add_processed_documents(
                processed,
                book_metadata={
                    "book_id": book_id,
                    "author": book_metadata.get('author', ''),
                    "publisher": book_metadata.get('publisher', ''),
                    "year": book_metadata.get('year', 0),
                    "tags": book_metadata.get('tags', ''),
                    "language": book_metadata.get('language', ''),
                },
                calibre_id=calibre_id if calibre_id else None,
                source_id=source_id,
            )

            print(f"\n  Indexed {chunks_added} chunks in {processed.total_time:.1f}s total")

            return {
                'book_id': book_id,
                'status': 'indexed',
                'chunks_indexed': chunks_added,
                'pipeline': 'modular',
                'profile': profile_name,
                'parse_time': processed.parse_time,
                'chunk_time': processed.chunk_time,
                'embed_time': processed.embed_time,
                'total_time': processed.total_time,
            }

        except Exception as e:
            print(f"\n  ModularPipeline failed: {e}")
            print(f"  Falling back to legacy extractor...")
            self._rag.use_modular_pipeline = False  # Disable for this session
            return self.index_book(str(book_path), book_id, force=True)

    def _index_book_phase1(self, book_path: Path, book_id: str, book_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Phase 1 indexing: Metadata + comments + annotations (fast, no fulltext).

        Creates a bibliographic stub chunk (title/author/publisher/…), plus
        structure-aware calibre_comment chunks from the same
        _build_comment_chunks() that phase 2 uses, plus annotation chunks
        (carrying annotation_hash) when the book has highlights/notes — the
        watchdog diffs against that hash.

        The stub and the comment chunks both carry metadata_hash, so phase1
        books take part in the watchdog's metadata diff: a comment written
        after the stub was created is picked up on the next scan instead of
        waiting for phase 2.

        Args:
            book_path: Path to book file
            book_id: Book identifier
            book_metadata: Extracted metadata dictionary

        Returns:
            Dictionary with indexing statistics
        """
        print("  [Phase 1: Metadata + Comments only]")
        start_time = time.time()

        # Build searchable metadata text
        metadata_parts = []

        # Title
        title = book_metadata.get('title', book_path.stem)
        metadata_parts.append(f"Title: {title}")

        # Author
        if book_metadata.get('author'):
            metadata_parts.append(f"Author: {book_metadata['author']}")

        # Publisher
        if book_metadata.get('publisher'):
            metadata_parts.append(f"Publisher: {book_metadata['publisher']}")

        # ISBN
        if book_metadata.get('isbn'):
            metadata_parts.append(f"ISBN: {book_metadata['isbn']}")

        # Year
        if book_metadata.get('year'):
            metadata_parts.append(f"Year: {book_metadata['year']}")

        # Tags
        if book_metadata.get('tags'):
            metadata_parts.append(f"Tags: {self._rag._format_tags(book_metadata['tags'])}")

        # Subject/Keywords
        if book_metadata.get('subject'):
            metadata_parts.append(f"Subject: {book_metadata['subject']}")
        if book_metadata.get('keywords'):
            metadata_parts.append(f"Keywords: {book_metadata['keywords']}")

        # Description
        if book_metadata.get('description'):
            metadata_parts.append(f"Description: {book_metadata['description']}")

        # Calibre comments are deliberately NOT inlined here — they go into
        # dedicated calibre_comment chunks below, exactly as in phase 2. Inlining
        # flattened a whole comment (up to 20k+ words) into one oversized vector,
        # and PHASE1_METADATA misses the comment score boost in search.py.

        # Combine into single searchable text
        searchable_text = "\n".join(metadata_parts)

        # Generate embedding
        print("  [1/2] Generating metadata embedding...")
        embedding = self._rag.embedding_model.encode(
            searchable_text,
            show_progress_bar=False,
            convert_to_numpy=True
        ).tolist()

        # Prepare metadata for LanceDB
        chunk_metadata = {
            'book_id': book_id,
            'book_title': title,
            'chunk_index': 0,  # Single chunk for metadata
            'chunk_type': ChunkType.PHASE1_METADATA,
            'format': book_path.suffix.lower().replace('.', ''),
            'indexed_at': datetime.now().isoformat(),
            'phase': 'phase1'
        }

        # Add all book metadata fields
        self._apply_book_metadata_to_chunk(chunk_metadata, book_metadata)
        if book_metadata.get('isbn'):
            chunk_metadata['isbn'] = book_metadata['isbn']
            if book_metadata.get('isbn_source'):
                chunk_metadata['isbn_source'] = book_metadata['isbn_source']
        if book_metadata.get('custom_fields'):
            chunk_metadata['custom_fields'] = json.dumps(book_metadata['custom_fields'])

        # metadata_hash puts phase1 books into the watchdog's metadata diff.
        # Without it get_hashes_for_indexed_books() reports '' for the book,
        # the scanner's `meta_changed = bool(stored_meta_hash) and ...` is
        # always False, and the stub stays frozen at the state it had when it
        # was first written — every later comment edit is lost until phase 2.
        meta_hash = self._resolve_metadata_hash(book_id, book_metadata)
        chunk_metadata['metadata_hash'] = meta_hash

        chunk_metadata['source_file'] = str(book_path)

        # Index in LanceDB
        print("  [2/2] Indexing metadata chunk...")
        chunk_data = {
            'id': f"{book_id}_metadata",
            'text': searchable_text,
            **chunk_metadata
        }
        chunks = [chunk_data]
        embedding_arrays = [np.array([embedding])]

        # Calibre comments as structure-aware chunks — same treatment as phase 2:
        # H2–H4 sections, bold/key passages hoisted as "Key points:", and a
        # 400-word split at sentence boundaries for long sections.
        comment_count = 0
        if book_metadata.get('comments') or book_metadata.get('comments_html'):
            comment_chunks, comment_embeddings = self._build_comment_chunks(
                book_metadata=book_metadata,
                book_id=book_id,
                book_format=chunk_metadata['format'],
                metadata_hash=meta_hash,
            )
            if comment_chunks:
                chunks.extend(comment_chunks)
                embedding_arrays.append(np.array(comment_embeddings))
                comment_count = len(comment_chunks)

        # Persist annotations + annotation_hash on stubs too: the watchdog
        # compares the stored hash against the freshly computed one, so a stub
        # without it is re-flagged as annotations_changed on every scan and
        # delta-rewritten forever. metadata_hash mirrors the stub's own hash so
        # every chunk of the book reports the same value.
        annot_count = 0
        try:
            annot_result = get_combined_annotations(
                book_path=str(book_path),
                include_pdf=True,
                exclude_toc_markers=True,
                min_length=20
            )
            annotations = annot_result.get('annotations', [])
            if annotations:
                annot_hash = self._compute_annotation_hash(annotations)
                annot_chunks, annot_texts = self._build_annotation_chunks(
                    annotations,
                    book_id=book_id,
                    book_title=title,
                    annotation_hash=annot_hash,
                    book_format=chunk_metadata['format'],
                    metadata_hash=meta_hash,
                    book_metadata=book_metadata,
                    indexed_at=chunk_metadata['indexed_at'],
                )
                if annot_texts:
                    annot_emb = self._rag.embedding_model.encode(
                        annot_texts, show_progress_bar=False, convert_to_numpy=True
                    )
                    chunks.extend(annot_chunks)
                    embedding_arrays.append(annot_emb)
                    annot_count = len(annot_chunks)
        except Exception as e:
            print(f"  ⚠ Annotation extraction failed (non-fatal): {e}")

        embeddings_array = np.concatenate(embedding_arrays)
        self._rag.store.add_chunks(chunks, embeddings_array)
        if comment_count:
            print(f"    Added {comment_count} comment chunk(s)")
        if annot_count:
            print(f"    Added {annot_count} annotation chunk(s)")

        index_time = time.time() - start_time

        print(f"  Phase 1 complete ({index_time:.1f}s)")
        print(f"     Collection size: {self._rag.store.count()} chunks\n")

        return {
            'book_id': book_id,
            'chunks_indexed': len(chunks),
            'total_words': len(searchable_text.split()),
            'total_pages': None,
            'extraction_time': 0,
            'embedding_time': index_time,
            'indexing_time': index_time,
            'total_time': index_time,
            'phase': 'phase1'
        }

    def index_book(self, book_path: str, book_id: str = None, force: bool = False, phase: str = 'phase2') -> Dict[str, Any]:
        """
        Extract and index a book.

        Args:
            book_path: Path to book file
            book_id: Optional book ID (default: filename)
            force: If True, delete existing chunks before re-indexing
            phase: 'phase1' (metadata + comments only, fast) or 'phase2' (full content, slow)

        Returns:
            Dictionary with indexing statistics
        """
        book_path = Path(book_path)

        if not book_path.exists():
            raise FileNotFoundError(f"Book not found: {book_path}")

        book_id = book_id or book_path.stem

        print(f"  File: {book_path.name}")

        # Check for existing CONTENT chunks (not just metadata)
        # Books with only phase1_metadata chunks still need full content indexing.
        # Finding 8.7: targeted, windowless state query — the previous
        # get_by_book_id(limit=100) window missed the annotation/metadata
        # hashes for books with many chunks and re-embedded them every scan.
        state = self._rag.store.get_book_state(book_id)
        if state['has_content']:
            if force:
                print(f"  Deleting existing chunks for {book_id}...", flush=True)
                deleted = self._rag.store.delete_by_book_id(book_id)
                print(f"    Deleted {deleted} chunks")
            else:
                # Check if metadata or annotations have changed (smart update without full re-index)
                book_metadata = self._extract_metadata(book_path)
                current_meta_hash = self._resolve_metadata_hash(book_id, book_metadata)

                # Check annotation changes (adapter-aware, finding 1.4)
                try:
                    current_annotations = self._resolve_annotations(book_id, book_path)
                    current_annot_hash = self._compute_annotation_hash(current_annotations)
                except Exception:
                    current_annotations = []
                    current_annot_hash = ''

                meta_changed = current_meta_hash != state['metadata_hash']
                annot_changed = current_annot_hash != state['annotation_hash']

                if meta_changed or annot_changed:
                    return self._update_metadata_only(
                        book_id, book_metadata, current_meta_hash, state,
                        annotations=current_annotations if annot_changed else None,
                        annotation_hash=current_annot_hash if annot_changed else None,
                        book_path=book_path,
                    )

                print(f"  Book already indexed ({state['content_count']} content chunks). Use --force to reindex.")
                return {
                    'book_id': book_id,
                    'status': 'already_indexed',
                    'chunks_indexed': state['content_count'],
                    'existing_chunks': state['total']
                }
        elif state['total'] and not force:
            # Has metadata-only chunks — delete them before full indexing
            print(f"  Replacing {state['total']} metadata-only chunks with full content...")
            self._rag.store.delete_by_book_id(book_id)

        # Extract metadata (author, title, year, ISBN, publisher, etc.)
        # Works for PDF, EPUB, and other formats
        book_metadata = self._extract_metadata(book_path)

        # MODULAR PIPELINE PATH: Use new architecture if flag is set
        if self._rag.use_modular_pipeline:
            return self._index_book_modular_pipeline(book_path, book_id, book_metadata)

        # PHASE 1: Metadata + Comments only (fast indexing)
        if phase == 'phase1':
            return self._index_book_phase1(book_path, book_id, book_metadata)

        # PHASE 2: Full content indexing (default)
        # Step 1: Extract text
        start_time = time.time()
        extracted = self._rag.extractor.extract(book_path)
        extract_time = time.time() - start_time

        # Detect scanned/mostly-scanned PDFs
        needs_ocr = self._detect_needs_ocr(extracted)

        if self._rag.hierarchical and extracted.chunks:
            self._apply_hierarchical_chunking(extracted, book_id)
            parent_count = sum(1 for c in extracted.chunks if c.get('metadata', {}).get('chunk_type') == ChunkType.PARENT)
            child_count = sum(1 for c in extracted.chunks if c.get('metadata', {}).get('chunk_type') == ChunkType.CHILD)
            print(f"  Extract: {parent_count}p+{child_count}c chunks, {extracted.metadata.total_words:,}w, {extracted.metadata.total_pages or '?'}p ({extract_time:.1f}s)")
        else:
            print(f"  Extract: {len(extracted.chunks)} chunks, {extracted.metadata.total_words:,}w, {extracted.metadata.total_pages or '?'}p ({extract_time:.1f}s)")

        # Step 2: Generate embeddings
        start_time = time.time()

        texts = [chunk['text'] for chunk in extracted.chunks]
        embedding_batches = []

        # Batch process for speed (batch_size determined by profile)
        for i in tqdm(range(0, len(texts), self._rag.batch_size), desc="    Embedding"):
            batch = texts[i:i+self._rag.batch_size]
            batch_embeddings = self._rag.embedding_model.encode(
                batch,
                show_progress_bar=False,
                convert_to_numpy=True
            )
            embedding_batches.append(batch_embeddings)

        embeddings_array = np.concatenate(embedding_batches) if embedding_batches else np.array([])

        embed_time = time.time() - start_time
        print(f"  Embed:   {len(embeddings_array)} vectors ({embed_time:.1f}s)")

        # Step 3: Index in LanceDB
        start_time = time.time()

        # Prepare chunks with metadata
        indexed_at = datetime.now().isoformat()
        meta_hash = self._resolve_metadata_hash(book_id, book_metadata)
        chunks = self._build_chunk_dicts(extracted, book_id, book_metadata, indexed_at, meta_hash)

        # Collect extra embedding arrays for comments/annotations
        extra_embedding_arrays = []

        # Add Calibre comments as structured chunk(s) (if available)
        has_comment = bool(book_metadata and (book_metadata.get('comments') or book_metadata.get('comments_html')))
        if has_comment:
            comment_chunks, comment_embeddings = self._build_comment_chunks(
                book_metadata=book_metadata,
                book_id=book_id,
                book_format=extracted.metadata.detected_format,
                metadata_hash=meta_hash,
            )
            chunks.extend(comment_chunks)
            if comment_embeddings:
                extra_embedding_arrays.append(np.array(comment_embeddings))

        # Add user annotations. Calibre: viewer sidecars + PDF-embedded.
        # Other sources: through the adapter, which is where their reader keeps
        # them — Zotero's highlights live in zotero.sqlite (finding 1.4).
        annot_count = 0
        try:
            annotations = self._resolve_annotations(book_id, book_path)
            if annotations:
                annot_hash = self._compute_annotation_hash(annotations)

                annot_chunks_pending, annot_texts = self._build_annotation_chunks(
                    annotations,
                    book_id=book_id,
                    book_title=book_metadata.get('title', book_path.stem) if book_metadata else book_path.stem,
                    annotation_hash=annot_hash,
                    book_format=extracted.metadata.detected_format,
                    metadata_hash=meta_hash,
                    book_metadata=book_metadata,
                    indexed_at=indexed_at,
                )

                # Batch-encode all annotation texts at once
                if annot_texts:
                    annot_emb = self._rag.embedding_model.encode(
                        annot_texts, show_progress_bar=False, convert_to_numpy=True
                    )
                    chunks.extend(annot_chunks_pending)
                    extra_embedding_arrays.append(annot_emb)

                annot_count = len(annot_texts)

        except Exception as e:
            print(f"  ⚠ Annotation extraction failed (non-fatal): {e}")
            annot_count = 0

        # Concatenate all embedding arrays (content + comments + annotations)
        all_arrays = [embeddings_array] + extra_embedding_arrays if len(embeddings_array) else extra_embedding_arrays
        embeddings_array = np.concatenate(all_arrays) if all_arrays else np.array([])

        # Add to LanceDB
        num_indexed = self._rag.store.add_chunks(chunks, embeddings_array)

        index_time = time.time() - start_time
        extras = []
        if has_comment:
            extras.append("comment")
        if annot_count:
            extras.append(f"{annot_count} annot")
        extras_str = f" + {', '.join(extras)}" if extras else ""
        total_time = extract_time + embed_time + index_time
        print(f"  Index:   {num_indexed} chunks{extras_str} ({index_time:.1f}s) | total {total_time:.1f}s")

        return {
            'book_id': book_id,
            'chunks_indexed': num_indexed,
            'total_words': extracted.metadata.total_words,
            'total_pages': extracted.metadata.total_pages,
            'extraction_time': extract_time,
            'embedding_time': embed_time,
            'indexing_time': index_time,
            'total_time': total_time,
            'needs_ocr': needs_ocr,
        }

    @contextmanager
    def _override_extractor_chunking(self, chunk_size: int, overlap: int):
        """Temporarily swap chunk_size/overlap on every sub-extractor of
        UniversalExtractor (pdf/epub/txt/html), restoring on exit.

        UniversalExtractor instantiates one BaseExtractor per format, each
        with its own chunk_size — `BaseExtractor._temporary_chunk_params`
        only handles a single instance, so we iterate them all here.
        """
        sub_attrs = ("pdf_extractor", "epub_extractor", "txt_extractor", "html_extractor")
        saved = []
        for attr in sub_attrs:
            sub = getattr(self._rag.extractor, attr, None)
            if sub is None:
                continue
            saved.append((sub, sub.chunk_size, sub.overlap))
            sub.chunk_size = chunk_size
            sub.overlap = overlap
        try:
            yield
        finally:
            for sub, saved_size, saved_overlap in saved:
                sub.chunk_size = saved_size
                sub.overlap = saved_overlap

    def prepare_book(self, book_path: str, book_id: str = None,
                     output_dir: str = "./prepared_chunks") -> Dict[str, Any]:
        """
        Extract and chunk a book WITHOUT embedding (Phase 1 of two-phase indexing).
        Writes one JSONL file per book to output_dir.

        Args:
            book_path: Path to book file
            book_id: Optional book ID (default: filename)
            output_dir: Directory for JSONL output files

        Returns:
            Dictionary with preparation statistics
        """
        book_path = Path(book_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not book_path.exists():
            raise FileNotFoundError(f"Book not found: {book_path}")

        book_id = book_id or book_path.stem

        print(f"  File: {book_path.name}")

        # Check skip-if-exists BEFORE metadata extraction (cheap skip, no
        # SQLite/adapter lookup). The file is named after book_id — see
        # prepared_jsonl_name for why calibre_id-based names collided (5.1).
        out_file = output_dir / prepared_jsonl_name(book_id)
        if out_file.exists():
            header = read_prepared_header(out_file)
            if header is not None:
                print(f"  Already prepared ({header.get('chunk_count', '?')} chunks). Skipping.")
                return {
                    'book_id': book_id,
                    'status': 'already_prepared',
                    'chunk_count': header.get('chunk_count', 0),
                }
            # Invalid header or a chunk-count mismatch (interrupted prepare) —
            # fall through and re-prepare; the atomic write below replaces it.

        # Extract metadata
        book_metadata = self._extract_metadata(book_path)
        calibre_id = book_metadata.get('calibre_id', 0) or 0

        # Step 1: Extract text — apply Phase-1 chunk settings temporarily
        # (groesser als der Live-Default, damit Phase-1-JSONL kompakt bleibt
        # fuer spaetere Cloud-GPU-Embeddings; Live-Index bleibt unangetastet).
        start_time = time.time()
        with self._override_extractor_chunking(
            self._rag._prepare_chunk_size, self._rag._prepare_overlap
        ):
            extracted = self._rag.extractor.extract(book_path)
        extract_time = time.time() - start_time

        # Handle hierarchical chunking if requested
        if self._rag.hierarchical and extracted.chunks:
            self._apply_hierarchical_chunking(extracted, book_id)

        print(f"  Extract: {len(extracted.chunks)} chunks, {extracted.metadata.total_words:,}w, {extracted.metadata.total_pages or '?'}p ({extract_time:.1f}s)")

        # Detect scanned/mostly-scanned PDFs
        needs_ocr = self._detect_needs_ocr(extracted)

        # Step 2: Build chunk dicts (shared with index_book)
        indexed_at = datetime.now().isoformat()
        meta_hash = self._resolve_metadata_hash(book_id, book_metadata)
        chunks = self._build_chunk_dicts(extracted, book_id, book_metadata, indexed_at, meta_hash)

        # Step 2b: Add Calibre comments as structured chunk(s) (if available)
        has_comment = bool(book_metadata and (book_metadata.get('comments') or book_metadata.get('comments_html')))
        if has_comment:
            comment_chunks, _ = self._build_comment_chunks(
                book_metadata=book_metadata,
                book_id=book_id,
                book_format=extracted.metadata.detected_format,
                metadata_hash=meta_hash,
                embed=False,
            )
            chunks.extend(comment_chunks)

        # Step 3: Write JSONL
        header = {
            '_header': True,
            'calibre_id': calibre_id,
            'book_id': book_id,
            'book_metadata': {k: v for k, v in book_metadata.items()
                             if k not in ('custom_fields',)},
            'chunk_count': len(chunks),
            'prepared_at': datetime.now().isoformat(),
        }

        # Write to a temp name and rename atomically onto the final name: a
        # process kill mid-write leaves only the (inert, *.jsonl.tmp) temp
        # file — the final name never observes a partial file, and *.jsonl
        # globs elsewhere (embed_prepared, batch_prepare's discovery) never
        # match it.
        tmp_file = out_file.with_suffix('.jsonl.tmp')
        with open(tmp_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps(header, ensure_ascii=False) + '\n')
            for chunk in chunks:
                f.write(json.dumps(chunk, ensure_ascii=False) + '\n')
        os.replace(tmp_file, out_file)

        print(f"  Prepared: {len(chunks)} chunks -> {out_file.name} ({out_file.stat().st_size / 1024:.0f} KB)")

        return {
            'book_id': book_id,
            'status': 'prepared',
            'chunk_count': len(chunks),
            'extraction_time': extract_time,
            'output_file': str(out_file),
            'needs_ocr': needs_ocr,
        }

    def embed_prepared(self, input_dir: str, mode: str = 'local',
                       host: str = None, port: int = 8000, token: str = None,
                       batch_size: int = 100, use_gzip: bool = True,
                       profile: str = None, force: bool = False) -> Dict[str, Any]:
        """
        Embed pre-prepared chunks and store in LanceDB (Phase 2 of two-phase indexing).

        Args:
            input_dir: Directory with JSONL files from prepare_book()
            mode: 'local' (use local GPU/CPU) or 'remote' (use remote server)
            host: Remote server host (required if mode='remote')
            port: Remote server port
            token: Bearer token for remote server
            batch_size: Texts per HTTP request (remote) or encode batch (local)
            use_gzip: Use gzip compression for remote requests
            profile: Hardware profile for local mode
            force: Delete existing chunks and re-embed (for re-indexing with improved chunks)

        Returns:
            Summary statistics
        """
        input_dir = Path(input_dir)
        if not input_dir.exists():
            raise FileNotFoundError(f"Input directory not found: {input_dir}")

        # Set up embedder
        if mode == 'remote':
            if not host:
                raise ValueError(
                    "Remote mode requires a host "
                    "(pass --host or set embedder.host in .archilles/config.json)"
                )
            from src.archilles.embedders.remote import RemoteBGEEmbedder
            embedder = RemoteBGEEmbedder(
                host=host, port=port, token=token,
                batch_size=batch_size, use_gzip=use_gzip,
            )
            embedder.load_model()  # checks health
            print(f"  Remote embedder: {host}:{port}")
        else:
            # Use local SentenceTransformer (already loaded in self._rag.embedding_model)
            if self._rag.embedding_model is None:
                raise RuntimeError("No embedding model loaded. Don't use --skip-model with local embed mode.")
            print(f"  Local embedder: {self._rag.device}, batch_size={self._rag.batch_size}")

        # Find all JSONL files
        jsonl_files = sorted(input_dir.glob('*.jsonl'))
        if not jsonl_files:
            print("  No JSONL files found.")
            return {'total_books': 0, 'total_chunks': 0, 'skipped': 0, 'failed': 0}

        # Hardware-Tiers-V2 §12: books indexed provisionally light (full-external)
        # are marked pending_external. The external embed always replaces them, so
        # treat them as force even without --force; the freshly added hierarchical
        # chunks carry no marker, which clears it implicitly.
        pending_external_ids = self._rag.store.get_pending_external_book_ids()

        # Load resume checkpoint (completed + skipped books)
        cp_path = input_dir / '.embed_checkpoint.json'
        cp = IndexingCheckpoint.load_or_create(cp_path, profile="", book_ids=[])
        embedded_set = set(cp.completed_books) | set(cp.skipped_books)

        total_books = 0
        total_chunks = 0
        skipped = 0
        failed = 0

        for jsonl_file in jsonl_files:
            # Read and parse the WHOLE file before touching LanceDB (2.1): the
            # old flow deleted a book's chunks first and read/embedded after,
            # so a truncated file or a mid-run embedder failure lost the book.
            # Unreadable files are a per-book failure — skip, keep going.
            try:
                with open(jsonl_file, 'r', encoding='utf-8') as f:
                    header = json.loads(f.readline())
                    if not header.get('_header'):
                        print(f"  Skipping {jsonl_file.name}: no header")
                        continue
                    chunk_lines = f.readlines()
                chunks = [json.loads(line) for line in chunk_lines if line.strip()]
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  ⚠️  {jsonl_file.name}: unreadable ({exc}) — skipped, "
                      f"nothing deleted. Re-prepare this book.")
                failed += 1
                continue

            # Checkpoint key per book_id (unique across adapters). The old
            # calibre_id key was '0' for every non-Calibre book, so all but
            # the first file were skipped as already embedded (5.1). Old
            # checkpoints keep matching: for Calibre, book_id == str(calibre_id).
            file_key = str(header.get('book_id')
                           or header.get('calibre_id')
                           or jsonl_file.stem)

            # Skip if already embedded
            if file_key in embedded_set:
                skipped += 1
                continue

            book_id = header['book_id']

            # Validate against the header's chunk_count: a mismatch means the
            # prepare was interrupted mid-write — never let a truncated file
            # replace existing chunks (2.1).
            expected = header.get('chunk_count')
            if expected is not None and len(chunks) != expected:
                print(f"  ⚠️  {book_id}: prepare file truncated "
                      f"({len(chunks)}/{expected} chunks) — skipped, "
                      f"nothing deleted. Re-prepare this book.")
                failed += 1
                continue
            if not chunks:
                # Legitimately empty prepare (e.g. scanned PDF without OCR).
                print(f"  {book_id}: prepare file has 0 chunks — skipping (needs OCR?).")
                cp.skip_book(file_key)
                embedded_set.add(file_key)
                skipped += 1
                continue

            # Check LanceDB for existing chunks (2.4): read full book state
            # instead of sampling one arbitrary row — get_by_book_id(limit=1)
            # could return a metadata/annotation row and misjudge whether real
            # content exists. Provisional-light books are always replaced.
            book_force = force or book_id in pending_external_ids
            state = self._rag.store.get_book_state(book_id)
            if state['has_content'] and not book_force:
                print(f"  {book_id}: already in LanceDB ({state['content_count']} content chunks). Skipping.")
                cp.skip_book(file_key)
                embedded_set.add(file_key)  # keep in-memory skip-set in sync for this run
                skipped += 1
                continue

            print(f"  {book_id}: {len(chunks)} chunks...", end=' ', flush=True)

            # Embed. Failures here (e.g. remote server down) abort the run —
            # deliberately BEFORE any delete, so the existing chunks stay
            # intact and the checkpoint resumes the run later (2.1).
            texts = [c['text'] for c in chunks]
            start = time.time()

            if mode == 'remote':
                result = embedder.embed_batch(texts)
                embeddings_array = result.embeddings
            else:
                # Local: batch through SentenceTransformer
                all_emb = []
                for i in range(0, len(texts), self._rag.batch_size):
                    batch = texts[i:i + self._rag.batch_size]
                    batch_emb = self._rag.embedding_model.encode(
                        batch, show_progress_bar=False, convert_to_numpy=True
                    )
                    all_emb.append(batch_emb)
                embeddings_array = np.concatenate(all_emb, axis=0)

            embed_time = time.time() - start

            # Replace only now that the embeddings exist (2.1). Delete every
            # existing chunk EXCEPT annotations (2.2): the prepared file never
            # carries user highlights, so a blanket delete would drop them.
            # Guarded on total>0 (2.4) so a stub-only book (e.g. a leftover
            # phase1_metadata row) has its stub cleaned up when the real
            # content arrives, not just force/pending replacements.
            replaced = ''
            if state['total'] > 0:
                deleted = self._rag.store.delete_by_book_id_except_annotations(book_id)
                replaced = (f" (replaced {deleted} provisional flat chunks)"
                            if book_id in pending_external_ids
                            else f" (deleted {deleted} old chunks)")

            # Store in LanceDB
            num_added = self._rag.store.add_chunks(chunks, embeddings_array)

            # Preserving annotations means the replace can no longer rely on
            # "delete everything → marker gone" to de-flag a pending_external
            # book: mark_pending_external stamps the marker on every chunk, so a
            # surviving annotation would keep the book pending forever. Clear it
            # explicitly now that fresh (external/local) content is in place.
            if book_id in pending_external_ids:
                self._rag.store.clear_pending_external(book_id)

            print(f"{num_added} indexed ({embed_time:.1f}s){replaced}")

            total_books += 1
            total_chunks += num_added

            # Update progress
            cp.complete_book(file_key)
            embedded_set.add(file_key)  # keep in-memory skip-set in sync for this run

        print(f"\n  Done: {total_books} books, {total_chunks} chunks embedded. "
              f"{skipped} skipped." + (f" {failed} FAILED (see warnings above)." if failed else ""))

        # Refresh the FTS index so keyword/hybrid search sees the freshly
        # embedded chunks immediately, matching batch_index's post-index
        # refresh. Never let an index-creation failure fail the run itself.
        if total_books > 0:
            try:
                self._rag.store.create_fts_index()
            except Exception as exc:
                print(f"  ⚠️  FTS index refresh failed: {exc}")
                print("     Keyword search may be stale. Run: python scripts/rag_demo.py create-index")
            # Ensure the ANN index exists — without it semantic/hybrid
            # search brute-force-scans the vector column (minutes instead
            # of seconds at million-chunk scale).
            try:
                self._rag.store.ensure_vector_index()
            except Exception as exc:
                print(f"  ⚠️  Vector index check failed: {exc}")
                print("     Semantic search may be slow. Run: python scripts/rag_demo.py create-index")

        cp.delete()
        return {
            'total_books': total_books,
            'total_chunks': total_chunks,
            'skipped': skipped,
            'failed': failed,
        }

    def _build_comment_chunks(
        self,
        book_metadata: Dict[str, Any],
        book_id: str,
        book_format: str,
        metadata_hash: str,
        embed: bool = True,
    ) -> tuple:
        """
        Build calibre_comment chunk(s) with structure-aware text.

        If comments_html is available, parses H2–H4 headlines into separate
        chunks and prepends bold/<strong>/!!!...!!! passages as "Key points:"
        so they carry extra weight in the embedding.

        Returns:
            (chunks, embeddings) — parallel lists ready for store.add_chunks()
        """
        comments_html = book_metadata.get('comments_html', '')
        if comments_html:
            sections = CalibreDB.parse_html_comment(comments_html)
        else:
            plain = book_metadata.get('comments', '')
            sections = [{'headline': None, 'headline_level': None,
                         'text': plain, 'key_passages': []}] if plain else []

        if not sections:
            return [], []

        # Split sections that are too long for a useful single embedding.
        # BGE-M3 retrieval quality degrades significantly beyond ~500 words.
        # Long headline-less sections (e.g. a 3500-word comment block) are
        # split at word boundaries into sub-chunks of MAX_COMMENT_WORDS each.
        MAX_COMMENT_WORDS = 400

        def split_section(section: dict) -> list:
            words = section['text'].split()
            if len(words) <= MAX_COMMENT_WORDS:
                return [section]
            # Split at sentence boundaries (. ! ?) — never mid-sentence
            sentences = re.split(r'(?<=[.!?])\s+', section['text'])
            sub_sections = []
            current_words = 0
            current_sents: list[str] = []
            first = True
            for sent in sentences:
                sent_words = len(sent.split())
                if current_sents and current_words + sent_words > MAX_COMMENT_WORDS:
                    sub_sections.append({
                        'headline': section['headline'],
                        'headline_level': section['headline_level'],
                        'text': ' '.join(current_sents),
                        'key_passages': section['key_passages'] if first else [],
                    })
                    first = False
                    current_sents = [sent]
                    current_words = sent_words
                else:
                    current_sents.append(sent)
                    current_words += sent_words
            if current_sents:
                sub_sections.append({
                    'headline': section['headline'],
                    'headline_level': section['headline_level'],
                    'text': ' '.join(current_sents),
                    'key_passages': section['key_passages'] if first else [],
                })
            return sub_sections

        flat_sections = []
        for section in sections:
            flat_sections.extend(split_section(section))

        chunks, texts = [], []
        title = book_metadata.get('title', book_id)

        for i, section in enumerate(flat_sections):
            parts = []
            if section['headline']:
                parts.append(f"## {section['headline']} ##")
            if section['key_passages']:
                kp = ' | '.join(section['key_passages'])
                parts.append(f"Key points: {kp}")
            if section['text']:
                parts.append(section['text'])

            chunk_text = f"[CALIBRE_COMMENT] {' '.join(parts)}"

            chunk = {
                'id': f"{book_id}_comment_{i}",
                'text': chunk_text,
                'book_id': book_id,
                'book_title': title,
                'chunk_index': -(i + 1),
                'chunk_type': ChunkType.CALIBRE_COMMENT,
                'format': book_format,
                'indexed_at': datetime.now().isoformat(),
                'metadata_hash': metadata_hash,
            }
            if section['headline']:
                chunk['section_title'] = section['headline']
            self._apply_book_metadata_to_chunk(chunk, book_metadata)

            chunks.append(chunk)
            texts.append(chunk_text)

        # Batch-encode all comment texts at once
        if embed and texts:
            encoded = self._rag.embedding_model.encode(
                texts, show_progress_bar=False, convert_to_numpy=True
            )
            embeddings = [e.tolist() for e in encoded]
        else:
            embeddings = [[] for _ in chunks]

        return chunks, embeddings

    def _update_metadata_only(self, book_id: str, book_metadata: Dict[str, Any],
                               new_hash: str, state: Dict[str, Any],
                               annotations: Optional[List[Dict[str, Any]]] = None,
                               annotation_hash: Optional[str] = None,
                               book_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Smart metadata/annotation update: refresh only changed parts
        WITHOUT re-extracting or re-embedding the book text.

        This is ~50-100x faster than a full re-index (~1-2s vs ~90s).

        Args:
            state: Book state from store.get_book_state() (finding 8.7 —
                replaces the old row-window view of existing chunks).
        """
        # Refuse destructive empty updates (finding 4.1c): empty book_metadata
        # against a non-empty stored hash means extraction returned nothing
        # (e.g. an adapterless Zotero delta, a missing/renamed file). Writing
        # here would set metadata_hash='' — disabling all future change
        # detection for this book (bool('') is False) — and delete the comment/
        # abstract chunk with nothing to re-add. Skip and re-detect next scan.
        if not book_metadata and state.get('metadata_hash'):
            print(f"  ⚠️  {book_id}: metadata extraction returned empty but a stored "
                  f"hash exists — skipping update to avoid wiping the hash / "
                  f"deleting the comment chunk.")
            return {'book_id': book_id, 'status': 'metadata_extract_failed',
                    'chunks_indexed': 0}

        start_time = time.time()
        meta_changed = new_hash != state.get('metadata_hash', '')
        annot_changed = annotations is not None

        if meta_changed and annot_changed:
            print(f"  📝 Metadata + annotations changed — updating without full re-index...")
        elif meta_changed:
            print(f"  📝 Metadata changed — updating without full re-index...")
        elif annot_changed:
            print(f"  📝 Annotations changed — updating without full re-index...")

        # 1. Update metadata fields in all content chunks (no embedding change)
        updated_fields = {}
        if book_metadata.get('author'):
            updated_fields['author'] = book_metadata['author']
        if book_metadata.get('title'):
            updated_fields['book_title'] = book_metadata['title']
        if book_metadata.get('tags'):
            updated_fields['tags'] = self._rag._format_tags(book_metadata['tags'])
        if book_metadata.get('publisher'):
            updated_fields['publisher'] = book_metadata['publisher']
        updated_fields['metadata_hash'] = new_hash

        if updated_fields:
            num_updated = self._rag.store.update_metadata_fields(book_id, updated_fields)
            print(f"    Updated metadata in {num_updated} chunks")

        # 2. Replace calibre_comment chunk if metadata changed
        comment_added = False
        if meta_changed:
            # Finding 8.7: delete unconditionally — the old guard depended on
            # a row window that could miss the comment chunk, so the re-add
            # below silently accumulated duplicates.
            deleted = self._rag.store.delete_by_book_id_and_type(book_id, ChunkType.CALIBRE_COMMENT)
            if deleted:
                print(f"    Deleted {deleted} old comment chunk(s)")

            if book_metadata.get('comments') or book_metadata.get('comments_html'):
                book_format = state.get('format', '')
                comment_chunks, comment_embeddings = self._build_comment_chunks(
                    book_metadata=book_metadata,
                    book_id=book_id,
                    book_format=book_format,
                    metadata_hash=new_hash,
                )
                if comment_chunks:
                    embeddings_array = np.array(comment_embeddings)
                    self._rag.store.add_chunks(comment_chunks, embeddings_array)
                    comment_added = True
                    print(f"    Added {len(comment_chunks)} comment chunk(s)")

        # 3. Replace annotation chunks if annotations changed
        annot_updated = False
        if annot_changed and annotations is not None:
            # Finding 8.7: delete old annotation chunks unconditionally — the
            # old window-based guard skipped the delete for books with many
            # chunks, adding one duplicate copy per routine run.
            deleted = self._rag.store.delete_by_book_id_and_type(book_id, ChunkType.ANNOTATION)
            if deleted:
                print(f"    Deleted {deleted} old annotation chunk(s)")

            # Add new annotation chunks
            if annotations:
                annot_chunks, annot_texts = self._build_annotation_chunks(
                    annotations,
                    book_id=book_id,
                    book_title=book_metadata.get('title', book_id),
                    annotation_hash=annotation_hash or '',
                    book_format=state.get('format', ''),
                    metadata_hash=new_hash,
                    book_metadata=book_metadata,
                )

                if annot_chunks:
                    # Batch-encode all annotation texts at once
                    embeddings_array = self._rag.embedding_model.encode(
                        annot_texts, show_progress_bar=False, convert_to_numpy=True
                    )
                    self._rag.store.add_chunks(annot_chunks, embeddings_array)
                    print(f"    Added {len(annot_chunks)} new annotation chunks")
                    annot_updated = True
            else:
                print(f"    No annotations found (removed all)")

        elapsed = time.time() - start_time
        content_count = state.get('content_count', 0)
        print(f"  ✅ Updated in {elapsed:.1f}s (content chunks untouched: {content_count})\n")

        return {
            'book_id': book_id,
            'status': 'metadata_updated',
            'chunks_indexed': content_count,
            'metadata_updated': meta_changed,
            'comment_updated': comment_added,
            'annotations_updated': annot_updated,
            'total_time': elapsed,
        }
