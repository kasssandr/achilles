# ARCHILLES – Architecture

**Last updated:** September 2026 (Scriptor seam: `ScriptorExtractor`, bundle text source, provenance columns; the modular pipeline described as what it is since July — experimental)

This document describes *how* ARCHILLES is built. For *why* these choices were made, see [DECISIONS.md](DECISIONS.md).

* * *

## System Overview

ARCHILLES is a local-first RAG (Retrieval-Augmented Generation) system that transforms Calibre e-book libraries into semantically searchable knowledge bases. It runs entirely on the user's machine—no cloud services, no telemetry, no data leaving the device unless the user explicitly connects to an external LLM.

The system is built around three principles: privacy by architecture (not by policy), academic-grade citations with verifiable source references, and modular extensibility through formal registries that anticipate future components without over-engineering the present.

```
                        ┌──────────────────────────┐
                        │      Calibre Library      │
                        │  (metadata.db + books +   │
                        │   annotations + comments) │
                        └────────────┬─────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │    Calibre DB Layer  │
                          │   (read-only access) │
                          └──────────┬──────────┘
                                     │
                   ┌─────────────────▼─────────────────┐
                   │          Extractors Layer         │
                   │  ┌────────┐ ┌────────┐ ┌────────┐ │
                   │  │  PDF   │ │  EPUB  │ │Scriptor│ │
                   │  │PyMuPDF │ │ebooklib│ │ bundle │ │
                   │  │+OCR (T)│ │        │ │(master)│ │
                   │  └───┬────┘ └───┬────┘ └───┬────┘ │
                   └──────┼──────────┼──────────┼──────┘
                          │          │          │
                   ┌──────▼──────────▼──────────▼──────┐
                   │   Indexer (src/archilles/engine)  │
                   │ extractor chunks → BGE-M3 vectors │
                   │  (ExecutionPlan: mode, hierarchy) │
                   └─────────────────┬─────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │         Storage Layer            │
                    │  ┌──────────────────────────┐   │
                    │  │  LanceDB ("chunks" table) │   │
                    │  │  book content, annotations│   │
                    │  │  calibre comments         │   │
                    │  │  BGE-M3 embeddings        │   │
                    │  │  (chunk_type field filters│   │
                    │  │   content vs annotations) │   │
                    │  └──────────────────────────┘   │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │         Retriever Layer          │
                    │                                  │
                    │  Stage 1: LanceDB Hybrid Search  │
                    │    (dense vectors + BM25 FTS)    │
                    │              │                    │
                    │  Stage 2: Cross-Encoder Reranker │
                    │    (optional, BAAI bge-reranker)  │
                    └────────────────┬────────────────┘
                                     │
                    ┌────────────────▼────────────────┐
                    │        ArchillesService          │
                    │  (central business logic facade) │
                    └──┬─────────────┬─────────────┬──┘
                       │             │             │
                  ┌────▼────┐  ┌────▼────┐  ┌────▼────┐
                  │   MCP   │  │ Web UI  │  │   CLI   │
                  │ Server  │  │Streamlit│  │rag_demo │
                  └─────────┘  └─────────┘  └─────────┘
                                                 ▲
                                                 │
                          ┌─────────────────────┴─────────────────────┐
                          │     Scheduled Routines Layer               │
                          │  run_routine.py  →  per-source wrapper     │
                          │  run_link_vault.py  →  gated vault linker  │
                          │  weekly_status_mail.py  →  Gmail digest    │
                          │  (Windows Task Scheduler, OnLogon trigger) │
                          └────────────────────────────────────────────┘
```

* * *

## Core Components

### 1\. Source Adapter Layer (`src/adapters/`)

ARCHILLES supports multiple library backends through a common adapter interface. Each adapter implements `list_books()`, `get_metadata()`, and `resolve_file()`. The batch indexer auto-detects the appropriate adapter from the library structure.

| Adapter | Backend | Detection |
| --- | --- | --- |
| `CalibreAdapter` | Calibre library (`metadata.db`) | Default, detects `metadata.db` |
| `ZoteroAdapter` | Zotero library (`zotero.sqlite`) | Detects `zotero.sqlite` |
| `ObsidianAdapter` | Obsidian vault (Markdown + YAML frontmatter) | Detects `.obsidian/` directory |
| `FolderAdapter` | Plain directory of files | Fallback when no specific backend detected |

The adapter layer sits above the Calibre DB layer and generalizes library access. For Calibre libraries, the `CalibreAdapter` delegates to `calibre_db.py`.

### 1b\. Calibre Database Layer (`src/calibre_db.py`)

Read-only interface to Calibre's `metadata.db`. This component never modifies the database—a firm architectural boundary documented in DECISIONS.md (ADR-005).

**What it extracts:**

Per book: title, author(s), publisher, publication date, ISBN/identifiers, language, series information, file formats and paths, cover path, and reading progress. Tags and custom columns are extracted generically—the system discovers and indexes any user-defined custom fields without manual configuration.

The comments field deserves special attention. In many researchers' Calibre libraries, comments contain far more than publisher blurbs: NotebookLM deep-dive analyses, personal reading notes, collected reviews, and structured research summaries. ARCHILLES treats this as a first-class data source, routing it to the separate annotations database (see Storage Architecture below).

**Custom field discovery** works by querying Calibre's `custom_columns` table at startup and mapping each field to the appropriate search metadata. This means a user who has created fields like `research_project`, `source_reliability`, or `century` gets those fields indexed automatically without touching any configuration.

### 2\. Extractors Layer (`src/extractors/`)

Each extractor is responsible for turning a book file into structured text with metadata. The extractors are modular and independently deployable, coordinated by a `UniversalExtractor` that delegates to the appropriate format-specific extractor. A `FormatDetector` identifies file types, and a `LanguageDetector` (Lingua, 75+ languages) assigns language codes to extracted text.

**Available extractors:**

