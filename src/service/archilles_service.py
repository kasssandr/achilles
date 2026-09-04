"""
Central service layer for ARCHILLES.

Wraps ArchillesRAG and provides a clean interface used by:
- MCP server (src/calibre_mcp/server.py)
- Web UI (scripts/web_ui.py)
- CLI (scripts/rag_demo.py)

Handles lazy initialization, stdout redirection (for MCP safety),
and optional cross-encoder reranking.
"""

import logging
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from src.archilles.constants import ChunkType, SectionType
from src.archilles.stdout_guard import redirect_stdout_to_stderr
from src.retriever.results import diversify_results, matches_tag_filter  # noqa: F401  (Re-Export — Alt-Abnehmer importieren von hier)

logger = logging.getLogger(__name__)

# The guard now lives in src/archilles/stdout_guard.py so mcp_server.py can wrap
# the whole tool dispatch with it (review 1.3) without importing this module.
# Both must share one refcount — two would race — hence the alias rather than a
# second implementation.
_redirect_stdout_to_stderr = redirect_stdout_to_stderr


# Serialises embedding-/reranker-model construction across ALL service instances.
# transformers/sentence-transformers initialise a model on the 'meta' device and
# then materialise its weights via .to_empty(); running that from several threads
# at once — as the unified server does when it initialises every source in
# parallel — races and raises "Cannot copy out of meta tensor". Holding this lock
# means at most one model is built at a time (loads run sequentially, once each).
_model_init_lock = threading.Lock()


# Process-wide cache of CrossEncoderReranker instances, keyed by
# (model_name, device). The unified server creates one service per source;
# sharing the (lazily loading) wrapper means all sources share one loaded
# cross-encoder instead of one ~2.3 GB copy each. Guarded by
# _model_init_lock in _get_reranker.
_shared_rerankers: dict = {}


def _get_shared_reranker(model_name: str | None, device: str | None):
    """Return a process-wide shared CrossEncoderReranker for (model_name, device).

    Caller must hold ``_model_init_lock``.
    """
    key = (model_name, device)
    reranker = _shared_rerankers.get(key)
    if reranker is None:
        from src.retriever import CrossEncoderReranker
        reranker = CrossEncoderReranker(model_name=model_name, device=device)
        _shared_rerankers[key] = reranker
    return reranker


def _filter_by_rerank_score(results: list[dict], min_similarity: float) -> list[dict]:
    """Filter reranked results by min_similarity (finding 4.1).

    rerank_score is sigmoid-bounded 0-1 (see CrossEncoderReranker), so a
    cosine-style threshold is meaningful there. Results WITHOUT a
    rerank_score (reranker unavailable mid-flight) pass through unfiltered —
    their RRF-scale score (~1/60) must never be compared against a 0-1
    threshold, which would silently empty the list.
    """
    if min_similarity <= 0:
        return results
    return [
        r for r in results
        if r.get("rerank_score") is None or r["rerank_score"] >= min_similarity
    ]


