# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ARCHILLES is a privacy-first, local-first RAG (Retrieval-Augmented Generation) system for semantic search across Calibre e-book libraries. It provides hybrid vector+BM25 search with academic-grade citations, MCP integration for Claude Desktop; it will provide MCP intergration for other LLMs soon, and finally a Streamlit web UI.

## Commands

### Installation
```bash
pip install -r requirements.txt
```

### Indexing
```bash
python scripts/rag_demo.py index "/path/to/book.pdf" --book-id "AuthorName"
python scripts/batch_index.py --tag "Your-Tag" [--dry-run] [--skip-existing]
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

## Conventions

### Language in code: English only

All source code — comments, docstrings, identifiers, log/error messages and test prose — is written in **English**. This keeps the public repository consistent and accessible to contributors who don't read German.

German is allowed only where it is **data, not code**:
- **User-facing / locale strings**: translations and language-specific content (e.g. `src/archilles/i18n.py`, the German locale entries in `kindle_provider.py`).
- **Docs under `docs/`**: `DECISIONS.md` (the German *Entscheidungsarchiv*), `ROADMAP.md`, etc. are intentionally German.

Note: parts of the codebase still carry German comments/docstrings from before this rule. New or touched code must be English; a repo-wide cleanup is a separate task.

## Architecture

Non-obvious constraints and entry points (the rest of the layout is best read from the code):

- **`src/calibre_db.py`** — Read-only access to Calibre's `metadata.db` (SQLite). This is an absolute boundary: never write to the Calibre library.
- **`src/service/archilles_service.py`** — Single facade used by MCP server, web UI, and CLI. Start here when adding new features.
- **`src/archilles/engine/`** — Core RAG engine (`ArchillesRAG` facade composing `Indexer`, `Searcher`, `PromptBuilder`). Start here for engine changes.
- **Scriptor seam** — `archilles-scriptor` is a hard runtime dependency, one direction only. A bundle under `<library>/.archilles/scriptor/<key>/` replaces a book's *text source* (`Indexer._text_source`), never its identity. `src/extractors/scriptor_extractor.py` reads it, `scripts/scriptor_prepare.py` builds it (ADR-032).
- **`src/calibre_mcp/server.py`** — MCP server. Carefully manages stdout/stderr: any stray write corrupts the JSON-RPC protocol.

### Search Architecture (Two-Stage)

1. **Hybrid Search** in LanceDB: dense vector (BGE-M3, semantic) + BM25 (keyword), fused via RRF
2. **Optional Cross-Encoder Reranking**: BAAI bge-reranker-v2-m3 rescores top-k candidates; gracefully disabled if not configured

Search modes: `hybrid` (default), `semantic`, `keyword`.

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
  }
}
```

The optional `embedder` block supplies defaults for the `embed` command (Phase 2 of two-phase indexing); CLI flags override it. Omit it for local embedding.

Environment variable: `ARCHILLES_LIBRARY_PATH` (legacy: `CALIBRE_LIBRARY_PATH` also accepted).

## Registry Pattern

New input formats belong in `src/extractors/`: a `BaseExtractor` subclass that `UniversalExtractor` dispatches to. That is the production path — the extractor also chunks — and the Scriptor import is built there (ADR-032).

Formal registries exist only where selection is a real dispatch, on the generic `BaseRegistry[T]` (`src/archilles/registry.py`): `AnnotationProviderRegistry` (annotation sources, production) and `ParserRegistry` (`parsers/registry.py`, by file format), which serves only the experimental `ModularPipeline` (`--use-modular-pipeline`, ADR-004 addendum) — do not dock new features there. Chunkers and embedders have no registry; their openness comes from the `TextChunker`/`TextEmbedder` ABCs.

## Chunk Schema

Non-obvious default: sections are filtered to `section_type == "main"` by default, which excludes front matter, bibliography and index noise from search results.

## Session Reports

When the user asks for a closing report ("Bericht schreiben"), write it to
`D:\Archilles-Lab\CLAUDE\CC-Sessions\`. That directory's `CLAUDE.md` holds the
mandatory format (filename, YAML frontmatter, structure) — read it first, and read
nothing else there: it is an Obsidian vault, and old reports are not a template.
Reports are written in German. No commit needed; the Lab routine indexes the vault.

## Important Docs

- `docs/ARCHITECTURE.md` — Technical deep-dive
- `docs/DECISIONS.md` — Decision archive (German): why the system is built the way it is. Read the relevant ADR before revisiting a settled design question.
- `docs/ROADMAP.md` — Planned work and, more importantly, its sequencing and the gates between stages
- `docs/WATCHDOG_AND_WIKI.md` — Watchdog spec; §II.5/§II.6 carry the cross-repo citation contract (page markers and their provenance) shared with `archilles-scriptor`
- `docs/INSTALLATION.md` — Setup, including the Windows path