| Extractor | File | Formats | Notes |
| --- | --- | --- | --- |
| `PDFExtractor` | `pdf_extractor.py` | PDF | Primary: PyMuPDF (fitz); fallback: pdfplumber; OCR path |
| `EPUBExtractor` | `epub_extractor.py` | EPUB 2/3 | ebooklib with custom TOC parser |
| `ScriptorExtractor` | `scriptor_extractor.py` | Scriptor master (`.md` whose metadata block names a `format_version`) | Reads a bundle from `archilles-scriptor`: printed page labels, regions, bound footnotes; see below |
| `TXTExtractor` | `txt_extractor.py` | TXT | Plain text with encoding detection |
| `HTMLExtractor` | `html_extractor.py` | HTML | HTML documents |
| `OCRExtractor` | `ocr_extractor.py` | Scanned PDFs | Tesseract baseline; VLM upgrade path prepared |
| `CalibreConverter` | `calibre_converter.py` | MOBI, DJVU, etc. | Delegates to Calibre's `ebook-convert` |

Common infrastructure: `base.py` (base class), `models.py` (dataclasses), `exceptions.py` (error types).

#### PDF Extractor (`pdf_extractor.py`)

Primary extractor for PDF files, built on PyMuPDF (fitz). PDF is the preferred format because it provides reliable page number mapping—essential for citable references. Where a book has a Scriptor bundle, the bundle is read instead (see Scriptor Extractor below); for all other PDFs this extractor remains the path, frozen as the fallback rather than developed further (ADR-032).

Key capabilities: CropBox filtering eliminates headers, footers, and running page numbers before the text reaches the chunker, which significantly improves embedding quality. Page label extraction maps printed page numbers (which may differ from internal PDF page indices) to chunks, enabling citations like "p. 47" that match what a reader sees in the physical or digital book. Footer detection with footnote disambiguation ensures that footnotes are preserved while page-bottom noise is removed.

**TOC-aware chunking** (March 2026): `_build_page_toc_map(toc)` maps the PDF's table of contents to page ranges. Level-1 entries become `chapter`, level-2+ entries become `section_title`. Junk TOCs from scanner PDFs are automatically detected and ignored (pattern matching for artifacts like "scan 1", threshold checks for entries count and page diversity). `_section_type_from_toc_title()` derives `section_type` from TOC titles with comprehensive DE/EN keyword matching, falling back to the existing position heuristic when no TOC is available.

**Running footer removal**: `_detect_running_footers()` mirrors the existing header detection but targets the last 3 lines per page. Uses a higher occurrence threshold (5% of pages, minimum 10) to avoid false positives with variable content lines.

The multi-tier fallback system activates pdfplumber when PyMuPDF's extraction quality falls below threshold (measured by character density and encoding coherence). For scanned PDFs without embedded text, the system falls through to the OCR path.

#### EPUB Extractor (`epub_extractor.py`)

Built on ebooklib with custom TOC parsing. EPUBs provide richer structural information than PDFs: the table of contents maps directly to chapter boundaries, and the internal HTML structure enables section-level metadata extraction.

Section metadata classification automatically labels content as Front Matter (foreword, table of contents), Main Content (the actual text, including introductions), or Back Matter (bibliography, index, appendices). A readable title (H1 text or TOC entry title) decides. Only an item without one falls back to its filename (`notes.xhtml`, `copyright.html`), and only in books whose filenames distinguish anything: a book converted into `index_split_000.html` … `index_split_412.html` names the converter, not the section, and the signal is discarded for it. Introduction/Einleitung is deliberately classified as main_content, not front_matter. This classification powers the `section_filter` parameter in search—by default set to `'main'`, which excludes bibliography and index noise from results.

#### Scriptor Extractor (`scriptor_extractor.py`)

The seam to [archilles-scriptor](https://github.com/kasssandr/archilles-scriptor), which turns printed books in PDF form into a *prepared document* (`PREPARED_FORMAT_SPEC.md`): a Markdown master carrying printed page markers (`[p. 88]`), region markers (`[region: bibliography]`) and bound footnotes, plus sidecars. Background and the choices behind it: ADR-032.

- **The bundle is a text source, not a format.** It lives in `<library>/.archilles/scriptor/<key>/` (`key` = the book ID, numeric IDs zero-padded to five digits). `Indexer._text_source` swaps only where the text comes from, in `index_book` and `prepare_book`. Every caller still passes the book file, which stays the book's identity: Calibre metadata, viewer annotations (keyed by the file's path), export links and `source_file` all refer to it. `src/archilles/book_files.py` finds the master: exactly one `*.md` that is not `*.review.md` and names a `format_version`; two candidates are refused, not chosen.
- **Reading.** The grammar is Scriptor's, imported from `scriptor.document` (`archilles-scriptor` is a hard runtime dependency, one direction only). What is Archilles' own is the mapping onto the chunk schema:
  - The region becomes `section_type`: front matter and contents → `front_matter`; apparatus (`bibliography`, `index`, `abbreviations`, `notes`, `appendix`) → `back_matter`; everything else, including `preface` and any unknown name → `main_content`. The region name itself goes to `region`.
  - The pagination sidecar's `pages[].pos` gives `page_number`, its `source` gives `label_source`.
  - `#` headings give `chapter`, deeper ones `section_title`.
  - `format_version` goes to `producer_version`. A master whose spec major differs from `SUPPORTED_SPEC_MAJOR` is refused, never indexed on a guess.
- **Composition.** Chunks follow the master's `chunking_strategy`: `scientific` appends each footnote definition to the paragraph carrying its anchor, `basic` leaves definitions out. Markers are removed from the chunk text after they have been read. A chunk never spans two regions or sections, and overlap is by whole sentences.
- **Producing bundles.** Bundles are built by `scripts/scriptor_prepare.py`, never by the watchdog. It runs Scriptor in-process, judges each volume by four admission conditions (`src/archilles/scriptor_build.py`: text coverage, notes against the audit, attested page labels, text under an inherited page number), moves a bundle into place only after it passes, and indexes it. A rejected run lands in `scriptor/_rejected/<key>/` and leaves the existing bundle and index untouched. `scriptor/REPORT.md` and `state.json` show each volume's values and whether its master is prepared, hand-edited, or released to the index. The library's `scriptor` config block sets the chunking strategy (default `scientific`).

