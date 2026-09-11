# AGENTS.md

This file provides guidance to Codex when working with code in this repository.

## Project Overview

ARCHILLES is a privacy-first, local-first RAG (Retrieval-Augmented Generation) system for semantic search across Calibre e-book libraries. It provides hybrid vector+BM25 search with academic-grade citations, MCP integration for Claude Desktop; it will provide MCP integration for other LLMs soon, and finally a Streamlit web UI.

Note for Codex: ARCHILLES cannot currently be consumed via MCP from Codex — local MCP servers are not supported there. If that policy changes, wiring it up is worth a new attempt.

## Commands

### Installation
```bash
pip install -r requirements.txt
```

### Indexing
```bash
python scripts/rag_demo.py index "/path/to/book.pdf" --book-id "AuthorName"
python scripts/batch_index.py --tag "Your-Tag" [--dry-run] [--skip-existing]
python scripts/scriptor_prepare.py --tag "Your-Tag" [--dry-run] [--no-index]   # Scriptor bundles
```

### Searching
```bash
python scripts/rag_demo.py query "search term" [--mode hybrid|semantic|keyword]
python scripts/rag_demo.py query "text" --language de --tag-filter History
python scripts/rag_demo.py stats
python scripts/rag_demo.py list-indexed
```

### Web UI
```bash
streamlit run scripts/web_ui.py
```

### MCP Server (Claude Desktop)
```bash
python mcp_server.py
```

### Code Quality
```bash
black src/ scripts/    # formatting
flake8 src/ scripts/   # linting
pytest                 # tests
```

## Conventions

All source code — comments, docstrings, identifiers, log/error messages and test prose — is written in **English**. German is allowed only where it is data, not code: user-facing locale strings (e.g. `src/archilles/i18n.py`) and the docs under `docs/` that are intentionally German (`DECISIONS.md`, `ROADMAP.md`).

## Architecture

The system follows a layered pipeline:

```
Library (Calibre, read-only SQLite; Zotero, Obsidian, folders via adapters)
    ↓
Text source: the book file, or its Scriptor bundle (<library>/.archilles/scriptor/<key>/)
    ↓
Extractors (PDF/EPUB/Scriptor/TXT/HTML; MOBI/DJVU via Calibre conversion; OCR) — they also chunk
    ↓
Indexer (src/archilles/engine): Calibre metadata, BGE-M3 embedding, write
    ↓
LanceDB (one `chunks` table; hybrid: dense vectors + BM25 FTS)
    ↓
Retriever (RRF fusion + optional cross-encoder reranking)
    ↓
ArchillesService (central facade)
    ↓
Consumers: MCP Server | Web UI (Streamlit) | CLI
```

### Key Modules

- **`src/calibre_db.py`** — Read-only access to Calibre's `metadata.db` (SQLite). This is an absolute boundary: never write to the Calibre library.
- **`src/service/archilles_service.py`** — Single facade used by MCP server, web UI, and CLI. Start here when adding new features.
- **`src/archilles/engine/`** — Core RAG engine (`ArchillesRAG` facade composing `Indexer`, `Searcher`, `PromptBuilder`). `Indexer.index_book`/`prepare_book` is the production indexing path. Start here for engine changes.
- **`src/extractors/`** — Format-specific extractors, coordinated by `UniversalExtractor`; the extractor also chunks (`BaseExtractor`). PDF uses PyMuPDF with a pdfplumber fallback, EPUB uses ebooklib with TOC-based section classification, `scriptor_extractor.py` reads Scriptor bundles.
- **The Scriptor seam** — `archilles-scriptor` is a hard runtime dependency in one direction: Archilles imports `scriptor.document` (the format reader), the marker grammar and the region vocabulary; Scriptor imports nothing from here. A bundle under `<library>/.archilles/scriptor/<key>/` replaces a book's *text source* in `Indexer._text_source`, never its identity — metadata, annotations and `source_file` stay with the book file. Bundles are built by `scripts/scriptor_prepare.py`. See `docs/DECISIONS.md` ADR-032.
- **`src/archilles/pipeline.py`** — `ModularPipeline`: experimental since July 2026, reachable only behind `--use-modular-pipeline`, set by no routine. Not the production path; do not build on it (ADR-004, September addendum).
- **`src/storage/lancedb_store.py`** — LanceDB backend. One `chunks` table for book content, annotations and Calibre comments, told apart by `chunk_type`; 1024-dim BGE-M3 vectors with rich metadata. New columns are added by schema migration with a default, so existing rows stay untouched.
- **`src/calibre_mcp/server.py`** — MCP server exposing 13 tools (search, metadata, citations, annotations, stats). Carefully manages stdout/stderr: any stray write corrupts the JSON-RPC protocol.
- **`mcp_server.py`** — Entry point for Claude Desktop MCP integration.
- **`scripts/rag_demo.py`** — Thin CLI around the engine. Index, query, stats, list-indexed.

