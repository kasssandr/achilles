"""
Unified MCP server — wraps multiple :class:`CalibreMCPServer` instances behind
a single process for the multi-source ARCHILLES configuration.

When a master config (``~/.archilles/config.json`` or ``$ARCHILLES_CONFIG_PATH``)
describes multiple sources (Calibre, Folder, Obsidian, Zotero, ...), this
server instantiates one inner server per source with its own adapter and
LanceDB, and routes tool calls:

- single-source tools dispatch via the ``source`` parameter (default =
  ``master.default_source``);
- aggregation tools (search, list-style) fan out across all sources in
  parallel and merge the results — search-tools merge by score, list-tools
  concatenate with source markers;
- ``detect_duplicates`` is gated behind
  :pyattr:`UnifiedMCPServer.calibre_sources` and requires an explicit
  ``source``; ``watchdog_scan`` is gated behind
  :pyattr:`UnifiedMCPServer.watchdog_sources` (Calibre *and* Zotero — both have
  a hash-diff scanner);
- ``set_research_interests`` reads/writes a master file when ``source`` is
  ``None``, or the source-local file when set, and the search-time boost
  layers them via ``load_effective_research_interests``.

The legacy single-source :class:`CalibreMCPServer` is left untouched —
``mcp_server.py`` chooses between the two depending on whether a master
config is present.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from src.archilles.config import (
    MasterConfig,
    master_archilles_dir,
    resolve_source_config,
)
from src.calibre_mcp.server import CalibreMCPServer, create_mcp_tools

logger = logging.getLogger(__name__)


def _year_sort_key(value) -> int:
    """Coerce a year value (int, str, or None) to int for cross-source sorting.

    Calibre paths historically deliver years as strings ('2019'), adapter
    paths as ints — mixing them in sort() raised TypeError (finding 5.2).
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def merge_source_results(per_source: list, top_k: int) -> list:
    """Merge per-source result lists into one ranking (finding 5.4).

    Raw scores from separate LanceDB instances are NOT comparable — RRF
    scores are a function of local candidate ranks, so a raw-score sort put
    every result of the 'louder' source above all results of the others.

    Strategy:
    - If EVERY result carries a rerank_score, sort by it: cross-encoder
      scores are query-document specific and therefore comparable across
      sources (sigmoid-bounded 0-1, see CrossEncoderReranker).
    - Otherwise merge rank-based: RRF over sources (k=60), i.e. results are
      interleaved by their rank within their own source.

    Args:
        per_source: List of (source_name, results) tuples; each result may
            carry 'rank' (1-based within its source) and 'rerank_score'.
        top_k: Number of merged results to return (re-ranked 1..top_k).
    """
    merged = []
    for _name, results in per_source:
        for fallback_rank, result in enumerate(results, 1):
            result["_merge_rank"] = result.get("rank") or fallback_rank
            merged.append(result)

    if not merged:
        return []

    if all(r.get("rerank_score") is not None for r in merged):
        merged.sort(key=lambda r: r["rerank_score"], reverse=True)
    else:
        merged.sort(key=lambda r: 1.0 / (60 + r["_merge_rank"]), reverse=True)

    for result in merged:
        result.pop("_merge_rank", None)

    merged = merged[:top_k]
    for i, result in enumerate(merged, 1):
        result["rank"] = i
    return merged


# ── Tool classification (used by create_unified_tools) ────────────────────

_REMOVED_TOOLS = {"get_doublette_tag_instruction"}
_CALIBRE_ONLY_TOOLS = {"detect_duplicates"}
# Tools that need a hash-diff scanner: Calibre or Zotero, not path-keyed
# sources. Kept apart from _CALIBRE_ONLY_TOOLS so the source enum each one
# advertises matches what it can actually serve.
_WATCHDOG_TOOLS = {"watchdog_scan"}
_PATH_INFERRED_TOOLS = {"get_book_annotations", "compute_annotation_hash"}
_AGGREGATION_TOOLS = {
    "search_books_with_citations",
    "search_annotations",
    "list_books_by_author",
    "list_tags",
    "list_annotated_books",
    "export_bibliography",
}
# Tools that take an optional `source` parameter (default = master default_source).
_SOURCE_OPTIONAL = _AGGREGATION_TOOLS | {"get_book_details", "set_research_interests"}