#### OCR Extractor (`ocr_extractor.py`)

Tesseract serves as the baseline for modern printed text. The interface is designed for drop-in replacement with VLM-based OCR systems (LightOnOCR-2, GOT-OCR 2.0) as they mature. The `ArchillesService` exposes `ocr_backend` configuration (auto/tesseract/lighton/olmocr) to select backends. This path is due to be replaced: scans are to go through Scriptor's OCR backend onto its page model (boxes, sizes, confidences), so they get printed page labels instead of physical numbers (see ROADMAP v1.2). The VLM placeholders go with that step.

### 3\. Indexing Engine (`src/archilles/engine/`) and the experimental Modular Pipeline

#### What runs in production

`ArchillesRAG` (`engine/core.py`) composes `Indexer`, `Searcher` and `PromptBuilder`.
- `Indexer.index_book` reads the book's text through `UniversalExtractor`, from its Scriptor bundle where one exists (`_text_source`).
- The format extractor chunks the text itself (`BaseExtractor`): paragraphs gathered up to `chunk_size`, oversized paragraphs split at sentence ends, `window_text` for Small-to-Big, language detection per chunk. Chunks are optionally grouped into parents and children (`--hierarchical`, or `mode` through the `ExecutionPlan`, ADR-028).
- `_build_chunk_dicts` carries the chunk metadata into the row, BGE-M3 embeds it, and `LanceDBStore.add_chunks` writes it. Nothing is deleted before its replacement has been extracted and embedded.
- `prepare_book` does the same without embedding and writes `prepared_chunks/*.jsonl`, which `embed_prepared` embeds locally or on a remote GPU (ADR-015).

A new input format therefore belongs in `src/extractors/`, next to `UniversalExtractor`. That is where the Scriptor import was built (ADR-032).

#### Modular Pipeline (experimental — not the production path)

`pipeline.py`, `parsers/` and `chunkers/` implement an alternative Parser → Chunker → Embedder pipeline. Since 4 July 2026 it is marked experimental and runs only behind `--use-modular-pipeline`, which no routine sets. It is not integrated with the `ExecutionPlan`, hierarchical chunking and language detection; it loads a second embedder next to the one already loaded; and it skips `metadata_hash` and annotation chunks, so the watchdog cannot see its books change. Whether it is removed is an open decision (ADR-004, September addendum). `embedders/` and `registry.py` are not part of that question: both are in production (external embedding, annotation embedding, `AnnotationProviderRegistry`).

For reference, `ModularPipeline` chains three stages:

1.  **Parsers** (`parsers/`): Convert files into `ParsedDocument` objects with structural metadata. `PyMuPDFParser` and `EPUBParser` are registered in `ParserRegistry` — parser selection is a genuine dispatch by file format, so it goes through a formal registry.
    
2.  **Chunkers** (`chunkers/`): Split parsed text into `TextChunk` objects. Three chunkers are available; selection happens directly by frontmatter strategy (`pipeline._select_chunker`), **not** via a registry:
    - `FixedSizeChunker`: Simple token/character-based splitting
    - `SemanticChunker`: Sentence-boundary-aware, with Markdown heading detection (H1/H2 force chunk breaks) and oversized paragraph splitting
    - `DialogueChunker`: Specialized for chat/Q&A exports (ChatGPT, Gemini, Grok, NotebookLM). Recognizes turn markers (`## User`, `**Q:**`, etc.) and chunks per turn or turn-pair, preserving `speaker` metadata.
    Configuration via `ChunkerConfig` (chunk_size, chunk_overlap, size_unit, respect_sentences, respect_paragraphs).
    
3.  **Embedders** (`embedders/`): Generate vector representations. `BGEEmbedder` supports bge-small (384 dim), bge-base (768 dim), and bge-m3 (1024 dim, multilingual). Embedder selection happens directly by hardware profile (`pipeline._create_embedder_from_profile`), **not** via a registry.
    

**On registries:** ARCHILLES uses formal registries only where component selection is a real runtime dispatch. Two exist, both on the generic `BaseRegistry[T]` (`src/archilles/registry.py`): `ParserRegistry` (by file format, on the experimental path) and `AnnotationProviderRegistry` (annotation sources, in production). Chunkers and embedders are *not* registry-based — their openness comes from the `TextChunker`/`TextEmbedder` ABCs, where a new variant is a single class. The earlier `ChunkerRegistry`/`EmbedderRegistry` were removed in the June 2026 code-review hardening (P2-Etappe 5) as dead infrastructure that merely simulated extensibility. See DECISIONS.md (ADR-004).

#### Hardware Profiles (`profiles.py`)

Since ADR-028 the user-facing switch is `mode` in `.archilles/config.json` (`auto | light | full-local | full-external`), resolved together with the detected hardware into an `ExecutionPlan`. The three profiles below remain as a legacy override (`--profile`). All use BGE-M3 for consistent embedding quality—only batch size and speed differ:

| Profile | GPU VRAM | Batch Size | Speed | Use Case |
| --- | --- | --- | --- | --- |
| `minimal` | 4–6 GB | 8   | ~2 min/book | Quadro T1000, GTX 1650 |
| `balanced` | 8–12 GB | 32  | ~30s/book | RTX 3060, RTX 2070 |
| `maximal` | 16+ GB | 64  | ~15s/book | RTX 3090, RTX 4080 |

Hardware detection (`hardware.py`) auto-selects the appropriate profile when none is specified.

#### Indexing Checkpoints (`indexer/checkpoint.py`)

`IndexingCheckpoint` provides checkpoint-based resume for long-running batch operations, tracking per-book progress (chunks processed, errors, timing) with JSON persistence.

### 4\. Storage Layer (`src/storage/`)

#### LanceDB Store (`lancedb_store.py`)

The central storage backend for book content. `LanceDBStore` provides:

