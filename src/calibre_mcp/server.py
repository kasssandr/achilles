#!/usr/bin/env python3
"""
Calibre MCP Server

A Model Context Protocol server for Calibre library integration.
Provides tools for searching, analyzing, and accessing Calibre library data.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from src.archilles.constants import ChunkType
from src.archilles.bibliography import BIB_FORMATTERS
from .calibre_analyzer import CalibreAnalyzer

try:
    from src.service import ArchillesService
    SERVICE_AVAILABLE = True
except ImportError:
    SERVICE_AVAILABLE = False
    logging.warning("ArchillesService not available. XML prompt generation disabled.")

from .annotations import (
    compute_book_hash,
    get_annotations_dir,
    get_combined_annotations,
    list_all_annotated_books,
    search_annotations,
)

logger = logging.getLogger(__name__)


class CalibreMCPServer:
    """
    MCP Server for Calibre library integration.

    This server provides tools for:
    - Retrieving book annotations (highlights, notes, bookmarks)
    - Searching through annotations
    - Analyzing library metadata
    """

    def __init__(
        self,
        library_path: Optional[str] = None,
        annotations_dir: Optional[str] = None,
        rag_db_path: Optional[str] = None,
        enable_reranking: bool = False,
        reranker_device: Optional[str] = 'cpu',
        citation_config: Optional[Any] = None,
        adapter=None,
        instance_name: str = "archilles",
    ):
        """
        Initialize the MCP Server.

        Args:
            library_path: Path to the library (Calibre, Zotero, or folder)
            annotations_dir: Custom path to annotations directory
            rag_db_path: Path to RAG database (default: ./archilles_rag_db)
            enable_reranking: Enable cross-encoder reranking for search results
            reranker_device: Device for reranker model ('cpu' to avoid GPU OOM)
            citation_config: CitationConfig instance for bibliography formatting
            adapter: Optional SourceAdapter for metadata access
            instance_name: MCP server instance name (e.g. 'archilles-bib')
        """
        self.library_path = Path(library_path) if library_path else None
        self.annotations_dir = annotations_dir
        self.adapter = adapter
        self.instance_name = instance_name

        self.db_path = self._resolve_db_path(library_path)

        self.citation_config = citation_config

        self._archilles_dir = (Path(library_path) / ".archilles") if library_path else None
        self.rag_db_path = rag_db_path or "./archilles_rag_db"

        self.service = ArchillesService(
            db_path=self.rag_db_path,
            enable_reranking=enable_reranking,
            reranker_device=reranker_device,
            citation_config=citation_config,
            archilles_dir=str(self._archilles_dir) if self._archilles_dir else None,
            adapter=adapter,
        ) if SERVICE_AVAILABLE else None

    @staticmethod
    def _resolve_db_path(library_path: Optional[str]) -> Optional[str]:
        """Resolve the path to Calibre's metadata.db from a library path."""
        if not library_path:
            return None
        lib = Path(library_path)
        db_candidate = lib / "metadata.db"
        if db_candidate.exists():
            return str(db_candidate)
        if lib.name == "metadata.db" and lib.exists():
            return str(lib)
        return None

    def _require_db(self) -> Optional[dict[str, Any]]:
        """Return an error dict if the Calibre metadata database is unavailable, else None.

        Tools backed by CalibreAnalyzer require a metadata.db.  For non-Calibre
        adapters these tools are not available.
        """
        if self.db_path:
            return None
        adapter_hint = ""
        if self.adapter and self.adapter.adapter_type != "calibre":
            adapter_hint = (
                f" (adapter type is '{self.adapter.adapter_type}' — "
                f"this tool requires a Calibre library)"
            )
        return {
            'error': f'Calibre metadata database not available{adapter_hint}',
            'help': 'This tool requires a Calibre library with metadata.db'
        }

    def _ensure_rag_initialized(self) -> bool:
        """Ensure RAG system is initialized (lazy loading via service layer)."""
        if self.service is None:
            logger.warning("ArchillesService not available")
            return False
        return self.service._ensure_initialized()

    def preload(self) -> None:
        """Warm the RAG stack (embedding model, LanceDB, reranker).

        Called from a background thread at server start so the first search
        does not pay the multi-minute lazy model load inside an MCP client's
        tool-call timeout (Claude-Code-based clients abort after 60 s).
        Failures are logged, never raised.
        """
        try:
            ok = self._ensure_rag_initialized()
            if ok:
                self.service._get_reranker()
            logger.info("Preload finished for %r (rag=%s)", self.instance_name, ok)
        except Exception as e:
            logger.warning("Preload failed for %r: %s", self.instance_name, e)

    def get_book_annotations_tool(
        self,
        book_path: str,
        annotation_type: Optional[str] = None,
        exclude_toc_markers: bool = True,
        include_pdf: bool = True,
        min_length: int = 20,
        exclude_first_percent: float = 5.0
    ) -> dict[str, Any]:
        """
        MCP Tool: Get annotations for a specific book with intelligent filtering.

        Args:
            book_path: Full path to the book file
            annotation_type: Optional filter - 'highlight', 'note', or 'bookmark'
            exclude_toc_markers: Whether to exclude TOC markers and technical highlights
            include_pdf: Whether to extract PDF-internal annotations (for PDF files)
            min_length: Minimum character length for annotations
            exclude_first_percent: Exclude annotations in first X% of book

        Returns:
            Dictionary with annotations and metadata
        """
        book_hash = compute_book_hash(book_path)
        annotation_types = [annotation_type] if annotation_type else None

        result = get_combined_annotations(
            book_path=book_path,
            annotations_dir=self.annotations_dir,
            include_pdf=include_pdf,
            exclude_toc_markers=exclude_toc_markers,
            min_length=min_length,
            exclude_first_percent=exclude_first_percent,
            annotation_types=annotation_types
        )

        result['book_hash'] = book_hash
        return result

    def search_annotations_tool(
        self,
        query: str,
        max_results: int = 30,
        max_per_book: int = 5
    ) -> dict[str, Any]:
        """
        MCP Tool: Search through all annotations and Calibre comments via LanceDB.

        Args:
            query: Search query string
            max_results: Maximum number of results
            max_per_book: Maximum results per book to prevent one book dominating results

        Returns:
            Dictionary with search results
        """
        if self._ensure_rag_initialized():
            results = self.service.search(
                query=query,
                top_k=max_results,
                chunk_type_filter=ChunkType.ANNOTATIONS_AND_COMMENTS,
                section_filter=None,
                max_per_book=max_per_book if max_per_book > 0 else 999,
            )
            return {
                'query': query,
                'search_type': 'semantic',
                'result_count': len(results),
                'results': results
            }

        # Fallback: text search if LanceDB not initialized
        results = search_annotations(query, self.annotations_dir, case_sensitive=False)
        return {
            'query': query,
            'search_type': 'text',
            'result_count': len(results),
            'results': results
        }

    def list_annotated_books_tool(self) -> dict[str, Any]:
        """
        MCP Tool: List all books that have annotations.

        Returns:
            Dictionary with list of annotated books
        """
        books = list_all_annotated_books(self.annotations_dir)

        return {
            'total_books': len(books),
            'books': books
        }

    def compute_hash_tool(self, book_path: str) -> dict[str, Any]:
        """
        MCP Tool: Compute the annotation hash for a book path.

        This is useful for debugging annotation lookup issues.

        Args:
            book_path: Full path to the book file

        Returns:
            Dictionary with hash information
        """
        book_hash = compute_book_hash(book_path)
        annots_dir = self.annotations_dir or get_annotations_dir()
        expected_file = Path(annots_dir) / f"{book_hash}.json"

        return {
            'book_path': book_path,
            'hash': book_hash,
            'expected_annotation_file': str(expected_file),
            'file_exists': expected_file.exists()
        }

    def detect_duplicates_tool(
        self,
        method: str = 'title_author',
        include_doublette_tag: bool = True
    ) -> dict[str, Any]:
        """
        MCP Tool: Detect duplicate books in the library.

        Supports multiple detection methods:
        - 'title_author': Match by normalized title and author (most thorough)
        - 'isbn': Match by ISBN (most accurate)
        - 'exact_title': Match by exact title only

        Args:
            method: Detection method - 'title_author', 'isbn', or 'exact_title'
            include_doublette_tag: Also show books carrying the configured
                duplicate tag (default "duplicate")

        Returns:
            Dictionary with duplicate groups and statistics
        """
        if err := self._require_db():
            return err

        try:
            from src.archilles.config import get_duplicate_tag, get_library_path
            duplicate_tag = get_duplicate_tag(get_library_path(required=False))
            with CalibreAnalyzer(self.db_path) as analyzer:
                return analyzer.detect_duplicates(
                    method=method,
                    include_doublette_tag=include_doublette_tag,
                    duplicate_tag=duplicate_tag,
                )
        except Exception as e:
            return {'error': f'Failed to detect duplicates: {e}'}

    def get_book_details_tool(self, book_id: int) -> dict[str, Any]:
        """
        MCP Tool: Get detailed information about a specific document.

        Uses Calibre metadata.db when available; falls back to the SourceAdapter.

        Args:
            book_id: Calibre book ID (or adapter doc_id for non-Calibre sources)

        Returns:
            Dictionary with book/document details
        """
        if self.db_path:
            try:
                with CalibreAnalyzer(self.db_path) as analyzer:
                    book = analyzer.get_book_details(book_id)
                    if not book:
                        return {'error': f'Book with ID {book_id} not found'}
                    return book
            except Exception as e:
                return {'error': f'Failed to get book details: {e}'}

        if self.adapter:
            return self._get_book_details_via_adapter(str(book_id))

        return {
            'error': 'No metadata source available',
            'help': 'This tool requires a Calibre library with metadata.db or a configured adapter',
        }

    def _get_book_details_via_adapter(self, doc_id: str) -> dict[str, Any]:
        try:
            doc = self.adapter.get_metadata(doc_id)
            if not doc:
                return {'error': f'Document with ID {doc_id} not found'}
            return {
                'id': doc.doc_id,
                'title': doc.title,
                'authors': doc.authors,
                'pubdate': f"{doc.year}-01-01" if doc.year else None,
                'year': doc.year,
                'tags': doc.tags,
                'language': doc.language,
                'publisher': doc.publisher,
                'series': doc.series,
                'identifiers': doc.identifiers,
                'comments': doc.comments,
                'formats': [doc.file_format] if doc.file_format else [],
                'file_path': str(doc.file_path) if doc.file_path else None,
                'source': self.adapter.adapter_type,
            }
        except Exception as e:
            return {'error': f'Failed to get book details: {e}'}

    def get_doublette_tag_instruction_tool(self, book_id: int) -> dict[str, Any]:
        """
        MCP Tool: Get instructions for adding the duplicate tag to a book.

        Note: This server cannot modify the database directly.
        Use Calibre's calibredb command to actually add tags.

        Args:
            book_id: Calibre book ID

        Returns:
            Dictionary with tagging instructions
        """
        if err := self._require_db():
            return err

        try:
            from src.archilles.config import get_duplicate_tag, get_library_path
            duplicate_tag = get_duplicate_tag(get_library_path(required=False))
            with CalibreAnalyzer(self.db_path) as analyzer:
                return analyzer.add_doublette_tag(book_id, duplicate_tag=duplicate_tag)
        except Exception as e:
            return {'error': f'Failed to get tag instruction: {e}'}

    def export_bibliography_tool(
        self,
        format: str = 'bibtex',
        author: Optional[str] = None,
        tag: Optional[str] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        max_books: Optional[int] = None
    ) -> dict[str, Any]:
        """
        MCP Tool: Export bibliography in various formats.

        Supports BibTeX, RIS, EndNote, JSON, and CSV formats.
        Books can be filtered by author, tag, and publication year.
        Uses Calibre metadata.db when available; falls back to the SourceAdapter.

        Args:
            format: Export format - 'bibtex', 'ris', 'endnote', 'json', 'csv'
            author: Filter by author name (case-insensitive partial match)
            tag: Filter by tag name (case-insensitive partial match)
            year_from: Filter books published from this year
            year_to: Filter books published up to this year
            max_books: Maximum number of books to export

        Returns:
            Dictionary with exported bibliography data
        """
        if self.db_path:
            try:
                with CalibreAnalyzer(self.db_path) as analyzer:
                    return analyzer.export_bibliography(
                        format=format,
                        author=author,
                        tag=tag,
                        year_from=year_from,
                        year_to=year_to,
                        max_books=max_books
                    )
            except Exception as e:
                return {'error': f'Failed to export bibliography: {e}'}

        if self.adapter:
            return self._export_bibliography_via_adapter(
                fmt=format, author=author, tag=tag,
                year_from=year_from, year_to=year_to, max_books=max_books,
            )

        return {
            'error': 'No metadata source available',
            'help': 'This tool requires a Calibre library with metadata.db or a configured adapter',
        }

    def _export_bibliography_via_adapter(
        self,
        fmt: str,
        author: Optional[str],
        tag: Optional[str],
        year_from: Optional[int],
        year_to: Optional[int],
        max_books: Optional[int],
    ) -> dict[str, Any]:
        try:
            docs = self.adapter.list_documents()
            results = []
            for doc in docs:
                if author and not any(author.lower() in a.lower() for a in doc.authors):
                    continue
                if tag and not any(tag.lower() in t.lower() for t in doc.tags):
                    continue
                if year_from and (not doc.year or doc.year < year_from):
                    continue
                if year_to and (not doc.year or doc.year > year_to):
                    continue
                results.append(doc)
            if max_books:
                results = results[:max_books]

            books = [
                {
                    'id': doc.doc_id,
                    'title': doc.title,
                    'authors': doc.authors,
                    'year': doc.year,
                    'publisher': doc.publisher,
                    'identifiers': doc.identifiers,
                    'tags': doc.tags,
                    'formats': [doc.file_format] if doc.file_format else [],
                }
                for doc in results
            ]

            if fmt == 'json':
                data = json.dumps(books, indent=2, ensure_ascii=False)
            elif fmt in BIB_FORMATTERS:
                data = BIB_FORMATTERS[fmt](books)
            else:
                return {'error': f'Unsupported format: {fmt}'}

            return {
                'format': fmt,
                'book_count': len(books),
                'filters': {'author': author, 'tag': tag, 'year_from': year_from, 'year_to': year_to},
                'data': data,
                'source': self.adapter.adapter_type,
            }
        except Exception as e:
            return {'error': f'Failed to export bibliography: {e}'}

    def list_books_by_author_tool(
        self,
        author: str,
        tags: Optional[list[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        sort_by: str = 'title'
    ) -> dict[str, Any]:
        """
        MCP Tool: List all books by an author.

        Uses Calibre metadata.db when available (Calibre instances); falls back
        to the SourceAdapter for non-Calibre sources (Zotero, folder).

        Args:
            author: Author name (case-insensitive partial match, required)
            tags: Optional list of tag names to filter by (AND logic)
            year_from: Optional minimum publication year
            year_to: Optional maximum publication year
            sort_by: Sort order - 'title' (default) or 'year'

        Returns:
            Dictionary with matched books and metadata
        """
        if self.db_path:
            try:
                with CalibreAnalyzer(self.db_path) as analyzer:
                    return analyzer.list_books_by_author(
                        author=author,
                        tags=tags,
                        year_from=year_from,
                        year_to=year_to,
                        sort_by=sort_by
                    )
            except Exception as e:
                return {'error': f'Failed to list books by author: {e}'}

        if self.adapter:
            return self._list_books_by_author_via_adapter(
                author=author, tags=tags, year_from=year_from,
                year_to=year_to, sort_by=sort_by,
            )

        return {
            'error': 'No metadata source available',
            'help': 'This tool requires a Calibre library with metadata.db or a configured adapter',
        }

    def _list_books_by_author_via_adapter(
        self,
        author: str,
        tags: Optional[list[str]],
        year_from: Optional[int],
        year_to: Optional[int],
        sort_by: str,
    ) -> dict[str, Any]:
        """Adapter-based fallback for list_books_by_author (Zotero, folder, etc.)."""
        author_lower = author.lower()
        try:
            docs = self.adapter.list_documents()
            results = []
            for doc in docs:
                if not any(author_lower in a.lower() for a in doc.authors):
                    continue
                if tags:
                    doc_tags_lower = [t.lower() for t in doc.tags]
                    if not all(
                        any(ft.lower() in dt for dt in doc_tags_lower)
                        for ft in tags
                    ):
                        continue
                if year_from and doc.year and doc.year < year_from:
                    continue
                if year_to and doc.year and doc.year > year_to:
                    continue
                results.append({
                    'calibre_id': doc.doc_id,
                    'title': doc.title,
                    'author': ' & '.join(doc.authors),
                    'year': doc.year,
                    'tags': doc.tags,
                    'publisher': doc.publisher,
                })
            if sort_by == 'year':
                results.sort(key=lambda x: x.get('year') or 0, reverse=True)
            else:
                results.sort(key=lambda x: (x.get('title') or '').lower())
            return {
                'books': results,
                'count': len(results),
                'author_query': author,
                'source': self.adapter.adapter_type,
            }
        except Exception as e:
            return {'error': f'Failed to list books by author: {e}'}

    def list_tags_tool(
        self,
        min_books: int = 1,
        max_tags: int = 100
    ) -> dict[str, Any]:
        """
        MCP Tool: List all tags with book counts.

        Uses Calibre metadata.db when available; falls back to the SourceAdapter.

        Args:
            min_books: Only show tags with at least this many books (default: 1)
            max_tags: Maximum number of tags to return (default: 100)

        Returns:
            Dictionary with tag list and statistics
        """
        if self.db_path:
            try:
                with CalibreAnalyzer(self.db_path) as analyzer:
                    all_tags = analyzer.get_tags_stats()
                    filtered = [t for t in all_tags if t['book_count'] >= min_books]
                    limited = filtered[:max_tags]
                    return {
                        'total_tags': len(all_tags),
                        'filtered_tags': len(filtered),
                        'returned_tags': len(limited),
                        'tags': limited,
                        'usage': 'Use these tag names in the "tags" parameter of search_books_with_citations'
                    }
            except Exception as e:
                return {'error': f'Failed to list tags: {e}'}

        if self.adapter:
            return self._list_tags_via_adapter(min_books, max_tags)

        return {
            'error': 'No metadata source available',
            'help': 'This tool requires a Calibre library with metadata.db or a configured adapter',
        }

    def _list_tags_via_adapter(self, min_books: int, max_tags: int) -> dict[str, Any]:
        try:
            tag_counts: dict[str, int] = {}
            for doc in self.adapter.list_documents():
                for tag in doc.tags:
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            all_tags = [
                {'name': tag, 'book_count': count}
                for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1])
            ]
            filtered = [t for t in all_tags if t['book_count'] >= min_books]
            limited = filtered[:max_tags]
            return {
                'total_tags': len(all_tags),
                'filtered_tags': len(filtered),
                'returned_tags': len(limited),
                'tags': limited,
                'usage': 'Use these tag names in the "tags" parameter of search_books_with_citations',
                'source': self.adapter.adapter_type,
            }
        except Exception as e:
            return {'error': f'Failed to list tags: {e}'}

    def set_research_interests_tool(
        self,
        keywords: Optional[list[str]] = None,
        boost_factor: float = 0.15,
        action: str = 'set',
    ) -> dict[str, Any]:
        """
        MCP Tool: View or update research interest keywords for score boosting.

        Args:
            keywords: List of keywords to set (required for action='set')
            boost_factor: Additive score boost per matching keyword (0.0–1.0, default: 0.15)
            action: 'set' to update keywords, 'get' to view current keywords

        Returns:
            Dictionary with current keywords and boost_factor
        """
        if not self._archilles_dir:
            return {'error': 'Library path not configured — cannot locate .archilles/ directory'}

        try:
            from src.retriever.research_boost import (
                load_research_interests,
                save_research_interests,
            )

            if action == 'get':
                loaded_keywords, loaded_boost = load_research_interests(self._archilles_dir)
                return {
                    'action': 'get',
                    'keywords': loaded_keywords,
                    'boost_factor': loaded_boost,
                    'keyword_count': len(loaded_keywords),
                    'file': str(self._archilles_dir / "research_interests.json"),
                }

            if action == 'set':
                if keywords is None:
                    return {'error': 'keywords parameter required for action="set"'}
                save_research_interests(self._archilles_dir, keywords, boost_factor)
                return {
                    'action': 'set',
                    'keywords': keywords,
                    'boost_factor': boost_factor,
                    'keyword_count': len(keywords),
                    'message': f'Saved {len(keywords)} keywords. Boost will apply to future searches.',
                }

            return {'error': f'Unknown action: {action!r}. Use "get" or "set".'}

        except Exception as e:
            logger.error("set_research_interests failed: %s", e, exc_info=True)
            return {'error': str(e)}

    def search_books_with_citations_tool(
        self,
        query: str,
        top_k: int = 10,
        mode: str = 'hybrid',
        language: Optional[str] = None,
        tags: Optional[list[str]] = None,
        expand_context: bool = False,
        boost_research_interests: bool = True,
        max_per_book: int = 3,
    ) -> dict[str, Any]:
        """
        MCP Tool: Search books and generate XML-structured prompts with citation support.

        This tool combines RAG search with XML prompt generation, providing:
        - Structured <documents> with metadata
        - System prompt with citation instructions
        - Ready-to-use prompts for Claude

        Args:
            query: Search query
            top_k: Number of results to return (default: 5)
            mode: Search mode - 'hybrid', 'semantic', or 'keyword' (default: 'hybrid')
            language: Filter by language (e.g., 'de', 'en', 'la')
            tags: Filter by Calibre tags (e.g., ['Geschichte', 'Philosophie'])
            expand_context: Enable context expansion (Small-to-Big) if char_offsets available

        Returns:
            Dictionary with XML prompts and search results
        """
        if not self._ensure_rag_initialized():
            return {
                'error': 'RAG system not available',
                'help': 'RAG system requires ArchillesService to be installed and initialized',
                'initialization_log': 'Check ~/.archilles/mcp_server.log for details'
            }

        try:
            result = self.service.search_with_citations(
                query=query,
                top_k=top_k,
                mode=mode,
                language=language,
                tags=tags,
                expand_context=expand_context,
                boost_research_interests=boost_research_interests,
                max_per_book=max_per_book,
            )

            if "error" in result:
                return result

            results = result.get("results", [])
            if not results:
                return {
                    'results': [],
                    'message': 'No results found',
                    'query': query
                }

            # Return structured response
            return {
                'query': query,
                'num_results': result['num_results'],
                'search_mode': mode,
                'language_filter': language,
                'system_prompt': result['system_prompt'],
                'user_prompt': result['user_prompt'],
                'usage_instructions': (
                    "Copy 'system_prompt' to Claude's System Prompt field, "
                    "then copy 'user_prompt' to the message. "
                    "Claude will cite sources as [doc_1], [doc_2], etc."
                ),
                'raw_results': [
                    {
                        'rank': r['rank'],
                        'text_preview': r['text'][:200] + '...' if len(r['text']) > 200 else r['text'],
                        'similarity': r.get('similarity', 0),
                        'metadata': {
                            'author': r['metadata'].get('author'),
                            'title': r['metadata'].get('book_title'),
                            'year': r['metadata'].get('year'),
                            'page': r['metadata'].get('page'),
                        }
                    }
                    for r in results
                ]
            }

        except Exception as e:
            logger.error(f"Search with citations failed: {e}", exc_info=True)
            return {
                'error': f'Search failed: {str(e)}',
                'query': query
            }


    def watchdog_scan_tool(
        self,
        dry_run: bool = False,
        queue_new: bool = True,
        index_new: bool = False,
        first_authors: list[str] | None = None,
        first_tags: list[str] | None = None,
        first_titles: list[str] | None = None,
        max_new: int | None = None,
    ) -> dict[str, Any]:
        """
        MCP Tool: Scan the library for changes and sync into LanceDB.

        Serves **Calibre and Zotero** sources. Both scanners have existed for
        months and both run daily through the scheduled routines; only this
        tool was Calibre-only, so from a client Zotero could not be scanned at
        all — capability present, access missing.

        ``max_new`` caps how many new items a run indexes. It matters most for
        Zotero, whose backlog is large enough that an uncapped run cannot
        finish inside a client's request timeout.

        Detects three change types:
          new_books          — in Calibre but not yet indexed
          metadata_changed   — title / author / tags / comments / publisher changed
          annotations_changed — highlights or notes changed

        Metadata detection runs entirely on SQLite + LanceDB hash lookups
        (no book files opened). Annotation detection caches a (mtime, size)
        signature per book; the first scan opens each annotated PDF once to
        seed the cache, and subsequent scans only reopen books whose
        signature changed.

        Delta updates (metadata + annotations) are applied automatically unless
        dry_run=True.  New books are written to index_queue.json for later
        batch indexing, or indexed immediately when index_new=True.

        Designed to be called from a Claude Routine (e.g. once a day or once
        a week — whatever matches how often you edit your library) to keep
        LanceDB automatically in sync with an evolving Calibre collection.
        Non-Claude users can schedule scripts/watchdog.py via Windows Task
        Scheduler or cron; see docs/MCP_GUIDE.md § "Keeping Your Index in Sync".

        Args:
            dry_run:       Report changes only; do not modify LanceDB or the queue.
            queue_new:     Write newly discovered Calibre IDs to index_queue.json.
            index_new:     Index new books immediately (slow: ~90s/book with embeddings).
            first_authors: Substring list — books by matching author are indexed first.
            first_tags:    Substring list — books carrying a matching tag are indexed first.
            first_titles:  Substring list — books with a matching title are indexed first.
            max_new:       Cap on new items indexed in this run (None = no cap).

        Within each priority group, books are ordered 5★ → 4★ → 3★ → unrated → 2–1★.

        Returns:
            Dictionary with scan results: counts, changed IDs, timing, errors.
        """
        if not self.library_path:
            return {
                'error': 'Library path not configured',
                'help': 'Set ARCHILLES_LIBRARY_PATH environment variable',
            }
        if not self._archilles_dir:
            return {'error': 'Archilles directory not resolved'}

        adapter_type = getattr(self.adapter, "adapter_type", "calibre") if self.adapter else "calibre"
        if adapter_type not in ("calibre", "zotero"):
            return {
                'error': (
                    f"watchdog_scan supports Calibre and Zotero sources; this "
                    f"one is {adapter_type!r}"
                ),
                'help': (
                    "Path-keyed sources (folder, obsidian) sync via "
                    "scripts/batch_index.py --all --skip-existing; they have no "
                    "hash-diff scanner yet."
                ),
            }

        try:
            from src.archilles.config import get_excluded_tags
            from src.archilles import watchdog as _watchdog

            scanner_cls = (
                _watchdog.ZoteroWatchdogScanner if adapter_type == "zotero"
                else _watchdog.WatchdogScanner
            )
            scanner = scanner_cls(
                library_path=self.library_path,
                db_path=self.rag_db_path,
                archilles_dir=self._archilles_dir,
                excluded_tags=get_excluded_tags(self.library_path),
            )
            results = scanner.scan(
                dry_run=dry_run,
                queue_new=queue_new,
                index_new=index_new,
                first_authors=first_authors,
                first_tags=first_tags,
                first_titles=first_titles,
                max_new=max_new,
            )
            # Add human-readable summary for the Routine output
            summary = (
                f"Scanned {results['scanned']} books: "
                f"{len(results['new_books'])} new, "
                f"{len(results['metadata_changed'])} metadata changed, "
                f"{len(results['annotations_changed'])} annotations changed, "
                f"{results['delta_updates']} delta updates"
            )
            if results.get('new_indexed'):
                summary += f", {results['new_indexed']} new books indexed"
            summary += f" in {results['total_time']}s"
            results['summary'] = summary
            return results
        except Exception as exc:
            logger.error("watchdog_scan failed: %s", exc, exc_info=True)
            return {'error': str(exc)}


