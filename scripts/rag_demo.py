#!/usr/bin/env python3
"""
ARCHILLES RAG System with Hybrid Search

Features:
1. Extract text from books (30+ formats: PDF, EPUB, DJVU, MOBI, etc.)
2. BGE-M3 embeddings (multilingual, optimized for German/Latin/Greek)
3. LanceDB with native hybrid search (vector + full-text)
4. Language filtering (auto-detected: de, en, la, fr, etc.)
5. Local storage (100% offline)

Search Modes:
- hybrid (default): Best of both worlds - finds concepts AND exact words
- semantic: Concept-based search using BGE-M3 embeddings
- keyword: Exact word matching using full-text search (great for Latin phrases, custom terms)

Usage:
    # Index a book
    python scripts/rag_demo.py index "path/to/book.pdf" --book-id "Josephus"

    # Hybrid search (recommended - combines semantic + keyword)
    python scripts/rag_demo.py query "evangelista et a presbyteris"

    # Keyword-only (exact word matching)
    python scripts/rag_demo.py query "Herrschaftslegitimation" --mode keyword

    # With language filter
    python scripts/rag_demo.py query "Rex" --language la --mode hybrid
"""

import sys
import argparse
import json
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.archilles.config import (
    get_library_path,
    get_languages,
    get_mode,
    get_rag_db_path,
    get_embedder_config,
    resolve_embedder_settings,
)
from src.archilles.constants import ChunkType
from src.archilles.engine import ArchillesRAG, LanceDBError  # noqa: F401 — LanceDBError: Re-Export fuer Alt-Abnehmer

# Kompat-Shim (Spec 2026-06-11): Alt-Abnehmer importieren ``archillesRAG`` von hier.
archillesRAG = ArchillesRAG