- **Hybrid search**: Native vector + BM25 full-text search with RRF (Reciprocal Rank Fusion) reranking via `lancedb.rerankers.RRFReranker`
- **Vector search**: Pure semantic similarity
- **Full-text search**: BM25 keyword matching for exact terms
- **SQL-like filtering**: By book_id, calibre_id, section_type, chunk_type, language
- **Schema migration**: Automatic addition of new columns to existing tables (page_label, window_text, parent_id, etc.)
- **IVF-PQ indexing**: For corpora exceeding 256 chunks, with configurable partitions

**Database schema** (LanceDB table "chunks"):

| Column | Type | Purpose |
| --- | --- | --- |
| `id` | str | Unique chunk identifier |
| `text` | str | Chunk text content |
| `vector` | float\[1024\] | BGE-M3 embedding |
| `book_id`, `book_title`, `author`, `publisher`, `year` | —   | Book metadata |
| `calibre_id`, `source_id` | int, str | Calibre internal book ID; adapter-agnostic document ID |
| `tags`, `language` | str | Calibre tags, detected language |
| `chunk_index`, `chunk_type` | —   | Position and type (content/parent/child/calibre_comment/annotation) |
| `page_number`, `page_label` | —   | Physical page + printed label ("xiv", "62") |
| `chapter`, `section`, `section_title`, `section_type` | —   | Structural metadata |
| `char_start`, `char_end`, `window_text` | —   | Context expansion (Small-to-Big) |
| `parent_id` | str | Parent chunk reference (hierarchical chunking) |
| `annotation_type`, `annotation_source`, `annotation_hash` | str | Annotation rows; hash for change detection |
| `source_file`, `format`, `indexed_at` | —   | Technical metadata. `source_file` is the book file, even where the text came from a Scriptor bundle or a Calibre conversion (rows written before September 2026 may still name the conversion's temporary file); `format` is what the text was (`scriptor` for a bundle) |
| `metadata_hash` | str | Calibre metadata hash for change detection (watchdog) |
| `pipeline_version` | str | Generation of the pipeline that wrote the row (`''` = before the marker existed) |
| `region`, `label_source`, `producer_version` | str | Scriptor provenance: region name beside `section_type`, the witness behind `page_label`, the prepared-format version of the master; `''` for every other row |
| `pending_external` | int | 1 = provisionally light, waiting for an external hierarchical re-embed (ADR-028) |

The `add_processed_documents()` method bridges `ProcessedDocument` objects from the modular pipeline directly into LanceDB storage.

#### Annotations Storage (LanceDB, unified)

Annotations (highlights, notes from Calibre's E-book Viewer and PDF readers) are stored directly in the main LanceDB `chunks` table using `chunk_type='annotation'`. Calibre book comments use `chunk_type='calibre_comment'`. Both use the same BGE-M3 embeddings (1024 dimensions) as book content chunks, enabling cross-type hybrid search without model incompatibility.

The `search_annotations` MCP tool queries both types via the `'annotations_and_comments'` composite filter in `LanceDBStore._build_filter()`. Annotation indexing happens automatically as Phase 2 of the regular `index_book()` pipeline — no separate indexing step required.

**Annotation infrastructure:**
- `src/calibre_mcp/annotations.py` — File-based annotation extraction, hash-to-book mapping (100+ path variants), text/keyword search (text-only fallback when LanceDB unavailable)
- `src/calibre_mcp/calibre_analyzer.py` — Calibre metadata analysis and statistics


### 5\. Retriever Layer (`src/retriever/`)

#### Search Logic

The hybrid search logic is implemented across two layers:

- **`LanceDBStore.hybrid_search()`** handles the database-level search: vector similarity + BM25 full-text search with RRF fusion, section/chunk/language filtering
- **`ArchillesRAG.query()`** in `src/archilles/engine/` (delegating to `Searcher` in `search.py`) orchestrates the high-level search: mode selection (semantic/keyword/hybrid), exact phrase matching, tag filtering, result diversification (max_per_book), minimum similarity thresholds, and context expansion

There is no separate `hybrid.py` file—the retrieval logic is distributed between the storage layer and the RAG class, unified through the service layer.

**Section filtering** applies at query time: `section_filter='main'` (the default) restricts results to main body content, excluding front matter, back matter, bibliography, and index sections. Users can override this for specific searches where those sections are relevant.

**Smart boosting** applies configurable multipliers: Calibre comments matches receive a 1.2x boost, tag matches 1.15x. These weights are tunable in configuration.

#### Cross-Encoder Reranking (`reranker.py`, optional)

When enabled, the top candidates from Stage 1 are re-scored by a cross-encoder model (BAAI/bge-reranker-v2-m3, configurable) that evaluates each query-document pair jointly rather than independently. This produces significantly more accurate relevance ranking at the cost of additional inference time. The reranker runs on CPU by default (since GPU is typically occupied by BGE-M3 embeddings) and falls back gracefully if system memory is insufficient. First use downloads the model (~560 MB).

Configuration via `.archilles/config.json`:

```json
{
  "enable_reranking": true,
  "reranker_device": "cpu"
}
```

### 6\. Service Layer (`src/service/archilles_service.py`)

Introduced in February 2026 to solve a growing consistency problem: the Web UI, MCP server, and CLI all imported the RAG class directly, meaning every change to search logic had to be replicated in three places.

`ArchillesService` is the central facade that all consumers use. It exposes:

- `search()` — with mode, filters, reranking, diversification, similarity thresholds
- `search_with_citations()` — generates XML-structured prompts with citation metadata
- `get_index_status()` — database statistics (chunks, books, formats, languages)
- `get_book_list()` — all indexed books with chunk counts
- `get_chunk_by_id()` — single chunk retrieval (for parent-child lookup)

The service handles lazy initialization (RAG loading deferred to first use), stdout redirection (critical for MCP JSON-RPC safety), and cross-encoder reranking orchestration. When reranking is enabled, the service fetches more raw results from the RAG layer, applies the cross-encoder, then diversifies.

### 7\. Client Interfaces

#### MCP Server (`src/calibre_mcp/server.py`, entry point: `mcp_server.py`)

The primary interface. Implements the Model Context Protocol for integration with Claude Desktop and other MCP-compatible AI assistants. The entry point `mcp_server.py` supports two transports:

- **stdio** (default): JSON-RPC 2.0 over stdin/stdout. Used by Claude Desktop, Gemini CLI, and any client that spawns the server as a subprocess. stdout is reserved exclusively for JSON-RPC; all logging goes to stderr and `~/.archilles/mcp_server.log`.
- **SSE** (`--transport sse`): HTTP/Server-Sent Events via Starlette/Uvicorn, binding to `127.0.0.1` only. Used by ChatGPT Desktop, OpenAI Codex, Cursor, and other clients that connect by URL. Activate with `--transport sse [--host 127.0.0.1] [--port 8765]` or via the `transport` block in `.archilles/config.json`. An optional Bearer token (`transport.auth_token`) restricts access. Two instances can run in parallel on different ports.

Both transports expose the same tools via the same `CalibreMCPServer` and `create_mcp_tools()`. The SSE path uses `mcp.server.Server` (low-level MCP SDK) with `mcp.server.sse.SseServerTransport`; tool dispatch runs in a thread pool (`run_in_executor`) because `ArchillesService` calls are synchronous.

**Exposed tools** (13 tools via `create_mcp_tools()`):

| Tool | Description |
| --- | --- |
| `search_books_with_citations` | Hybrid search over book content with citation-ready output |
| `search_annotations` | Hybrid search in LanceDB across highlights, notes, and Calibre comments |
| `list_annotated_books` | Lists all books with indexed annotations |
| `get_book_annotations` | Get annotations for a specific book by path |
| `get_book_details` | Full metadata for a specific Calibre book ID |
| `list_books_by_author` | All titles by an author, directly from Calibre metadata (partial match, optional tag/year filter) |
| `export_bibliography` | Bibliography in BibTeX, RIS, EndNote, JSON, CSV |
| `detect_duplicates` | Find duplicate books by title+author, ISBN, or exact title |
| `list_tags` | All Calibre tags with book counts |
| `set_research_interests` | Register project keywords that receive a score boost in subsequent searches (no re-indexing) |
| `watchdog_scan` | Hash-diff scan of the Calibre library; updates metadata/annotation deltas and queues new books (Calibre-only) |
| `compute_annotation_hash` | Compute Calibre annotation hash for a book path |
| `get_doublette_tag_instruction` | Helper for Calibre duplicate tagging workflow |

> The unified multi-library server (`src/calibre_mcp/unified_server.py`) exposes the same tool set with adapter-aware gating: `detect_duplicates` and `watchdog_scan` are Calibre-only (`_CALIBRE_ONLY_TOOLS`), while aggregation tools and `set_research_interests`/`get_book_details` accept an optional `source`.

Each search result includes sufficient metadata for academic citation: author, title, year, and either page number/page label (PDF) or chapter/section (EPUB). A result read from a Scriptor bundle also carries `label_source`; a label that was inferred rather than printed is flagged in the CLI output and reaches Claude's prompt as `Page: 88 (inferred)` (citation contract: `WATCHDOG_AND_WIKI.md` §II.5/§II.6).

#### Web UI (`scripts/web_ui.py`)

Streamlit-based interface for users without Claude Desktop. Provides search with tag filtering, language selection, mode selection (semantic/keyword/hybrid), context expansion, and Markdown export. Positioned as a companion tool, not the primary interface.

#### CLI (`scripts/rag_demo.py`, `scripts/batch_index.py`)

The RAG engine lives in `src/archilles/engine/` (`ArchillesRAG` facade composing `Indexer`, `Searcher` and `PromptBuilder`); `rag_demo.py` is a thin CLI wrapper around it for single-book indexing, search queries, and result export.

`batch_index.py` handles batch indexing operations with tag-based filtering (`--tag`), author filtering (`--author`), dry-run previews, skip-existing for interrupted sessions, forced re-indexing (`--force`), hardware profile selection (`--profile`), checkpoint-based resume for long-running operations, format preference (`--prefer-format pdf|epub|mobi|azw3`), and orphan cleanup (`--cleanup-orphans` removes index entries for books deleted from Calibre).

* * *

### 8\. Scheduled Routines Layer (`scripts/run_routine.py`, `scripts/run_link_vault.py`, `scripts/weekly_status_mail.py`)

A thin orchestration layer that wraps the indexing tools above for unattended, scheduler-driven operation across the unified server's three sources (Calibre, Zotero, Obsidian/folder vaults). Background and rationale: see [ADR-025](DECISIONS.md#adr-025-scheduled-routines--pragmatischer-schritt-a-vor-watchdog-generalisierung-mai-2026).

#### `run_routine.py` — generic per-source wrapper

Reads the master config (`~/.archilles/config.json`), looks up the named source, and dispatches to the appropriate tool based on adapter type:

| Adapter | Tool invoked | What runs |
| --- | --- | --- |
| `calibre`, `zotero` | `scripts/watchdog.py --json` | Hash-diff scan + delta updates for metadata/annotations, queues new books |
| `obsidian`, `folder` | `scripts/batch_index.py --all --skip-existing` | Indexes documents not yet in LanceDB; metadata diff is *not* detected |

CLI: `--source <name>` (required, must match a master-config entry) and `--frequency daily|weekly` (required). Throttling lives in the script, not the trigger: each successful run writes `<library>/.archilles/last_routine_run.txt` with an ISO timestamp; subsequent invocations skip when the marker is from today (`daily`) or the same ISO calendar week (`weekly`). Flags `--force` and `--dry-run` bypass the marker and the subprocess respectively.

Output is captured (`subprocess.run(capture_output=True)`) and written to `<library>/.archilles/routine.log` (full subprocess stdout/stderr) and `<library>/.archilles/routine_history.jsonl` (one JSON record per run with timestamp, exit_code, duration_s, parsed stats). Stats parsing is best-effort: the Calibre path extracts the trailing JSON document from `watchdog --json` output via brace-counting; the batch-index path runs regexes over the human-readable summary lines.

#### `run_link_vault.py` — gated vault maintenance

A specialised wrapper around an external `link_vault.py --semantic --apply` maintenance script (path set via `--script` or the `link_vault_script` key on the vault source in the master config), which clusters Obsidian notes by topic, generates MOC files, and inserts cross-links based on LanceDB-derived semantic similarity. Because `--semantic` reads embeddings, the vault-linker requires a freshly indexed LanceDB. Two gates protect this:

1. **Lab-routine gate.** Reads `<lab>/.archilles/last_routine_run.txt`; if not dated today, the run is skipped with reason `lab_routine_not_today`.
2. **Monthly marker.** Own marker `<lab>/.archilles/last_link_vault_run.txt`; if it falls in the current calendar month, skipped with reason `monthly_marker`.

Skips are recorded (with reason) in `<lab>/.archilles/vault_linker_history.jsonl` — they are operational signals, not failures. The next logon trigger retries automatically; on a day when the Lab routine completes, the linker fires next.

#### `weekly_status_mail.py` — operations digest

Reads each source's `routine_history.jsonl` plus the linker's `vault_linker_history.jsonl`, filters to the past 7 days, formats a plaintext digest (per-source counters, last run, last failure, last skip with reason) and sends it via Gmail SMTPS. Auth: `GMAIL_APP_PASSWORD` from `~/.archilles/secrets.env` (or `secrets.env.txt`, accommodating Notepad's silent `.txt` suffix on Windows). Own ISO-week marker (`~/.archilles/last_weekly_mail.txt`) ensures one digest per week.

#### `install_scheduled_routines.ps1` — Windows Task Scheduler installer

Idempotent PowerShell installer that registers six tasks under the current user without admin rights, all with OnLogon trigger and per-task delay (5 min for the fast routines, 25 min for the Calibre full-text drainer, 15 min for the mailer, 30 min for the linker so it strictly runs after the Lab routine has had a chance to complete). Throttling lives in the scripts, not in the triggers — so a task can fire at every logon and the script decides whether work is due. Missed triggers (machine off) are picked up at the next logon via `-StartWhenAvailable`. Python interpreter and repo root are auto-detected; the per-source frequencies below are the shipped defaults and can be overridden via installer parameters (`-CalibreFrequency`, `-ZoteroFrequency`, `-LabFrequency`).

| Task | Delay | Default frequency | Tool |
| --- | --- | --- | --- |
| `Archilles-Routine-Calibre` | PT5M | weekly (Phase A) | `watchdog.py --json` |
| `Archilles-Routine-Calibre-B` | PT25M | weekly (Phase B, no execution time limit) | `watchdog.py --json` |
| `Archilles-Routine-Lab` | PT5M | daily | `batch_index --all --skip-existing` |
| `Archilles-Routine-Zotero` | PT5M | daily | `watchdog.py --json` |
| `Archilles-Status-Mail` | PT15M | weekly | `weekly_status_mail.py` |
| `Archilles-Vault-Linker` | PT30M | monthly + hard-gate | `link_vault.py --semantic --apply` |

* * *

## Data Flow

### Indexing Flow

```
Book file (PDF/EPUB/TXT/HTML/...) — the book's identity throughout
        │
        ▼
Text source (Indexer._text_source)
  • the book's Scriptor bundle, if <library>/.archilles/scriptor/<key>/ holds one
  • otherwise the book file itself
        │
        ▼
FormatDetector → select appropriate Extractor
        │
        ▼
Text extraction with metadata enrichment
  • Scriptor master: page markers → page_label (+ page_number, label_source
         from the pagination sidecar), regions → section_type/region,
         headings → chapter/section_title, footnotes bound per chunking_strategy
  • PDF: CropBox filtering, page label extraction, footnote detection,
         TOC-to-page mapping (chapter/section_title), running footer removal
  • EPUB: TOC parsing, section classification (front/main/back matter, title-based)
  • TXT: YAML frontmatter stripping (for Obsidian vault imports)
  • Other: CalibreConverter → EPUB/PDF → extraction
        │
        ▼
Calibre metadata lookup (read-only from metadata.db, via the book file)
  • title, author, year, publisher, tags, custom fields
  • language auto-detection via Lingua on extracted text
        │
        ▼
Chunking inside the extractor (BaseExtractor; ScriptorExtractor its own)
  • paragraphs gathered up to chunk_size (IndexRecipe), oversized ones split
  • window_text for context expansion
  • section_type, page_label, chapter/section_title carried per chunk
  • optional parent/child grouping (--hierarchical, or mode → ExecutionPlan)
        │
        ▼
BGE-M3 embedding generation (1024 dimensions)
  • GPU-accelerated when available (profile-based batch size)
  • Automatic CPU fallback
        │
        ▼
LanceDB storage
  • single "chunks" table: full-text chunks + annotations + comments,
    separated by chunk_type (content/child/parent/annotation/calibre_comment)
  • IVF-PQ + FTS indexes created after bulk ingestion
```

### Search Flow

```
User query (natural language)
        │
        ▼
ArchillesService.search()
        │
        ├──── LanceDBStore ──────────────────────┐
        │                                         │
        ▼                                         ▼
   BGE-M3 encode query              BM25 keyword expansion
        │                                         │
        └──────────┬──────────────────────────────┘
                   │
                   ▼
        LanceDB native hybrid search
        (dense + sparse vector fusion via RRFReranker)
                   │
                   ▼
        Section filtering (default: main only)
        Tag filtering, language filtering
                   │
                   ▼
        [Optional] Cross-encoder reranking
        (BAAI/bge-reranker-v2-m3, top candidates)
                   │
                   ▼
        Per-book diversification (max_per_book)
        Minimum similarity threshold
                   │
                   ▼
        Context expansion (Small-to-Big)
        Retrieve window_text for surrounding passages
                   │
                   ▼
        Formatted results with citations
        → MCP: structured JSON with source metadata
        → CLI: formatted text or Markdown export
        → Web UI: interactive result display
```

* * *

## Technology Stack

| Component | Implementation | Notes |
| --- | --- | --- |
| Vector database | LanceDB | Native hybrid search, IVF-PQ, Arrow-based |
| Annotations | LanceDB (same table) | chunk_type='annotation'/'calibre_comment' in chunks table |
| Embeddings (all) | BGE-M3 (BAAI) | 1024 dimensions, multilingual, GPU |
| Reranker | bge-reranker-v2-m3 | Optional cross-encoder, CPU default |
| PDF extraction | PyMuPDF (fitz) | Primary; pdfplumber as fallback |
| EPUB extraction | ebooklib | With custom TOC parser |
| OCR | Tesseract | Baseline; VLM upgrade path prepared |
| Language detection | Lingua | 75+ languages, ISO 639-1 codes |
| MCP protocol | JSON-RPC 2.0 / stdio + HTTP/SSE | stdio for Claude Desktop; SSE for ChatGPT, Codex, Cursor |
| Web UI | Streamlit | Companion interface |
| Configuration | JSON | `.archilles/config.json` in library |
| Runtime | Python 3.11+ | Cross-platform: macOS, Windows/WSL, Linux |

* * *

## Directory Structure

```
archilles/
├── mcp_server.py                  # MCP entry point (Claude Desktop calls this)
├── requirements.txt
├── README.md
│
├── src/
│   ├── adapters/                  # Source library backends
│   │   ├── __init__.py            # get_adapter() auto-detection
│   │   ├── base.py                # BaseAdapter ABC
│   │   ├── calibre_adapter.py     # Calibre library
│   │   ├── zotero_adapter.py      # Zotero library
│   │   ├── obsidian_adapter.py    # Obsidian vault (Markdown + YAML)
│   │   └── folder_adapter.py      # Plain directory fallback
│   │
│   ├── archilles/                 # Engine and indexing infrastructure
│   │   ├── engine/                # Core RAG engine (the production path)
│   │   │   ├── core.py            # ArchillesRAG facade (composition + delegators)
│   │   │   ├── indexing.py        # Indexer (index/prepare/embed, metadata, hashing, bundle text source)
│   │   │   ├── search.py          # Searcher (4 search modes, result formatting)
│   │   │   └── prompting.py       # PromptBuilder (Claude prompts, XML, markdown export)
│   │   ├── book_files.py          # discover_formats, Scriptor bundle master lookup
│   │   ├── scriptor_build.py      # The four admission conditions for a Scriptor bundle
│   │   ├── pipeline_version.py    # Generation marker written into every row
│   │   ├── pipeline.py            # ModularPipeline (experimental, behind --use-modular-pipeline)
│   │   ├── profiles.py            # IndexRecipe, legacy hardware profiles
│   │   ├── hardware.py            # Hardware detection (GPU, VRAM)
│   │   ├── parsers/               # experimental path only
│   │   │   ├── base.py            # DocumentParser ABC, ParsedDocument
│   │   │   ├── pymupdf_parser.py  # PDF parser
│   │   │   ├── epub_parser.py     # EPUB parser
│   │   │   └── registry.py        # ParserRegistry
│   │   ├── chunkers/             # experimental path only; selected by strategy (no registry)
│   │   │   ├── base.py            # TextChunker ABC, TextChunk, ChunkerConfig
│   │   │   ├── fixed.py           # FixedSizeChunker
│   │   │   ├── semantic.py        # SemanticChunker (sentence + heading aware)
│   │   │   └── dialogue.py        # DialogueChunker (chat/Q&A exports)
│   │   ├── embedders/            # selected by profile (no registry)
│   │   │   ├── base.py            # TextEmbedder ABC, EmbeddingResult
│   │   │   └── bge.py             # BGEEmbedder (bge-small/base/m3)
│   │   ├── annotation_providers/ # AnnotationProviderRegistry + calibre/kindle/pdf/zotero
│   │   ├── registry.py           # BaseRegistry[T] (generic base)
│   │   ├── constants.py          # ChunkType (content/parent/child/annotation/...)
│   │   └── indexer/
│   │       └── checkpoint.py      # IndexingCheckpoint (resume support)
│   │
│   ├── extractors/                # Text extraction from book files
│   │   ├── base.py                # BaseExtractor interface (+ oversized para splitting)
│   │   ├── universal_extractor.py # Delegates to format-specific extractors
│   │   ├── pdf_extractor.py       # PyMuPDF: CropBox, page labels, TOC mapping, footer removal
│   │   ├── epub_extractor.py      # ebooklib: TOC + section metadata (title-based classification)
│   │   ├── scriptor_extractor.py  # Scriptor bundle: master + pagination sidecar (via scriptor.document)
│   │   ├── txt_extractor.py       # Plain text (+ YAML frontmatter stripping)
│   │   ├── html_extractor.py      # HTML document extraction
│   │   ├── ocr_extractor.py       # Tesseract OCR integration
│   │   ├── calibre_converter.py   # Calibre ebook-convert bridge
│   │   ├── format_detector.py     # File format identification
│   │   ├── language_detector.py   # Lingua-based language detection
│   │   ├── models.py              # ChunkMetadata, ExtractionMetadata, ExtractedText
│   │   └── exceptions.py          # ExtractionError hierarchy
│   │
│   ├── storage/
│   │   └── lancedb_store.py       # LanceDBStore (all chunks: content + annotations)
│   │
│   ├── retriever/
│   │   └── reranker.py            # CrossEncoderReranker (optional)
│   │
│   ├── service/
│   │   └── archilles_service.py   # ArchillesService (central facade)
│   │
│   ├── citation/
│   │   └── config.py             # Citation style config (CSL-ready, Zotero-compatible)
│   │
│   ├── calibre_mcp/               # MCP server + annotation infrastructure
│   │   ├── server.py              # CalibreMCPServer + create_mcp_tools() (13 tools)
│   │   ├── unified_server.py      # Unified multi-library server (adapter-gated tools)
│   │   ├── annotations.py         # Annotation extraction, hash mapping, text search
│   │   ├── annotation_writer.py   # Writes annotation chunks into LanceDB
│   │   ├── annotation_providers/  # Provider registry: calibre, kindle, pdf
│   │   └── calibre_analyzer.py    # Library statistics and analysis
│   │
│   └── calibre_db.py              # Read-only Calibre metadata.db access
│
├── scripts/
│   ├── rag_demo.py                # Thin CLI wrapper around the engine (search, index, export)
│   ├── web_ui.py                  # Streamlit Web UI
│   ├── batch_index.py             # Batch indexing with tag/author filters
│   ├── scriptor_prepare.py        # Builds, judges and indexes Scriptor bundles
│   └── chunk_inspector.py         # Diagnostic: chunk quality, boundaries, TOC alignment
│
├── .archilles/                    # Per-library data (inside library folder)
│   ├── config.json                # User configuration
│   ├── rag_db/                    # LanceDB database (all chunks: content + annotations)
│   └── scriptor/<key>/            # Scriptor bundles: master, review copy, sidecars
│                                  #   (source, not derivative: back it up)
│
└── docs/
    ├── DECISIONS.md               # Strategic + technical decision log
    ├── ARCHITECTURE.md            # This document
    └── ...
```

* * *

## Configuration Reference

All configuration is stored in `.archilles/config.json` inside the user's Calibre library directory. This keeps configuration portable with the library.

| Key | Default | Description |
| --- | --- | --- |
| `enable_reranking` | auto | Cross-encoder reranking (Stage 2). Unset: hardware-tier default (GPU ≥8 GB VRAM: on, otherwise off); explicit `true`/`false` wins |
| `preload_models` | `true` | Warm the RAG stack (embedding model, reranker) in a background thread at MCP server start, so the first search does not hit MCP client tool-call timeouts |
| `reranker_device` | `"cpu"` | Device for reranker (`"cpu"` or `"cuda"`) |
| `rag_db_path` | `.archilles/rag_db` | Custom path for LanceDB database |
| `mode` | `auto` | Indexing path: `auto`, `light`, `full-local`, `full-external` (ADR-028) |
| `scriptor.chunking` | `"scientific"` | Chunking strategy Scriptor declares in the bundles `scriptor_prepare.py` builds: `scientific` keeps each footnote with its anchor paragraph, `basic` drops the definitions |
| `library_path` | (env var) | Override for ARCHILLES_LIBRARY_PATH |
| `transport.mode` | `"stdio"` | MCP transport: `"stdio"` or `"sse"` |
| `transport.host` | `"127.0.0.1"` | SSE bind address (localhost only) |
| `transport.port` | `8765` | SSE listen port |
| `transport.auth_token` | (none) | Optional Bearer token for SSE auth |

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `ARCHILLES_LIBRARY_PATH` | (required) | Path to library root (Calibre, Zotero, or folder) |
| `CALIBRE_LIBRARY_PATH` | — | Legacy alias for ARCHILLES_LIBRARY_PATH |
| `RAG_DB_PATH` | `$ARCHILLES_LIBRARY_PATH/.archilles/rag_db` | Override LanceDB location |
| `CUDA_VISIBLE_DEVICES` | —   | GPU selection for embeddings/reranking |

* * *

## Security and Privacy

ARCHILLES makes no network calls during normal operation. The only exceptions are the initial download of the BGE-M3 model (~2.2 GB) and optionally the cross-encoder model (~560 MB), both fetched from Hugging Face on first run.

There is no telemetry, no analytics, no usage tracking. The system reads Calibre's metadata.db but never writes to it. All ARCHILLES data lives in the `.archilles` directory within the user's Calibre library folder, making it easy to back up, move, or delete.

When connected to an external LLM via MCP, the search results (text chunks with metadata) are sent to the LLM—this is the intended use case. But the user controls which LLM they connect to, and the connection is initiated by the user's MCP client (e.g., Claude Desktop), not by ARCHILLES.

* * *

## Extension Points

The architecture is designed with explicit extension zones for future development:

**Extractors:** New format support (specialized XML schemas, proprietary formats) can be added by implementing the `BaseExtractor` interface and registering with `UniversalExtractor`. This is the extension point production uses; the Scriptor import is built there. `CalibreConverter` already bridges to 30+ formats via Calibre's ebook-convert.

**Pipeline components:** Embedders implement `TextEmbedder`, annotation sources register in the `AnnotationProviderRegistry`. The parser and chunker ABCs (`DocumentParser`, `TextChunker`) belong to the experimental modular pipeline (section 3) and reach production only through it.

**Embedding models:** BGE-M3 is the current default. The `BGEEmbedder` already supports multiple BGE variants; evaluation of multilingual-e5 and jina-embeddings-v3 is planned for Q2 2026.

**Chunking strategies:** `SemanticChunker` (sentence-boundary-aware) and `FixedSizeChunker` are available. Parent-child hierarchies (index small chunks for precision, deliver large context for comprehension) are supported via `parent_id` and `chunk_type` fields.

**Search backends:** The LanceDB abstraction supports plugging in alternative search strategies. Graph RAG via LightRAG is planned for evaluation in Q2 2026.

**The `.archilles` folder** serves as the defined extension zone per library. Configuration, databases, cached models, and any future plugin data live here—cleanly separated from Calibre's own data.

* * *

## Platform Compatibility

ARCHILLES targets cross-platform operation across macOS, Windows (native and WSL), and Linux. Key implementation rules:

All file paths use `pathlib.Path`, never string concatenation. All file I/O defaults to UTF-8 encoding. Dependencies are chosen for platform-agnostic availability—no OS-specific libraries in the core stack.

* * *

*For the rationale behind these architectural decisions, see [DECISIONS.md](DECISIONS.md).*
*For user-facing documentation, see the [README](../README.md).*