class UnifiedMCPServer:
    """Multi-source dispatcher for ARCHILLES MCP tools."""

    def __init__(
        self,
        servers: dict[str, CalibreMCPServer],
        default_source: Optional[str],
        master_dir: Path,
        instance_name: str = "archilles",
    ):
        if not servers:
            raise ValueError("UnifiedMCPServer requires at least one source")
        if default_source is not None and default_source not in servers:
            raise ValueError(
                f"default_source {default_source!r} not in {sorted(servers)}"
            )
        self.servers = servers
        if default_source is None and len(servers) == 1:
            default_source = next(iter(servers))
        self.default_source = default_source
        self.master_dir = master_dir
        self.instance_name = instance_name

    # ── Construction ─────────────────────────────────────────────────────

    @classmethod
    def from_master_config(cls, master: MasterConfig) -> "UnifiedMCPServer":
        """Build the unified server from a parsed :class:`MasterConfig`."""
        from src.adapters import create_adapter

        from src.citation.config import CitationConfig

        servers: dict[str, CalibreMCPServer] = {}
        for src in master.sources:
            cfg = resolve_source_config(master, src.name)
            try:
                adapter = create_adapter(cfg["library_path"], cfg["adapter"])
            except Exception as e:
                logger.error(
                    "Source %r: adapter construction failed (%s) — skipping",
                    src.name, e,
                )
                continue

            citation_config = CitationConfig.from_dict(cfg.get("citation") or {})
            servers[src.name] = CalibreMCPServer(
                library_path=str(cfg["library_path"]),
                annotations_dir=None,
                rag_db_path=cfg["rag_db_path"],
                enable_reranking=bool(cfg["enable_reranking"]),
                reranker_device=cfg["reranker_device"],
                citation_config=citation_config,
                adapter=adapter,
                instance_name=src.name,
            )
            logger.info(
                "Source %r ready: adapter=%s rag_db=%s",
                src.name, adapter.adapter_type, cfg["rag_db_path"],
            )

        if not servers:
            raise RuntimeError(
                "No source could be initialised — check master config and adapters"
            )

        default = master.default_source if master.default_source in servers else None
        return cls(
            servers=servers,
            default_source=default,
            master_dir=master_archilles_dir(),
        )

    def preload(self) -> None:
        """Warm every source's RAG stack.

        Sequential on purpose: embedding models are shared process-wide
        (see ``core._get_shared_embedding_model``), so the first source pays
        the model load and the others only attach + open their LanceDB.
        """
        for name, server in self.servers.items():
            logger.info("Preloading source %r ...", name)
            server.preload()

    # ── Source resolution ────────────────────────────────────────────────

    def resolve_source(self, source: Optional[str]) -> CalibreMCPServer:
        """Pick a server for a single-source tool call."""
        if source is None:
            if self.default_source is None:
                raise KeyError(
                    f"This tool requires an explicit 'source' parameter; "
                    f"available: {sorted(self.servers)}"
                )
            return self.servers[self.default_source]
        if source not in self.servers:
            raise KeyError(
                f"Unknown source {source!r}; available: {sorted(self.servers)}"
            )
        return self.servers[source]

    def resolve_source_for_path(self, book_path: str) -> Optional[str]:
        """Find which source a filesystem path belongs to.

        Longest-prefix match against each source's ``library_path``.
        Returns ``None`` if no source covers the path.
        """
        try:
            target = Path(book_path).resolve()
        except OSError:
            return None

        matches: list[tuple[int, str]] = []
        for name, server in self.servers.items():
            if server.library_path is None:
                continue
            try:
                root = server.library_path.resolve()
            except OSError:
                continue
            try:
                target.relative_to(root)
            except ValueError:
                continue
            matches.append((len(str(root)), name))

        if not matches:
            return None
        matches.sort(reverse=True)
        return matches[0][1]

    # ── Capability properties ────────────────────────────────────────────

    @property
    def calibre_sources(self) -> list[str]:
        """Names of sources whose adapter is Calibre."""
        return [
            name
            for name, srv in self.servers.items()
            if srv.adapter is not None and srv.adapter.adapter_type == "calibre"
        ]

    @property
    def watchdog_sources(self) -> list[str]:
        """Sources that have a hash-diff scanner: Calibre and Zotero.

        Both scanners have existed for months and run daily through the
        scheduled routines; only the MCP tool was Calibre-only, which left
        Zotero unscannable from a client. Path-keyed sources (folder,
        obsidian) have no hash-diff scanner yet and are not listed.
        """
        return [
            name
            for name, srv in self.servers.items()
            if srv.adapter is not None
            and srv.adapter.adapter_type in ("calibre", "zotero")
        ]

    @property
    def source_names(self) -> list[str]:
        """Sorted list of all source names, for tool-schema enumerations."""
        return sorted(self.servers)

    # ── Aggregation helper ───────────────────────────────────────────────

    def _parallel_call(self, method_name: str, **kwargs) -> dict[str, Any]:
        """Call ``method_name`` on every inner server in parallel.

        Returns ``{source_name: result}``. An exception in one source is
        captured as ``{'error': ...}`` for that source so a single failure
        cannot break the aggregation.
        """
        def _one(item):
            name, server = item
            try:
                method = getattr(server, method_name)
                return name, method(**kwargs)
            except Exception as e:
                logger.exception("Tool %s failed in source %r", method_name, name)
                return name, {"error": f"{method_name} failed: {e}"}

        if not self.servers:
            return {}
        with ThreadPoolExecutor(max_workers=len(self.servers)) as ex:
            return dict(ex.map(_one, self.servers.items()))

    # ── Search tools (score-merge) ───────────────────────────────────────

    def search_books_with_citations_tool(
        self,
        query: str,
        top_k: int = 10,
        mode: str = "hybrid",
        language: Optional[str] = None,
        tags: Optional[list[str]] = None,
        expand_context: bool = False,
        boost_research_interests: bool = True,
        max_per_book: int = 3,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """Multi-source search with central citation generation.

        With ``source`` set, delegates to a single inner server (per-source
        citations as today). Otherwise searches every source in parallel
        via ``service.search`` (no per-source citation block), merges
        results by score, and runs ONE ``create_claude_prompt`` over the
        merged top_k. Each result carries a ``source`` field so the LLM
        can disambiguate the citations.
        """
        if source is not None:
            srv = self.resolve_source(source)
            result = srv.search_books_with_citations_tool(
                query=query, top_k=top_k, mode=mode, language=language,
                tags=tags, expand_context=expand_context,
                boost_research_interests=boost_research_interests,
                max_per_book=max_per_book,
            )
            if isinstance(result, dict):
                result["source"] = source
            return result

        def _search_one(item):
            name, srv = item
            if not srv._ensure_rag_initialized():
                return name, []
            try:
                results = srv.service.search(
                    query=query,
                    mode=mode,
                    top_k=max(top_k * 2, top_k),  # over-fetch per source
                    language=language,
                    tag_filter=tags,
                    max_per_book=max_per_book,
                )
                if boost_research_interests and srv._archilles_dir:
                    from src.retriever.research_boost import (
                        apply_research_boost,
                        load_effective_research_interests,
                    )
                    kw, bf = load_effective_research_interests(srv._archilles_dir)
                    if kw:
                        results = apply_research_boost(results, kw, bf)
                for r in results:
                    r["source"] = name
                return name, results
            except Exception:
                logger.exception("search failed in source %r", name)
                return name, []

        with ThreadPoolExecutor(max_workers=len(self.servers)) as ex:
            per_source = list(ex.map(_search_one, self.servers.items()))

        per_source_counts: dict[str, int] = {
            name: len(results) for name, results in per_source
        }
        # Finding 5.4: rank-based merge — raw RRF scores from separate
        # LanceDB instances are not comparable across sources.
        merged = merge_source_results(per_source, top_k)

        if not merged:
            return {
                "query": query,
                "num_results": 0,
                "message": "No results found across any source",
                "per_source_counts": per_source_counts,
            }

        # Generate ONE citation block via the default-source's _rag instance.
        # The Citation-Builder formats result dicts to XML — it does not need
        # to live in the same library that produced the chunk.
        try:
            citation_srv = self.resolve_source(None)
        except KeyError:
            citation_srv = next(iter(self.servers.values()))
        if not citation_srv._ensure_rag_initialized():
            return {
                "query": query,
                "num_results": len(merged),
                "per_source_counts": per_source_counts,
                "raw_results": merged,
                "warning": "Citation generation skipped — RAG not initialised in default source",
            }

        claude_prompt = citation_srv.service.build_claude_prompt(
            results=merged,
            query=query,
            expand_context=expand_context,
            citation_config=citation_srv.citation_config,
        )

        response_extra = {}
        if not all(r.get("rerank_score") is not None for r in merged):
            # Without a cross-encoder the 'similarity' values are RRF
            # rank-fusion scores (~1/(60+rank); best possible hit ≈ 0.016).
            # Clients reading them as cosine similarities conclude every
            # result is noise.
            response_extra["score_note"] = (
                "Similarity values are RRF rank-fusion scores, not cosine "
                "similarities: the best possible hit scores ~0.016 (1/61). "
                "Judge relevance by rank and text, not by the absolute value."
            )

        return {
            "query": query,
            "num_results": len(merged),
            "search_mode": mode,
            "language_filter": language,
            "per_source_counts": per_source_counts,
            **response_extra,
            "system_prompt": claude_prompt["system"],
            "user_prompt": claude_prompt["user"],
            "usage_instructions": (
                "Copy 'system_prompt' to the system field, then 'user_prompt' to the message. "
                "Citations [doc_N] reference the documents — each carries its 'source' field."
            ),
            "raw_results": [
                {
                    "rank": r["rank"],
                    "source": r.get("source"),
                    "text_preview": r["text"][:200] + "..." if len(r.get("text", "")) > 200 else r.get("text", ""),
                    "similarity": r.get("rerank_score", r.get("score", 0.0)),
                    "metadata": {
                        "author": r.get("metadata", {}).get("author"),
                        "title": r.get("metadata", {}).get("book_title"),
                        "year": r.get("metadata", {}).get("year"),
                        "page": r.get("metadata", {}).get("page"),
                    },
                }
                for r in merged
            ],
        }

    def search_annotations_tool(
        self,
        query: str,
        max_results: int = 30,
        max_per_book: int = 5,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        if source is not None:
            srv = self.resolve_source(source)
            res = srv.search_annotations_tool(
                query=query, max_results=max_results, max_per_book=max_per_book,
            )
            if isinstance(res, dict):
                res["source"] = source
            return res

        per_source = self._parallel_call(
            "search_annotations_tool",
            query=query, max_results=max_results, max_per_book=max_per_book,
        )

        merged: list = []
        per_source_counts: dict[str, int] = {}
        for name, res in per_source.items():
            if not isinstance(res, dict) or "error" in res:
                per_source_counts[name] = 0
                continue
            results = res.get("results") or []
            for r in results:
                if isinstance(r, dict):
                    r["source"] = name
            merged.extend(results)
            per_source_counts[name] = len(results)

        merged.sort(
            key=lambda r: r.get("rerank_score", r.get("score", r.get("similarity", 0.0))) if isinstance(r, dict) else 0.0,
            reverse=True,
        )
        merged = merged[:max_results]

        return {
            "query": query,
            "search_type": "semantic",
            "result_count": len(merged),
            "per_source_counts": per_source_counts,
            "results": merged,
        }

    # ── List tools (concat) ──────────────────────────────────────────────

    def list_books_by_author_tool(
        self,
        author: str,
        tags: Optional[list[str]] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        sort_by: str = "title",
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        if source is not None:
            srv = self.resolve_source(source)
            res = srv.list_books_by_author_tool(
                author=author, tags=tags, year_from=year_from,
                year_to=year_to, sort_by=sort_by,
            )
            if isinstance(res, dict):
                res["source"] = source
            return res

        per_source = self._parallel_call(
            "list_books_by_author_tool",
            author=author, tags=tags, year_from=year_from,
            year_to=year_to, sort_by=sort_by,
        )

        all_books: list = []
        per_source_counts: dict[str, int] = {}
        for name, res in per_source.items():
            if not isinstance(res, dict) or "error" in res:
                per_source_counts[name] = 0
                continue
            books = res.get("books") or []
            for b in books:
                if isinstance(b, dict):
                    b["source"] = name
            all_books.extend(books)
            per_source_counts[name] = len(books)

        if sort_by == "year":
            all_books.sort(key=lambda x: _year_sort_key(x.get("year")), reverse=True)
        else:
            all_books.sort(key=lambda x: (x.get("title") or "").lower())

        return {
            "books": all_books,
            "count": len(all_books),
            "author_query": author,
            "per_source_counts": per_source_counts,
        }

    def list_tags_tool(
        self,
        min_books: int = 1,
        max_tags: int = 100,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        if source is not None:
            srv = self.resolve_source(source)
            res = srv.list_tags_tool(min_books=min_books, max_tags=max_tags)
            if isinstance(res, dict):
                res["source"] = source
            return res

        per_source = self._parallel_call(
            "list_tags_tool", min_books=min_books, max_tags=max_tags,
        )

        all_tags: list = []
        per_source_counts: dict[str, int] = {}
        for name, res in per_source.items():
            if not isinstance(res, dict) or "error" in res:
                per_source_counts[name] = 0
                continue
            tags = res.get("tags") or []
            for t in tags:
                if isinstance(t, dict):
                    t["source"] = name
            all_tags.extend(tags)
            per_source_counts[name] = len(tags)

        all_tags.sort(key=lambda t: -t.get("book_count", 0))
        limited = all_tags[:max_tags]

        return {
            "returned_tags": len(limited),
            "tags": limited,
            "per_source_counts": per_source_counts,
            "usage": (
                'Use these tag names in the "tags" parameter; '
                'pass "source" to scope to one library.'
            ),
        }

    def list_annotated_books_tool(
        self,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        if source is not None:
            srv = self.resolve_source(source)
            res = srv.list_annotated_books_tool()
            if isinstance(res, dict):
                res["source"] = source
            return res

        per_source = self._parallel_call("list_annotated_books_tool")
        all_books: list = []
        per_source_counts: dict[str, int] = {}
        for name, res in per_source.items():
            if not isinstance(res, dict) or "error" in res:
                per_source_counts[name] = 0
                continue
            books = res.get("books") or []
            for b in books:
                if isinstance(b, dict):
                    b["source"] = name
            all_books.extend(books)
            per_source_counts[name] = len(books)

        return {
            "total_books": len(all_books),
            "books": all_books,
            "per_source_counts": per_source_counts,
        }

    def export_bibliography_tool(
        self,
        format: str = "bibtex",
        author: Optional[str] = None,
        tag: Optional[str] = None,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        max_books: Optional[int] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        if source is not None:
            srv = self.resolve_source(source)
            res = srv.export_bibliography_tool(
                format=format, author=author, tag=tag,
                year_from=year_from, year_to=year_to, max_books=max_books,
            )
            if isinstance(res, dict):
                res["source"] = source
            return res

        per_source = self._parallel_call(
            "export_bibliography_tool",
            format=format, author=author, tag=tag,
            year_from=year_from, year_to=year_to, max_books=max_books,
        )

        chunks: list[str] = []
        per_source_counts: dict[str, int] = {}
        for name, res in per_source.items():
            if not isinstance(res, dict) or "error" in res:
                per_source_counts[name] = 0
                continue
            text = res.get("data") or ""
            count = res.get("book_count", 0)
            per_source_counts[name] = count
            if text:
                chunks.append(f"% --- Source: {name} ({count} entries) ---\n{text}")

        return {
            "format": format,
            "data": "\n\n".join(chunks),
            "book_count": sum(per_source_counts.values()),
            "per_source_counts": per_source_counts,
        }

    # ── Single-source tools (path-inferred) ──────────────────────────────

    def get_book_annotations_tool(self, book_path: str, **kwargs) -> dict[str, Any]:
        source = self.resolve_source_for_path(book_path)
        if source is None:
            return {
                "error": f"No source covers {book_path!r}",
                "available_sources": self.source_names,
            }
        srv = self.servers[source]
        res = srv.get_book_annotations_tool(book_path=book_path, **kwargs)
        if isinstance(res, dict):
            res["source"] = source
        return res

    def compute_hash_tool(self, book_path: str) -> dict[str, Any]:
        source = self.resolve_source_for_path(book_path)
        if source is None:
            return {
                "error": f"No source covers {book_path!r}",
                "available_sources": self.source_names,
            }
        srv = self.servers[source]
        res = srv.compute_hash_tool(book_path=book_path)
        if isinstance(res, dict):
            res["source"] = source
        return res

    # ── Single-source tool with `source` parameter ───────────────────────

    def get_book_details_tool(
        self,
        book_id: int,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            srv = self.resolve_source(source)
        except KeyError as e:
            return {"error": str(e), "available_sources": self.source_names}
        res = srv.get_book_details_tool(book_id=book_id)
        if isinstance(res, dict):
            res["source"] = source if source is not None else self.default_source
        return res

    # ── Global state ─────────────────────────────────────────────────────

    def set_research_interests_tool(
        self,
        keywords: Optional[list[str]] = None,
        boost_factor: float = 0.15,
        action: str = "set",
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """View or update research interests with master/source split.

        ``source=None`` operates on the master file
        (``master_dir/research_interests.json``); ``source='X'`` operates
        on the source's library-local file. ``action='get'`` with a source
        returns the EFFECTIVE merge so the user can see what actually
        applies to that source's searches.
        """
        from src.retriever.research_boost import (
            load_effective_research_interests,
            load_research_interests,
            save_research_interests,
        )

        if source is None:
            target_dir: Path = self.master_dir
            scope = "master"
        else:
            try:
                srv = self.resolve_source(source)
            except KeyError as e:
                return {"error": str(e), "available_sources": self.source_names}
            if srv._archilles_dir is None:
                return {"error": f"Source {source!r} has no .archilles directory"}
            target_dir = srv._archilles_dir
            scope = source

        try:
            if action == "get":
                if source is None:
                    kw, bf = load_research_interests(target_dir)
                    return {
                        "action": "get",
                        "scope": scope,
                        "keywords": kw,
                        "boost_factor": bf,
                        "keyword_count": len(kw),
                        "file": str(target_dir / "research_interests.json"),
                    }
                kw, bf = load_effective_research_interests(target_dir)
                return {
                    "action": "get",
                    "scope": scope,
                    "keywords": kw,
                    "boost_factor": bf,
                    "keyword_count": len(kw),
                    "effective": True,
                    "note": "Effective merge (master + library override) for this source.",
                }

            if action == "set":
                if keywords is None:
                    return {"error": 'keywords parameter required for action="set"'}
                save_research_interests(target_dir, keywords, boost_factor)
                return {
                    "action": "set",
                    "scope": scope,
                    "keywords": keywords,
                    "boost_factor": boost_factor,
                    "keyword_count": len(keywords),
                    "file": str(target_dir / "research_interests.json"),
                    "message": (
                        f"Saved {len(keywords)} keywords to {scope!r}. "
                        "Boost will apply to future searches."
                    ),
                }

            return {"error": f'Unknown action: {action!r}. Use "get" or "set".'}
        except Exception as e:
            logger.error("set_research_interests failed: %s", e, exc_info=True)
            return {"error": str(e)}

    # ── Calibre-only tools ───────────────────────────────────────────────

    def detect_duplicates_tool(
        self,
        source: str,
        method: str = "title_author",
        include_doublette_tag: bool = True,
    ) -> dict[str, Any]:
        try:
            srv = self.resolve_source(source)
        except KeyError as e:
            return {"error": str(e), "available_sources": self.source_names}
        if source not in self.calibre_sources:
            atype = srv.adapter.adapter_type if srv.adapter else "non-Calibre"
            return {
                "error": (
                    f"detect_duplicates is Calibre-only; {source!r} is a "
                    f"{atype} source"
                ),
                "calibre_sources": self.calibre_sources,
            }
        res = srv.detect_duplicates_tool(
            method=method, include_doublette_tag=include_doublette_tag,
        )
        if isinstance(res, dict):
            res["source"] = source
        return res

    def watchdog_scan_tool(
        self,
        source: str,
        dry_run: bool = False,
        queue_new: bool = True,
        index_new: bool = False,
        max_new: int | None = None,
    ) -> dict[str, Any]:
        try:
            srv = self.resolve_source(source)
        except KeyError as e:
            return {"error": str(e), "available_sources": self.source_names}
        if source not in self.watchdog_sources:
            atype = srv.adapter.adapter_type if srv.adapter else "unknown"
            return {
                "error": (
                    f"watchdog_scan supports Calibre and Zotero sources; "
                    f"{source!r} is a {atype} source"
                ),
                "watchdog_sources": self.watchdog_sources,
            }
        res = srv.watchdog_scan_tool(
            dry_run=dry_run, queue_new=queue_new, index_new=index_new,
            max_new=max_new,
        )
        if isinstance(res, dict):
            res["source"] = source
        return res


# ── Tool-schema generator ────────────────────────────────────────────────

def create_unified_tools(server: UnifiedMCPServer) -> list[dict]:
    """Build tool schemas for the unified server.

    Reuses the schemas from :func:`create_mcp_tools` so any change to the
    single-server tool list is picked up automatically. Cross-source
    machinery is applied here:

    - ``get_doublette_tag_instruction``: removed.
    - Calibre-only tools: gated behind ``calibre_sources``; if registered,
      ``source`` becomes a required enum of Calibre sources.
    - Path-inferred tools: untouched (source comes from ``book_path``).
    - Aggregation and other source-optional tools: gain an optional
      ``source`` enum across all sources, with semantics described in the
      schema's description string.
    """
    seed = next(iter(server.servers.values()))
    base_tools = create_mcp_tools(seed)
    source_names = server.source_names
    have_calibre = bool(server.calibre_sources)
    watchdog_sources = server.watchdog_sources

    out: list[dict] = []
    for tool in base_tools:
        name = tool["name"]
        if name in _REMOVED_TOOLS:
            continue
        if name in _CALIBRE_ONLY_TOOLS and not have_calibre:
            continue
        if name in _WATCHDOG_TOOLS and not watchdog_sources:
            continue

        if name in _CALIBRE_ONLY_TOOLS or name in _WATCHDOG_TOOLS:
            gated_sources = (
                watchdog_sources if name in _WATCHDOG_TOOLS
                else server.calibre_sources
            )
            what = (
                "Calibre or Zotero source name (required — these are the "
                "sources with a change scanner)."
                if name in _WATCHDOG_TOOLS
                else "Calibre source name (required — this tool is Calibre-specific)."
            )
            schema = dict(tool["inputSchema"])
            props = dict(schema.get("properties") or {})
            props["source"] = {
                "type": "string",
                "enum": gated_sources,
                "description": what,
            }
            schema["properties"] = props
            req_list = list(schema.get("required") or [])
            if "source" not in req_list:
                req_list.append("source")
            schema["required"] = req_list
            out.append(dict(tool, inputSchema=schema))
            continue

        if name in _PATH_INFERRED_TOOLS:
            out.append(tool)
            continue

        if name in _SOURCE_OPTIONAL:
            schema = dict(tool["inputSchema"])
            props = dict(schema.get("properties") or {})
            if name in _AGGREGATION_TOOLS:
                description = (
                    f"Optional: limit to one source. Available: {source_names}. "
                    "Default = aggregate across all sources."
                )
            elif name == "set_research_interests":
                # Special case: source=None routes to the master file at
                # master_dir/research_interests.json (applies as a baseline
                # to every source via the effective merge), NOT to the
                # default source's library — see set_research_interests_tool.
                description = (
                    f"Source name to scope research interests to one library. "
                    f"Available: {source_names}. "
                    "Omit to read or write the master research-interests file "
                    "at <master_dir>/research_interests.json, which applies as "
                    "a baseline to every source's searches via the effective "
                    "merge layer."
                )
            else:
                default = server.default_source
                description = (
                    f"Optional source override. Available: {source_names}. "
                    f"Default: {default!r}."
                )
            props["source"] = {
                "type": "string",
                "enum": source_names,
                "description": description,
            }
            schema["properties"] = props
            out.append(dict(tool, inputSchema=schema))
            continue

        out.append(tool)

    return out