### Search Architecture (Two-Stage)

1. **Hybrid Search** in LanceDB: dense vector (BGE-M3, semantic) + BM25 (keyword), fused via RRF
2. **Optional Cross-Encoder Reranking**: BAAI bge-reranker-v2-m3 rescores top-k candidates; gracefully disabled if not configured

Search modes: `hybrid` (default), `semantic`, `keyword`.

### Embeddings

BGE-M3 via sentence-transformers: 1024 dimensions, multilingual (75+ languages). The indexing path is chosen by `mode` in `.archilles/config.json` (`auto | light | full-local | full-external`), resolved together with the detected hardware into an `ExecutionPlan` (ADR-028). The hardware profiles in `src/archilles/profiles.py` (`minimal` batch 8, `balanced` 32, `maximal` 64) remain as a legacy override.

### Configuration

Runtime config at `.archilles/config.json` inside the Calibre library:
```json
{
  "enable_reranking": true,
  "reranker_device": "cpu",
  "rag_db_path": ".archilles/rag_db",
  "embedder": {
    "mode": "remote",
    "host": "http://192.168.1.50:8900",
    "port": 8900,
    "token": "…",
    "batch_size": 100,
    "use_gzip": true
  },
  "scriptor": {
    "chunking": "scientific"
  }
}
```

The optional `embedder` block supplies defaults for the `embed` command (Phase 2 of two-phase indexing); CLI flags override it. Omit it for local embedding. The optional `scriptor` block sets the chunking strategy of the bundles `scriptor_prepare.py` builds.

Environment variable: `ARCHILLES_LIBRARY_PATH` (legacy: `CALIBRE_LIBRARY_PATH` also accepted).

## Registry Pattern

New input formats belong in `src/extractors/`: a `BaseExtractor` subclass that `UniversalExtractor` dispatches to. That is the production path, and the Scriptor import is built there (ADR-032).

Formal registries exist only where selection is a real runtime dispatch, both on the generic `BaseRegistry[T]` (`src/archilles/registry.py`):
- `AnnotationProviderRegistry` (annotation sources) is in production.
- `ParserRegistry` (`parsers/registry.py`, by file format) serves only the experimental `ModularPipeline`. Do not dock new features there.

Chunkers and embedders have no registry; their openness comes from the `TextChunker`/`TextEmbedder` ABCs.

## Chunk Schema

Each row carries book metadata (`calibre_id`/`source_id`, title, author, tags, language), its address (`page_number`, `page_label`, `chapter`, `section_title`), `section_type` (`main_content`/`front_matter`/`back_matter`), `window_text` for Small-to-Big retrieval, change-detection hashes (`metadata_hash`, `annotation_hash`) and `pipeline_version`. Rows read from a Scriptor bundle add `region`, `label_source` and `producer_version`.

Non-obvious default: searches filter to `section_filter='main'` (main content or unclassified), which excludes front matter, bibliography and index noise.

## Important Docs

- `docs/ARCHITECTURE.md` — Technical deep-dive
- `docs/DECISIONS.md` — Decision archive (German): why the system is built the way it is. Read the relevant ADR before revisiting a settled design question.
- `docs/ROADMAP.md` — Planned work and, more importantly, its sequencing and the gates between stages
- `docs/WATCHDOG_AND_WIKI.md` — Watchdog spec; §II.5/§II.6 carry the cross-repo citation contract (page markers and their provenance) shared with `archilles-scriptor`
- `docs/INSTALLATION.md` — Setup, including the Windows path