class ArchillesService:
    """Central service facade for ARCHILLES RAG system."""

    def __init__(
        self,
        db_path: str = "./archilles_rag_db",
        model_name: str | None = None,
        profile: str | None = None,
        enable_ocr: bool = False,
        force_ocr: bool = False,
        ocr_backend: str = "auto",
        ocr_language: str | None = None,
        languages: list[str] | None = None,
        hierarchical: bool = False,
        enable_reranking: bool = False,
        reranker_model: str | None = None,
        reranker_device: str | None = None,
        citation_config: Any | None = None,
        archilles_dir: str | None = None,
        adapter=None,
    ):
        """
        Initialize service (RAG loading is deferred to first use).

        Args:
            db_path: Path to LanceDB storage
            model_name: Sentence transformer model (overrides profile)
            profile: Hardware profile (minimal/balanced/maximal)
            enable_ocr: Enable OCR for scanned PDFs
            force_ocr: Force OCR even for digital PDFs
            ocr_backend: OCR backend (auto, tesseract, lighton, olmocr)
            ocr_language: Language codes for Tesseract
            hierarchical: Enable parent-child chunking
            enable_reranking: Enable cross-encoder reranking
            reranker_model: Cross-encoder model name (default: bge-reranker-v2-m3)
            reranker_device: Device for reranker (None = auto)
            citation_config: CitationConfig instance for bibliography formatting
            archilles_dir: Path to .archilles/ directory (for research_interests.json)
        """
        self._config = {
            "db_path": db_path,
            "model_name": model_name,
            "profile": profile,
            "enable_ocr": enable_ocr,
            "force_ocr": force_ocr,
            "ocr_backend": ocr_backend,
            "ocr_language": ocr_language,
            "languages": languages,
            "hierarchical": hierarchical,
            "adapter": adapter,
        }
        self._rag = None
        self._init_attempted = False
        self._enable_reranking = enable_reranking
        self._reranker = None
        self._reranker_model = reranker_model
        self._reranker_device = reranker_device
        self._citation_config = citation_config
        self._archilles_dir = Path(archilles_dir) if archilles_dir else None
        self._adapter = adapter  # Optional SourceAdapter for metadata lookup

    def _ensure_initialized(self) -> bool:
        """
        Lazy-load the RAG system. Returns True if available.

        Redirects stdout to stderr during initialization to prevent
        print statements from corrupting MCP JSON-RPC communication.
        """
        if self._rag is not None:
            return True
        # NOTE: no lock-free `_init_attempted` fast path here. That flag is
        # already True while another thread (e.g. the preload thread at MCP
        # server start) is still mid-initialization — short-circuiting to
        # False made concurrent searches silently return zero results for
        # this source. Waiting on the lock is correct: once acquired, the
        # running init has finished and the re-check below gives the truth.

        # Serialise model construction across all sources: the unified server
        # initialises every source in parallel threads, and concurrent
        # meta-device weight materialisation races ("Cannot copy out of meta
        # tensor"). The lock ensures models load one at a time.
        with _model_init_lock:
            # Re-check under the lock — another thread may have advanced state
            # while we were waiting.
            if self._rag is not None:
                return True
            if self._init_attempted:
                return False

            self._init_attempted = True

            try:
                from src.archilles.engine import ArchillesRAG
            except ImportError:
                logger.warning("ArchillesRAG not available (import failed)")
                return False

            try:
                logger.info("Initializing RAG system (lazy loading)...")
                with _redirect_stdout_to_stderr():
                    self._rag = ArchillesRAG(**self._config)
                logger.info(f"RAG system initialized: {self._config['db_path']}")
                return True
            except Exception as e:
                logger.error(f"Failed to initialize RAG system: {e}", exc_info=True)
                self._rag = None
                return False

    def _get_reranker(self):
        """Lazy-load the cross-encoder reranker."""
        if self._reranker is not None:
            return self._reranker
        if not self._enable_reranking:
            return None

        # Same race as the embedding model: serialise reranker construction so
        # parallel sources don't materialise meta tensors concurrently.
        with _model_init_lock:
            if self._reranker is not None:
                return self._reranker
            if not self._enable_reranking:
                return None
            try:
                self._reranker = _get_shared_reranker(
                    self._reranker_model, self._reranker_device,
                )
                return self._reranker
            except Exception as e:
                logger.warning(f"Failed to create reranker: {e}")
                self._enable_reranking = False
                return None

    @property
    def is_initialized(self) -> bool:
        """Whether the RAG system is loaded and ready."""
        return self._rag is not None

    # ── Search ──────────────────────────────────────────────────

    def search(
        self,
        query: str,
        mode: Literal["semantic", "keyword", "hybrid"] = "hybrid",
        top_k: int = 10,
        language: str | None = None,
        book_id: str | None = None,
        exact_phrase: bool = False,
        tag_filter: list[str] | None = None,
        section_filter: str = SectionType.MAIN,
        chunk_type_filter: str = ChunkType.CONTENT,
        max_per_book: int = 2,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        """
        Search for relevant passages.

        Delegates to ArchillesRAG.query() and optionally applies
        cross-encoder reranking before returning results.

        Returns:
            List of result dicts with text, metadata, and scores.
        """
        if not self._ensure_initialized():
            return []

        reranker = self._get_reranker()

        query_kwargs = dict(
            query_text=query,
            mode=mode,
            language=language,
            book_id=book_id,
            exact_phrase=exact_phrase,
            tag_filter=tag_filter,
            section_filter=section_filter,
            chunk_type_filter=chunk_type_filter,
        )

        with _redirect_stdout_to_stderr():
            if reranker is None:
                return self._rag.query(
                    **query_kwargs,
                    top_k=top_k,
                    max_per_book=max_per_book,
                    min_similarity=min_similarity,
                )

            # Fetch more undiversified results for reranking
            raw_results = self._rag.query(
                **query_kwargs,
                top_k=30,
                max_per_book=999,
                min_similarity=0.0,
            )

        reranked = reranker.rerank(query, raw_results, top_k=top_k * 3)
        results = self._diversify(reranked, max_per_book, top_k)

        return _filter_by_rerank_score(results, min_similarity)

    def search_with_citations(
        self,
        query: str,
        top_k: int = 10,
        mode: Literal["hybrid", "semantic", "keyword"] = "hybrid",
        language: str | None = None,
        tags: list[str] | None = None,
        expand_context: bool = False,
        boost_research_interests: bool = True,
        max_per_book: int = 3,
    ) -> dict[str, Any]:
        """
        Search and generate XML-structured prompts with citation support.

        If boost_research_interests is True and archilles_dir was provided at
        init time, applies keyword boosting from research_interests.json before
        ranking results.

        Returns:
            Dictionary with system_prompt, user_prompt, num_sources, results.
        """
        if not self._ensure_initialized():
            return {
                "error": "RAG system not available",
                "help": "Check ~/.archilles/mcp_server.log for details",
            }

        reranker = self._get_reranker()

        with _redirect_stdout_to_stderr():
            if reranker is None:
                results = self._rag.query(
                    query_text=query,
                    top_k=top_k,
                    mode=mode,
                    language=language,
                    tag_filter=tags,
                    max_per_book=max_per_book,
                )
            else:
                # Finding 4.3: the citation path (primary MCP path) used to
                # skip the cross-encoder while search() reranked — same
                # pattern as search(): broad fetch, rerank, then diversify.
                raw_results = self._rag.query(
                    query_text=query,
                    top_k=30,
                    mode=mode,
                    language=language,
                    tag_filter=tags,
                    max_per_book=999,
                )
                reranked = reranker.rerank(query, raw_results, top_k=top_k * 3)
                results = self._diversify(reranked, max_per_book, top_k)

        if boost_research_interests and self._archilles_dir:
            try:
                from src.retriever.research_boost import (
                    apply_research_boost,
                    load_effective_research_interests,
                )
                keywords, boost_factor = load_effective_research_interests(self._archilles_dir)
                if keywords:
                    results = apply_research_boost(results, keywords, boost_factor)
                    logger.debug("Applied research boost (%d keywords)", len(keywords))
            except Exception as e:
                logger.warning("Research boost failed (non-fatal): %s", e)

        if not results:
            return {
                "results": [],
                "message": "No results found",
                "query": query,
            }

        claude_prompt = self.build_claude_prompt(
            results=results,
            query=query,
            expand_context=expand_context,
        )

        return {
            "query": query,
            "num_results": len(results),
            "search_mode": mode,
            "language_filter": language,
            "system_prompt": claude_prompt["system"],
            "user_prompt": claude_prompt["user"],
            "results": results,
        }

    def build_claude_prompt(
        self,
        results: list[dict[str, Any]],
        query: str,
        expand_context: bool = False,
        citation_config: Any | None = None,
    ) -> dict[str, Any]:
        """Build a Claude prompt from already-retrieved results.

        Public path for consumers that previously reached through
        ``service._rag.create_claude_prompt`` (review finding 5.15).
        On engine-init failure returns an error dict with empty prompts.
        """
        if not self._ensure_initialized():
            return {"error": "RAG system not available", "system": "", "user": ""}
        return self._rag.create_claude_prompt(
            results=results,
            query_text=query,
            expand_context=expand_context,
            citation_config=citation_config or self._citation_config,
        )

    # ── Index operations ────────────────────────────────────────

    def get_index_status(self) -> dict[str, Any]:
        """Get database statistics (total chunks, books, file types, etc.)."""
        if not self._ensure_initialized():
            return {"total_chunks": 0, "total_books": 0}
        return self._rag.store.get_stats()

    def get_book_list(self) -> list[dict[str, Any]]:
        """Get list of all indexed books with statistics."""
        if not self._ensure_initialized():
            return []
        return self._rag.store.get_indexed_books()

    def get_chunk_by_id(self, chunk_id: str) -> dict[str, Any] | None:
        """Get a single chunk by its ID (used for parent lookup)."""
        if not self._ensure_initialized():
            return None
        return self._rag.store.get_by_id(chunk_id)

    # ── Internal helpers ────────────────────────────────────────

    @staticmethod
    def _diversify(
        results: list[dict[str, Any]],
        max_per_book: int,
        top_k: int,
    ) -> list[dict[str, Any]]:
        return diversify_results(results, max_per_book, top_k)