def _handle_import_annotations(args):
    """Handle the import-annotations subcommand."""
    from src.archilles.annotation_providers import create_default_registry
    from src.calibre_mcp.book_matcher import BookMatcher, load_asin_index
    from src.calibre_mcp.annotation_writer import (
        annotation_dicts_for_matching,
        write_annotations,
    )

    source = args.source
    file_path = args.path

    # Resolve library path for Calibre DB matching
    library_path = get_library_path(required=False)
    library_path = str(library_path) if library_path else None

    # 1. Parse annotations
    print(f"\n{'='*60}")
    print(f"ANNOTATION IMPORT — Source: {source}")
    print(f"{'='*60}\n")

    registry = create_default_registry()

    if source == 'auto':
        provider = registry.detect(file_path)
        if not provider:
            print(f"ERROR: Could not auto-detect provider for: {file_path}")
            print(f"Available sources: {registry.available}")
            sys.exit(1)
        print(f"  Auto-detected provider: {provider.name}")
    else:
        provider = registry.get(source)
        if not provider:
            print(f"ERROR: Unknown source '{source}'. Available: {registry.available}")
            sys.exit(1)

    annotations = provider.extract(file_path)
    print(f"  Parsed: {len(annotations)} annotations from {file_path}")

    if not annotations:
        print("\n  No annotations found. Nothing to import.")
        return

    # Group by book for display
    by_book = {}
    for a in annotations:
        key = a.book_title or "(unknown)"
        by_book.setdefault(key, []).append(a)

    print(f"  Books:  {len(by_book)}")
    for title, annots in sorted(by_book.items()):
        types = {}
        for a in annots:
            types[a.type] = types.get(a.type, 0) + 1
        type_str = ", ".join(f"{v} {k}s" for k, v in types.items())
        print(f"    - {title}: {type_str}")

    # 2. Match to Calibre library
    if library_path:
        print(f"\n  Matching against Calibre library: {library_path}")
        try:
            from src.calibre_db import CalibreDB
            with CalibreDB(Path(library_path)) as db:
                books = db.get_all_books_brief()
            print(f"  Calibre books loaded: {len(books)}")
        except Exception as e:
            print(f"  WARNING: Could not load Calibre DB: {e}")
            books = []

        if books:
            asin_index = load_asin_index(Path(library_path))
            matcher = BookMatcher(
                books,
                fuzzy_threshold=args.fuzzy_threshold,
                asin_index=asin_index,
            )
            print(f"  ASIN identifiers in Calibre: {len(asin_index)}")

            items = annotation_dicts_for_matching(annotations)
            matched, unmatched = matcher.match_batch(items)

            print(f"\n  Match results:")
            print(f"    Matched:   {len(matched)} annotations")
            print(f"    Unmatched: {len(unmatched)} annotations")

            if matched:
                # Group matched by calibre book for display
                by_calibre = {}
                for m in matched:
                    cid = m["calibre_id"]
                    by_calibre.setdefault(
                        cid, {"title": m["calibre_title"], "items": []}
                    )
                    by_calibre[cid]["items"].append(m)
                print(f"\n  Matched books:")
                for cid, info in sorted(by_calibre.items()):
                    score = info["items"][0].get("match_score", 0)
                    mtype = info["items"][0].get("match_type", "?")
                    print(
                        f"    [{cid}] {info['title']} — "
                        f"{len(info['items'])} annotations "
                        f"({mtype}, score: {score:.0f})"
                    )

            if unmatched:
                print(f"\n  Unmatched (not in Calibre library):")
                unmatched_titles = set()
                for u in unmatched:
                    t = u.get("title", "(unknown)")
                    if t not in unmatched_titles:
                        unmatched_titles.add(t)
                        print(f"    - {t}")

                if not args.dry_run and library_path:
                    review_path = (
                        Path(library_path) / ".archilles" / "unmatched_annotations.json"
                    )
                    review_path.parent.mkdir(parents=True, exist_ok=True)
                    review_data = [
                        {"title": u.get("title"), "author": u.get("author")}
                        for u in unmatched
                    ]
                    with open(review_path, "w", encoding="utf-8") as f:
                        json.dump(review_data, f, indent=2, ensure_ascii=False)
                    print(f"\n  Review queue written to: {review_path}")

            # 3. Persistence: embed and write into LanceDB (unless --dry-run)
            if matched and not args.dry_run:
                db_path = Path(args.db_path) if args.db_path else (
                    Path(library_path) / ".archilles" / "rag_db"
                )
                print(f"\n  Embedding and writing to LanceDB: {db_path}")
                n_books, n_notes = write_annotations(
                    matched=matched,
                    library=Path(library_path),
                    db_path=db_path,
                )
                print(f"\n  Wrote {n_notes} note(s) for {n_books} book(s).\n")
    else:
        print(f"\n  WARNING: No ARCHILLES_LIBRARY_PATH set — skipping Calibre matching.")
        print(f"  Set the environment variable to enable book matching.")
        matched = []

    # 4. Summary
    if args.dry_run:
        print(f"\n  DRY RUN — no changes written to index.")
        print(f"  Remove --dry-run to import into the ARCHILLES index.\n")


def _resolve_index_plan(profile, hierarchical_flag, library_path):
    """Resolve execution_plan/hierarchical for `rag_demo.py index`.

    Without this, single-book indexing ignored the mode system entirely: a
    user on a full-local machine indexing one book got a flat book in an
    otherwise hierarchical DB, since batch_index and the watchdog would have
    chosen hierarchical via the same config. No CLI --mode flag exists for
    `index` (that's `embed`'s embedder mode, a different concept) — mode
    always comes from config here, matching batch_index.main().
    """
    from scripts.batch_index import resolve_indexing_plan
    from src.archilles.hardware import detect_hardware
    from src.archilles.recipe import default_recipe

    resolution = resolve_indexing_plan(
        mode_cli=None,
        mode_config=get_mode(library_path),
        profile_override=profile,
        hierarchical_flag=hierarchical_flag,
        prepare_only_flag=False,
        hw=detect_hardware(),
        recipe=default_recipe(),
    )
    return resolution.execution_plan, resolution.hierarchical