def create_mcp_tools(server: CalibreMCPServer) -> list[dict]:
    """
    Create MCP tool definitions for the server.

    Args:
        server: The CalibreMCPServer instance

    Returns:
        List of MCP tool definitions
    """
    return [
        {
            'name': 'get_book_annotations',
            'description': 'Get annotations (highlights, notes, bookmarks) for a specific book with intelligent filtering to exclude TOC markers and technical highlights',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'book_path': {
                        'type': 'string',
                        'description': 'Full path to the book file'
                    },
                    'annotation_type': {
                        'type': 'string',
                        'enum': ['highlight', 'note', 'bookmark'],
                        'description': 'Optional filter for annotation type'
                    },
                    'exclude_toc_markers': {
                        'type': 'boolean',
                        'description': 'Whether to exclude TOC markers and technical highlights (default: true)',
                        'default': True
                    },
                    'include_pdf': {
                        'type': 'boolean',
                        'description': 'Whether to extract PDF-internal annotations for PDF files (default: true)',
                        'default': True
                    },
                    'min_length': {
                        'type': 'integer',
                        'description': 'Minimum character length for annotations (default: 20)',
                        'default': 20
                    },
                    'exclude_first_percent': {
                        'type': 'number',
                        'description': 'Exclude annotations in first X% of book (default: 5.0)',
                        'default': 5.0
                    }
                },
                'required': ['book_path']
            }
        },
        {
            'name': 'search_annotations',
            'description': 'Semantically search highlights, notes, and Calibre comments via LanceDB',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Search query string'
                    },
                    'max_results': {
                        'type': 'integer',
                        'description': 'Maximum number of results',
                        'default': 30
                    },
                    'max_per_book': {
                        'type': 'integer',
                        'description': 'Maximum results per book (0 for unlimited)',
                        'default': 5
                    }
                },
                'required': ['query']
            }
        },
        {
            'name': 'list_annotated_books',
            'description': 'List all books that have annotations',
            'inputSchema': {
                'type': 'object',
                'properties': {}
            }
        },
        {
            'name': 'compute_annotation_hash',
            'description': 'Compute the annotation hash for a book path (for debugging)',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'book_path': {
                        'type': 'string',
                        'description': 'Full path to the book file'
                    }
                },
                'required': ['book_path']
            }
        },
        {
            'name': 'detect_duplicates',
            'description': 'Detect duplicate books in the library using various methods (title+author, ISBN, or exact title). Also shows books carrying the configured duplicate tag (default "duplicate").',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'method': {
                        'type': 'string',
                        'enum': ['title_author', 'isbn', 'exact_title'],
                        'description': 'Detection method: title_author (normalized title+author), isbn (by ISBN), or exact_title (exact title match)',
                        'default': 'title_author'
                    },
                    'include_doublette_tag': {
                        'type': 'boolean',
                        'description': 'Also show books carrying the configured duplicate tag (default "duplicate")',
                        'default': True
                    }
                }
            }
        },
        {
            'name': 'get_book_details',
            'description': 'Get detailed information about a specific book by Calibre book ID',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'book_id': {
                        'type': 'integer',
                        'description': 'Calibre book ID'
                    }
                },
                'required': ['book_id']
            }
        },
        {
            'name': 'get_doublette_tag_instruction',
            'description': 'Get instructions for adding the duplicate tag to a book (for manual tagging)',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'book_id': {
                        'type': 'integer',
                        'description': 'Calibre book ID to tag'
                    }
                },
                'required': ['book_id']
            }
        },
        {
            'name': 'export_bibliography',
            'description': 'Export bibliography in various formats (BibTeX, RIS, EndNote, JSON, CSV). Books can be filtered by author, tag, and publication year.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'format': {
                        'type': 'string',
                        'enum': ['bibtex', 'ris', 'endnote', 'json', 'csv'],
                        'description': 'Export format: bibtex (LaTeX), ris (Reference Manager), endnote, json, or csv',
                        'default': 'bibtex'
                    },
                    'author': {
                        'type': 'string',
                        'description': 'Filter by author name (case-insensitive partial match)'
                    },
                    'tag': {
                        'type': 'string',
                        'description': 'Filter by tag name (case-insensitive partial match)'
                    },
                    'year_from': {
                        'type': 'integer',
                        'description': 'Filter books published from this year (e.g., 2000)'
                    },
                    'year_to': {
                        'type': 'integer',
                        'description': 'Filter books published up to this year (e.g., 2023)'
                    },
                    'max_books': {
                        'type': 'integer',
                        'description': 'Maximum number of books to export'
                    }
                }
            }
        },
        {
            'name': 'list_books_by_author',
            'description': 'List all books by an author from the Calibre metadata database. Direct metadata query (not vector search), reliable for short texts like articles or book chapters. Supports tag and year filtering.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'author': {
                        'type': 'string',
                        'description': 'Author name to search for (case-insensitive partial match, e.g., "Mason" matches "Steve Mason")'
                    },
                    'tags': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Optional list of tag names to filter by (AND logic, case-insensitive partial match, e.g., ["Artikel"])'
                    },
                    'year_from': {
                        'type': 'integer',
                        'description': 'Filter books published from this year (e.g., 2000)'
                    },
                    'year_to': {
                        'type': 'integer',
                        'description': 'Filter books published up to this year (e.g., 2023)'
                    },
                    'sort_by': {
                        'type': 'string',
                        'enum': ['title', 'year'],
                        'description': 'Sort order: by title (default, alphabetical) or by year (descending)',
                        'default': 'title'
                    }
                },
                'required': ['author']
            }
        },
        {
            'name': 'list_tags',
            'description': 'List all tags in the Calibre library with book counts. Useful for discovering available tags before filtering search results.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'min_books': {
                        'type': 'integer',
                        'description': 'Only show tags with at least this many books (default: 1)',
                        'default': 1
                    },
                    'max_tags': {
                        'type': 'integer',
                        'description': 'Maximum number of tags to return (default: 100)',
                        'default': 100
                    }
                }
            }
        },
        {
            'name': 'search_books_with_citations',
            'description': 'Search books with RAG and generate XML-structured prompts with citation support. Returns system prompt + user prompt ready for Claude Desktop. Claude will cite sources as [doc_1], [doc_2], etc.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Search query (e.g., "Was ist Herrschaftslegitimation?")'
                    },
                    'top_k': {
                        'type': 'integer',
                        'description': 'Number of results to return (default: 10)',
                        'default': 10
                    },
                    'mode': {
                        'type': 'string',
                        'enum': ['hybrid', 'semantic', 'keyword'],
                        'description': 'Search mode: hybrid (recommended), semantic (meaning-based), or keyword (exact matching)',
                        'default': 'hybrid'
                    },
                    'language': {
                        'type': 'string',
                        'description': 'Filter by language code (e.g., "de" for German, "en" for English, "la" for Latin)'
                    },
                    'tags': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'Filter by Calibre tags (e.g., ["Geschichte", "Philosophie"]). Results must match ALL tags (AND logic).'
                    },
                    'expand_context': {
                        'type': 'boolean',
                        'description': 'Enable context expansion (Small-to-Big retrieval) if char_offsets available',
                        'default': False
                    },
                    'boost_research_interests': {
                        'type': 'boolean',
                        'description': 'Apply research interest keyword boosting to re-rank results (default: true). Configure keywords with set_research_interests.',
                        'default': True
                    },
                    'max_per_book': {
                        'type': 'integer',
                        'description': 'Maximum results per book to ensure source diversity (default: 3)',
                        'default': 3
                    }
                },
                'required': ['query']
            }
        },
        {
            'name': 'set_research_interests',
            'description': 'View or update research interest keywords. When set, results containing these keywords receive a score boost and rise in ranking — without re-indexing.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'action': {
                        'type': 'string',
                        'enum': ['get', 'set'],
                        'description': '"get" to view current keywords, "set" to update them',
                        'default': 'get'
                    },
                    'keywords': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of keywords to boost (e.g., ["Josephus", "Mithras", "priestly elite"]). Required for action="set".'
                    },
                    'boost_factor': {
                        'type': 'number',
                        'description': 'Additive score boost per matching keyword, 0.0–1.0 (default: 0.15)',
                        'default': 0.15
                    }
                },
                'required': ['action']
            }
        },
        {
            'name': 'watchdog_scan',
            'description': (
                'Scan the Calibre library for changes and automatically sync them into LanceDB. '
                'Detects new books, metadata changes (title/tags/comments), and annotation changes. '
                'Metadata detection runs purely on SQLite + LanceDB hash lookups; annotation '
                'detection caches a (mtime, size) signature per book so repeat scans only '
                "reopen books whose file signature changed. Delta updates are applied instantly; "
                'new books are queued. Call this tool from a Claude Routine (daily or weekly is '
                'usually enough) to keep the index current without manual re-indexing.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'dry_run': {
                        'type': 'boolean',
                        'description': 'Report changes only without modifying LanceDB or the queue (default: false)',
                        'default': False
                    },
                    'queue_new': {
                        'type': 'boolean',
                        'description': 'Write newly discovered Calibre IDs to index_queue.json for later indexing (default: true)',
                        'default': True
                    },
                    'index_new': {
                        'type': 'boolean',
                        'description': 'Index new books immediately with full embeddings — slow, ~90s per book (default: false)',
                        'default': False
                    }
                }
            }
        }
    ]


if __name__ == '__main__':
    # Example usage
    server = CalibreMCPServer()

    # Test with example book path
    test_path = r"D:\Calibre Library\Author Name\Book Title (1)\book.epub"

    result = server.compute_hash_tool(test_path)
    print(json.dumps(result, indent=2))