def _should_skip_model(command, mode):
    """Whether ArchillesRAG should skip loading the local embedding model.

    True for ``prepare`` (extraction/chunking only, no embeddings) and for
    ``embed`` when the resolved embedder mode is ``remote`` (embeddings are
    computed over HTTP). ``embed --mode local`` still needs the model.
    """
    return command == 'prepare' or (command == 'embed' and mode == 'remote')


def main():
    """Main CLI interface."""
    parser = argparse.ArgumentParser(
        description="archilles Mini-RAG: Semantic search in academic books",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index a book
  python scripts/rag_demo.py index "C:/Calibre Library/Author Name/Book Title (1)/book.pdf"

  # Recover from corrupted database (after CTRL+C during indexing)
  python scripts/rag_demo.py index "book.pdf" --reset-db

  # Query (hybrid mode by default - combines semantic + keyword)
  python scripts/rag_demo.py query "evangelista et a presbyteris"

  # Search modes (demonstration with different query types)
  python scripts/rag_demo.py query "network analysis" --mode hybrid     # Best: semantic + keyword (default)
  python scripts/rag_demo.py query "Herrschaftslegitimation" --mode keyword    # Exact word matching (FTS)
  python scripts/rag_demo.py query "migration narratives" --mode semantic   # Concept search (BGE-M3)

  # Filter by language
  python scripts/rag_demo.py query "kings" --language de
  python scripts/rag_demo.py query "Rex" --language la
  python scripts/rag_demo.py query "kings" --language de,en

  # Filter by book
  python scripts/rag_demo.py query "political theory" --book-id "Arendt_VitaActiva"

  # More results
  python scripts/rag_demo.py query "Jewish kings" --top-k 10

  # Result diversity (max results per book)
  python scripts/rag_demo.py query "Herrschaftslegitimation" --max-per-book 2  # Max 2 results per book
  python scripts/rag_demo.py query "Macht" --max-per-book 1                    # Max 1 result per book (max diversity)
  python scripts/rag_demo.py query "Marcion" --max-per-book 999                # Unlimited (all from one book OK)

  # Export to Markdown (for Joplin/Obsidian)
  python scripts/rag_demo.py query "evangelista et a presbyteris" --exact --export zitate.md
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Index command
    index_parser = subparsers.add_parser('index', help='Index a book')
    index_parser.add_argument('book_path', help='Path to book file')
    index_parser.add_argument('--book-id', help='Optional book ID (default: filename)')
    index_parser.add_argument('--db-path', default=None, help='Database path (default: CALIBRE_LIBRARY/.archilles/rag_db)')
    index_parser.add_argument('--force', action='store_true', help='Force reindex (delete existing chunks first)')
    index_parser.add_argument('--reset-db', action='store_true', help='Reset corrupted database (WARNING: deletes all indexed data)')
    # OCR options
    index_parser.add_argument('--enable-ocr', action='store_true', help='Enable OCR for scanned PDFs (auto-detect)')
    index_parser.add_argument('--force-ocr', action='store_true', help='Force OCR even for digital PDFs (skip text extraction)')
    index_parser.add_argument('--ocr-backend', choices=['auto', 'tesseract', 'lighton', 'olmocr'], default='auto',
                              help='OCR backend: auto (best available), tesseract, lighton, olmocr')
    index_parser.add_argument('--ocr-language', default=None, help='Tesseract language codes (default: derived from configured languages)')
    # Hardware profile options
    index_parser.add_argument('--profile', choices=['minimal', 'balanced', 'maximal'],
                              help='Hardware profile: minimal (CPU), balanced (GPU 6-12GB), maximal (GPU 12GB+)')
    index_parser.add_argument('--use-modular-pipeline', action='store_true',
                              help='EXPERIMENTAL — not integrated with ExecutionPlan/hierarchical/languages, '
                                   'loads a second embedder, and skips metadata_hash/annotation chunks (invisible '
                                   'to watchdog change detection). Use new ModularPipeline architecture '
                                   '(parser→chunker→embedder).')
    index_parser.add_argument('--hierarchical', action='store_true',
                              help='Enable parent-child chunking (parents ~2048, children ~512 tokens)')

    # Query command
    query_parser = subparsers.add_parser('query', help='Search indexed books')
    query_parser.add_argument('query', help='Search query')
    query_parser.add_argument('--top-k', type=int, default=10, help='Number of results (default: 10)')
    query_parser.add_argument('--mode', choices=['semantic', 'keyword', 'hybrid'], default='hybrid',
                              help='Search mode: semantic (BGE-M3), keyword (FTS), or hybrid (both, default)')
    query_parser.add_argument('--exact', action='store_true',
                              help='Exact phrase matching (case-insensitive) - critical for Latin quotes')
    query_parser.add_argument('--language', help='Filter by language (e.g., de, en, la) or comma-separated')
    query_parser.add_argument('--book-id', help='Filter by specific book ID')
    query_parser.add_argument('--tag-filter', nargs='+', help='Filter by Calibre tags (e.g., --tag-filter Geschichte Philosophie)')
    query_parser.add_argument('--section', choices=['main', 'main_content', 'front_matter', 'back_matter'],
                              default='main',
                              help='Filter by section type (default: main = exclude bibliography/index/TOC)')
    query_parser.add_argument('--all-sections', action='store_true',
                              help='Search all sections including bibliography and index (overrides --section)')
    query_parser.add_argument('--chunk-type', choices=['phase1_metadata', 'content', 'calibre_comment', 'all'],
                              default='content',
                              help='Filter by chunk type: content (book text only, DEFAULT), calibre_comment (Calibre comments), all (both)')
    query_parser.add_argument('--max-per-book', type=int, default=2, help='Maximum results per book (default: 2, use 999 for unlimited)')
    query_parser.add_argument('--db-path', default=None, help='Database path (default: CALIBRE_LIBRARY/.archilles/rag_db)')
    query_parser.add_argument('--export', metavar='FILE', help='Export results to Markdown file (for Joplin/Obsidian)')

    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Show index statistics')
    stats_parser.add_argument('--db-path', default=None, help='Database path (default: CALIBRE_LIBRARY/.archilles/rag_db)')

    # Create-index command
    create_index_parser = subparsers.add_parser('create-index', help='Create search indexes (FTS and/or IVF-PQ)')
    create_index_parser.add_argument('--db-path', default=None, help='Database path (default: CALIBRE_LIBRARY/.archilles/rag_db)')
    create_index_parser.add_argument('--fts-only', action='store_true', help='Only create FTS index (skip IVF-PQ vector index)')

    # Prepare command (extract + chunk, no embedding)
    prepare_parser = subparsers.add_parser('prepare', help='Extract and chunk a book without embedding (Phase 1)')
    prepare_parser.add_argument('book_path', help='Path to book file')
    prepare_parser.add_argument('--book-id', help='Optional book ID (default: filename)')
    prepare_parser.add_argument('--output-dir', default='./prepared_chunks', help='Output directory for JSONL files')
    prepare_parser.add_argument('--db-path', default=None, help='Database path (for metadata extraction)')
    prepare_parser.add_argument('--enable-ocr', action='store_true', help='Enable OCR for scanned PDFs')
    prepare_parser.add_argument('--force-ocr', action='store_true', help='Force OCR even for digital PDFs')
    prepare_parser.add_argument('--ocr-backend', choices=['auto', 'tesseract', 'lighton', 'olmocr'], default='auto')
    prepare_parser.add_argument('--ocr-language', default=None, help='Tesseract language codes (default: derived from configured languages)')
    prepare_parser.add_argument('--hierarchical', action='store_true', help='Enable parent-child chunking')

    # Embed command (embed prepared chunks, store in LanceDB)
    embed_parser = subparsers.add_parser('embed', help='Embed prepared chunks and store in LanceDB (Phase 2)')
    embed_parser.add_argument('--input-dir', default='./prepared_chunks', help='Directory with JSONL files from prepare')
    embed_parser.add_argument('--mode', choices=['local', 'remote'], default=None,
                              help='Embedding mode (default: local, or embedder.mode in config.json). '
                                   'Remote mode no longer loads the local embedding model.')
    embed_parser.add_argument('--host', help='Remote embedding server host (e.g. http://1.2.3.4:8000)')
    embed_parser.add_argument('--port', type=int, default=None, help='Remote server port (default: 8000)')
    embed_parser.add_argument('--token', help='Bearer token for remote server')
    embed_parser.add_argument('--batch-size', type=int, default=None, help='Texts per batch (default: 100)')
    embed_parser.add_argument('--use-gzip', action='store_true', default=True, help='Use gzip for remote requests')
    embed_parser.add_argument('--no-gzip', action='store_true', help='Disable gzip for remote requests')
    embed_parser.add_argument('--force', action='store_true', help='Re-embed: delete existing chunks and replace with prepared chunks')
    embed_parser.add_argument('--db-path', default=None, help='Database path')
    embed_parser.add_argument('--profile', choices=['minimal', 'balanced', 'maximal'], help='Hardware profile for local mode')

    # Import-annotations command
    import_parser = subparsers.add_parser('import-annotations',
        help='Import annotations from external reading apps (Kindle, Kobo, etc.)')
    import_parser.add_argument('--source', required=True,
        choices=['kindle', 'kobo', 'pdf', 'calibre_viewer', 'auto'],
        help='Annotation source')
    import_parser.add_argument('--path', required=True,
        help='Path to annotation file or database')
    import_parser.add_argument('--dry-run', action='store_true',
        help='Show what would be imported without writing to index')
    import_parser.add_argument('--fuzzy-threshold', type=float, default=80.0,
        help='Minimum fuzzy match score for book matching (0-100, default: 80)')
    import_parser.add_argument('--db-path', default=None,
        help='Database path (default: CALIBRE_LIBRARY/.archilles/rag_db)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Handle import-annotations separately (no RAG model needed)
    if args.command == 'import-annotations':
        _handle_import_annotations(args)
        return

    # Determine default database path if not specified
    if args.db_path is None:
        args.db_path = get_rag_db_path()
        print(f"📚 Using default RAG database: {args.db_path}")

    try:
        # Initialize RAG with OCR and profile options (only for index command)
        reset_db = getattr(args, 'reset_db', False)
        enable_ocr = getattr(args, 'enable_ocr', False)
        force_ocr = getattr(args, 'force_ocr', False)
        ocr_backend = getattr(args, 'ocr_backend', 'auto')
        ocr_language = getattr(args, 'ocr_language', None)
        profile = getattr(args, 'profile', None)
        use_modular_pipeline = getattr(args, 'use_modular_pipeline', False)
        hierarchical = getattr(args, 'hierarchical', False)

        # Resolve mode/plan like batch_index.main() so single-book indexing
        # doesn't silently diverge from batch/watchdog indexing (review 1.3).
        execution_plan = None
        if args.command == 'index':
            execution_plan, hierarchical = _resolve_index_plan(
                profile, hierarchical, get_library_path(required=False)
            )

        # Resolve embedder settings before constructing ArchillesRAG, so that
        # skip_model can already reflect a remote mode (avoids loading the
        # local embedding model for a run that will embed over HTTP).
        embedder_settings = None
        if args.command == 'embed':
            library_path = get_library_path(required=False)
            cfg = get_embedder_config(library_path)
            cli = {
                "mode": args.mode,
                "host": args.host,
                "port": args.port,
                "token": args.token,
                "batch_size": args.batch_size,
                "use_gzip": False if getattr(args, 'no_gzip', False) else None,
            }
            embedder_settings = resolve_embedder_settings(cli, cfg)

        # Skip embedding model for prepare command (no GPU needed) and for
        # embed --mode remote (embeddings are computed over HTTP)
        skip_model = _should_skip_model(
            args.command,
            embedder_settings['mode'] if embedder_settings else None,
        )

        rag = ArchillesRAG(
            db_path=args.db_path,
            reset_db=reset_db,
            enable_ocr=enable_ocr,
            force_ocr=force_ocr,
            ocr_backend=ocr_backend,
            ocr_language=ocr_language,
            languages=get_languages(get_library_path(required=False)),
            use_modular_pipeline=use_modular_pipeline,
            profile=profile,
            hierarchical=hierarchical,
            execution_plan=execution_plan,
            skip_model=skip_model,
        )

        if args.command == 'index':
            # Index a book
            stats = rag.index_book(args.book_path, args.book_id, force=args.force)

        elif args.command == 'query':
            # Search
            # Handle chunk_type: 'all' means no filter (None), otherwise use the specified type
            chunk_type = args.chunk_type if hasattr(args, 'chunk_type') else ChunkType.CONTENT
            chunk_type_filter = None if chunk_type == 'all' else chunk_type

            results = rag.query(
                args.query,
                top_k=args.top_k,
                mode=args.mode,
                language=args.language,
                book_id=args.book_id,
                exact_phrase=args.exact,
                tag_filter=args.tag_filter if hasattr(args, 'tag_filter') else None,
                section_filter=None if getattr(args, 'all_sections', False) else getattr(args, 'section', 'main'),
                chunk_type_filter=chunk_type_filter,
                max_per_book=args.max_per_book if hasattr(args, 'max_per_book') else 2
            )
            lang = get_languages(get_library_path(required=False))[0]
            rag.print_results(results, query_text=args.query, lang=lang)

            # Export to Markdown if requested
            if args.export:
                output_file = rag.export_to_markdown(results, args.query, args.export, lang=lang)
                print(f"? Exported to: {output_file}")

        elif args.command == 'stats':
            # Show stats
            stats = rag.store.get_stats()
            print(f"INDEX STATISTICS\n")
            print(f"  Total chunks:  {stats['total_chunks']}")
            print(f"  Total books:   {stats['total_books']}")
            print(f"  Avg chunks/book: {stats['avg_chunks_per_book']:.1f}")
            print(f"  Database path: {args.db_path}\n")
            if stats.get('chunk_types'):
                print(f"  Chunk types:")
                for ct, n in sorted(stats['chunk_types'].items(), key=lambda x: -x[1]):
                    print(f"    {ct:<25} {n:>8}")
                print()
            if stats.get('languages'):
                print(f"  Languages:")
                for lang, n in sorted(stats['languages'].items(), key=lambda x: -x[1])[:10]:
                    print(f"    {lang:<25} {n:>8}")
                print()
            if stats.get('file_types'):
                print(f"  File types:")
                for ft, n in sorted(stats['file_types'].items(), key=lambda x: -x[1]):
                    print(f"    {ft:<25} {n:>8}")
                print()
            versions = {v: n for v, n in stats.get('producer_versions', {}).items() if v}
            if versions:
                print(f"  Scriptor spec versions:")
                for v, n in sorted(versions.items()):
                    print(f"    {v:<25} {n:>8}")
                print()

        elif args.command == 'prepare':
            # Prepare book (extract + chunk, no embedding)
            stats = rag.prepare_book(
                args.book_path,
                book_id=args.book_id,
                output_dir=args.output_dir,
            )

        elif args.command == 'embed':
            # Embed prepared chunks. Remote/local settings resolve with
            # precedence CLI > .archilles/config.json (embedder block) > default
            # (resolved above, before ArchillesRAG construction).
            stats = rag.embed_prepared(
                input_dir=args.input_dir,
                profile=profile,
                force=getattr(args, 'force', False),
                **embedder_settings,
            )

        elif args.command == 'create-index':
            # Create search indexes
            chunk_count = rag.store.count()
            print(f"Creating indexes for {chunk_count} chunks...\n")

            if args.fts_only:
                rag.store.create_fts_index()
            else:
                rag.store.create_indexes(chunk_count)

            print("\nIndex creation complete.")

    except LanceDBError as e:
        # LanceDB error - show helpful error message
        print(f"\n{'='*60}")
        print(f"DATABASE ERROR")
        print(f"{'='*60}\n")
        print(str(e))
        print(f"\n{'='*60}\n")
        sys.exit(1)
    except Exception as e:
        print(f"? Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
