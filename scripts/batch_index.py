#!/usr/bin/env python3
"""
ARCHILLES Batch Indexer

Index multiple books from Calibre library by tag, author, or other criteria.

Hardware-Adaptive Profiles:
    - minimal:  CPU-only, resource-efficient (for laptops, <6GB VRAM)
    - balanced: GPU-accelerated, good quality (6-12GB VRAM)
    - maximal:  Full GPU, maximum quality (>12GB VRAM)

Usage:
    # Index ALL books in the library (no filter required)
    python scripts/batch_index.py --all

    # Index with a specific profile (auto-detects hardware if not specified)
    python scripts/batch_index.py --all --profile minimal
    python scripts/batch_index.py --all --profile balanced

    # Index all books with tag "must-read"
    python scripts/batch_index.py --tag "must-read"

    # Index specific author's books
    python scripts/batch_index.py --author "Hannah Arendt"

    # Dry run (show what would be indexed)
    python scripts/batch_index.py --tag "must-read" --dry-run

    # Limit number of books (for testing)
    python scripts/batch_index.py --tag "must-read" --limit 5

    # Continue from where you left off (skip already indexed)
    python scripts/batch_index.py --tag "must-read" --skip-existing

    # Recover from corrupted database (e.g., after CTRL+C during indexing)
    python scripts/batch_index.py --tag "must-read" --reset-db

    # Re-index books that were indexed before a certain date (with improved code)
    python scripts/batch_index.py --tag "must-read" --reindex-before 2024-12-01

    # Re-index books with missing page labels (indexed before page label feature)
    python scripts/batch_index.py --tag "must-read" --reindex-missing-labels

    # Run in non-interactive mode (auto-resume sessions, no prompts)
    python scripts/batch_index.py --tag "must-read" --non-interactive

    # Filter by author(s) — works alongside --tag, --all, or --author (OR logic)
    python scripts/batch_index.py --tag "must-read" --filter-author "Arendt"
    python scripts/batch_index.py --tag "must-read" --rating 0 --filter-author "Arendt" --filter-author "Benjamin"
    python scripts/batch_index.py --all --filter-author "Tucholsky"

    # Include books normally excluded via the 'exclude' tag (or custom
    # tags configured in .archilles/config.json → excluded_tags)
    python scripts/batch_index.py --tag "must-read" --include-excluded
"""

import argparse
import dataclasses
import json
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.archilles.engine import ArchillesRAG, LanceDBError
from src.archilles.engine.indexing import read_prepared_header
from src.archilles.book_files import bundle_master, discover_formats, prepared_jsonl_name
from scripts.safe_indexer import SafeIndexer
from scripts.find_books_missing_labels import find_books_missing_labels

# Hardware-adaptive profile system
from src.archilles.config import (
    DEFAULT_EXCLUDED_TAGS,
    get_excluded_tags,
    get_languages,
    get_library_path,
    get_mode,
)
from src.archilles.constants import ChunkType, SectionType
from src.archilles.execution import (
    ExecutionPlan,
    plan,
    warn_if_light_plan_hides_hierarchy,
)
from src.archilles.hardware import detect_hardware
from src.archilles.profiles import get_profile, list_profiles
from src.archilles.recipe import IndexRecipe, default_recipe
from src.archilles.indexer import IndexingCheckpoint
from src.archilles.external_catchup_guard import (
    DERIVED_COUNT_LIMIT,
    check_external_catchup_bound,
)
from src.archilles.orphan_guard import (
    ORPHAN_COUNT_LIMIT,
    ORPHAN_SHARE_LIMIT,
    backup_orphan_chunks,
    check_orphan_bound,
)
from src.archilles.sqlite_ro import connect_readonly

# Preferred book formats in order of priority — canonical list in constants.py
from src.archilles.constants import PREFERRED_FORMATS


@dataclass(frozen=True)
class IndexingResolution:
    """Resolved indexing configuration for one batch_index run (Hardware-Tiers-V2).

    Either ``profile_name`` (legacy ``--profile`` override) or ``execution_plan``
    (the ``mode``-derived path) is set, never both. ``hierarchical`` and
    ``prepare_only`` are the *effective* values after applying the advanced
    ``--hierarchical`` force-on and the ``full-external`` → prepare-only rule.
    """

    profile_name: Optional[str]
    execution_plan: Optional[ExecutionPlan]
    hierarchical: bool
    prepare_only: bool
    resolved_mode: Optional[str]


def resolve_indexing_plan(
    *,
    mode_cli: Optional[str],
    mode_config: str,
    profile_override: Optional[str],
    hierarchical_flag: bool,
    prepare_only_flag: bool,
    hw,
    recipe: IndexRecipe,
) -> IndexingResolution:
    """Decide how this run indexes — pure (hardware + recipe injected, no I/O).

    Precedence:
    - ``--profile`` (legacy/advanced) bypasses the plan entirely; the profile
      drives batch/device as before, and ``hierarchical`` follows the flag only.
    - Otherwise the mode (CLI over config) feeds :func:`plan` to build the
      ExecutionPlan. ``--hierarchical`` is an advanced force-on (it can turn a
      flat mode hierarchical, never the reverse — use ``--mode light`` for flat).
      ``full-external`` has no local-embedding index path, so it forces
      prepare-only (prepare locally → ``embed`` externally, §8).
    """
    if profile_override:
        return IndexingResolution(
            profile_name=profile_override,
            execution_plan=None,
            hierarchical=hierarchical_flag,
            prepare_only=prepare_only_flag,
            resolved_mode=None,
        )

    resolved_mode = mode_cli or mode_config
    execution_plan = plan(hw, recipe, resolved_mode)
    return IndexingResolution(
        profile_name=None,
        execution_plan=execution_plan,
        hierarchical=hierarchical_flag or execution_plan.hierarchical,
        prepare_only=prepare_only_flag or (not execution_plan.embed_local),
        resolved_mode=resolved_mode,
    )




def _format_label(book: Dict[str, Any], best: Dict[str, str]) -> str:
    """The format a book's text comes from. Where the library holds a Scriptor
    bundle for it, that is SCRIPTOR: index_book reads the bundle, although the
    book file is what it is handed (see Indexer._text_source)."""
    return 'SCRIPTOR' if book.get('bundle') else best['format']


def _select_best_format(formats: List[Dict[str, str]], prefer_format: str = 'PDF') -> Dict[str, str]:
    """Select the best format from available formats.

    Tries prefer_format first, then falls back through the remaining
    PREFERRED_FORMATS order, then returns whatever is available.
    """
    order = [prefer_format.upper()] + [
        f[1:].upper() for f in PREFERRED_FORMATS
        if f[1:].upper() != prefer_format.upper()
    ]
    for fmt in order:
        for f in formats:
            if f['format'] == fmt:
                return f
    return formats[0]


# ---------------------------------------------------------------------------
# Chunk quality scoring for format selection
# ---------------------------------------------------------------------------

SENTENCE_END_CHARS = set('.!?;:\'"»)')


def _score_chunks(chunks: List[dict]) -> dict:
    """Compute quality signals from a list of chunk dicts.

    Returns a dict with individual metrics and an overall quality score (0-100).
    Higher is better.
    """
    if not chunks:
        return {
            'score': 0, 'chunk_count': 0, 'content_chunks': 0,
            'truncation_rate': 0, 'misplaced_back_rate': 0,
            'cv': 0, 'short_rate': 0, 'section_coverage': 0,
            'has_pages': False,
        }

    content_chunks = [c for c in chunks
                      if c.get('chunk_type', ChunkType.CONTENT)
                      in (ChunkType.CONTENT, ChunkType.CHILD)]
    total = len(content_chunks) or 1

    # 1. Truncation rate: chunks ending mid-sentence
    # Only count as truncated if the last paragraph is long enough to be
    # a real sentence fragment (>20 words). Short endings like headings,
    # terms, or footnote markers are not meaningful truncations.
    truncated = 0
    for c in content_chunks:
        text = (c.get('text') or '').rstrip()
        if not text or text[-1] in SENTENCE_END_CHARS:
            continue
        last_para = text.split('\n\n')[-1].strip()
        if len(last_para.split()) > 20:
            truncated += 1
    truncation_rate = truncated / total

    # 2. Misplaced back_matter: back_matter chunks appearing before the last 10% of the book
    # PARENT rows are excluded here: they duplicate their children's text and
    # chunk_index range, so counting them would double-count the same
    # back-matter content and skew the threshold.
    non_parent_chunks = [c for c in chunks
                         if c.get('chunk_type', ChunkType.CONTENT) != ChunkType.PARENT]
    back_matter_chunks = [c for c in non_parent_chunks if c.get('section_type') in (SectionType.BACK_MATTER,)]
    early_back_matter = 0
    if back_matter_chunks and content_chunks:
        max_idx = max(c.get('chunk_index', 0) for c in non_parent_chunks)
        threshold = max_idx * 0.9
        early_back_matter = sum(1 for c in back_matter_chunks
                                if (c.get('chunk_index', 0)) < threshold)
    misplaced_back_rate = early_back_matter / total

    # 3. Chunk length variance (coefficient of variation) — lower is better
    word_counts = [len((c.get('text') or '').split()) for c in content_chunks]
    if word_counts:
        mean_wc = statistics.mean(word_counts)
        stdev_wc = statistics.stdev(word_counts) if len(word_counts) > 1 else 0
        cv = stdev_wc / mean_wc if mean_wc > 0 else 0
    else:
        cv = 1.0

    # 4. Very short chunks (< 50 words) — often garbage
    short_chunks = sum(1 for wc in word_counts if wc < 50)
    short_rate = short_chunks / total

    # 5. Section title coverage — higher is better
    has_section = sum(1 for c in content_chunks
                      if c.get('section_title') or c.get('chapter'))
    section_coverage = has_section / total

    # Composite score (0-100)
    # Each factor contributes to the score; weights reflect importance
    score = 100.0
    score -= truncation_rate * 30       # up to -30 for all chunks truncated
    score -= misplaced_back_rate * 25   # up to -25 for footnote pollution
    score -= min(cv, 1.0) * 10          # up to -10 for erratic chunk sizes
    score -= short_rate * 15            # up to -15 for many tiny chunks
    score += section_coverage * 10      # up to +10 bonus for section metadata
    # PDF bonus for page-level citations
    has_pages = any(c.get('page_number') or c.get('page_label') for c in content_chunks)
    if has_pages:
        score += 5                      # +5 bonus for page citations

    score = max(0, min(100, score))

    return {
        'score': round(score, 1),
        'chunk_count': len(chunks),
        'content_chunks': total,
        'truncation_rate': round(truncation_rate, 3),
        'misplaced_back_rate': round(misplaced_back_rate, 3),
        'cv': round(cv, 3),
        'short_rate': round(short_rate, 3),
        'section_coverage': round(section_coverage, 3),
        'has_pages': has_pages,
    }


def _select_best_format_by_quality(
    formats: List[Dict[str, str]],
    rag,
    book_id: str,
    prefer_format: str = 'PDF',
) -> tuple:
    """Prepare multiple formats, score them, return (best_format_entry, scores_dict).

    Falls back to static preference if only one format is available
    or if quality scoring fails.
    """
    # Filter to formats we can compare (PDF vs EPUB-like)
    comparable = [f for f in formats if f['format'] in ('PDF', 'EPUB', 'MOBI', 'AZW3')]
    if len(comparable) < 2:
        best = _select_best_format(formats, prefer_format)
        return best, {}

    import tempfile
    import io
    scores = {}
    for fmt_entry in comparable:
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                # Suppress prepare_book() output during quality scoring
                old_stdout = sys.stdout
                sys.stdout = io.StringIO()
                try:
                    result = rag.prepare_book(
                        fmt_entry['path'], book_id, output_dir=tmp_dir
                    )
                finally:
                    sys.stdout = old_stdout
                # Load the prepared chunks
                jsonl_files = list(Path(tmp_dir).glob('*.jsonl'))
                if not jsonl_files:
                    continue
                chunks = []
                with open(jsonl_files[0], 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        if rec.get('_header'):
                            continue
                        chunks.append(rec)
                scores[fmt_entry['format']] = _score_chunks(chunks)
                scores[fmt_entry['format']]['path'] = fmt_entry['path']
        except Exception as e:
            print(f"         ⚠  Quality check for {fmt_entry['format']} failed: {e}")

    if not scores:
        best = _select_best_format(formats, prefer_format)
        return best, {}

    # Pick format with highest score
    best_fmt = max(scores, key=lambda k: scores[k]['score'])
    best_entry = next(f for f in formats if f['format'] == best_fmt)
    return best_entry, scores


def _build_book_entry(row: sqlite3.Row, library_path: Path, include_rating: bool = False) -> Optional[Dict[str, Any]]:
    """Build a standardized book dictionary from a database row.

    Returns None if the book has no supported formats on disk.
    """
    book_path = library_path / row['path']
    formats = discover_formats(book_path)

    if not formats:
        return None

    bundle = bundle_master(library_path / '.archilles', str(row['id']))
    entry = {
        'id': row['id'],
        'title': row['title'],
        'author': row['author'] or 'Unknown',
        'path': str(book_path),
        'formats': formats,
        'best_format': _select_best_format(formats),
        'bundle': str(bundle) if bundle else None,
    }

    if include_rating:
        entry['rating'] = row['rating'] // 2 if row['rating'] else 0

    return entry


def get_books_by_tag(library_path: Path, tag_name: str, min_rating: int = 0, exclude_tags: List[str] = None, rating: Optional[int] = None, author_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Get all books with a specific tag from Calibre database.

    Args:
        library_path: Path to Calibre library
        tag_name: Tag to filter by (case-insensitive)
        min_rating: Minimum star rating (1-5, 0 = no filter). Mutually exclusive with rating.
        exclude_tags: List of tags to exclude (e.g., ['DeepL', 'Machine-translated'])
        rating: Exact star rating to filter by (0 = no rating / NULL, 1-5 = exact star count).
                Mutually exclusive with min_rating.
        author_filter: Optional list of author name fragments; only books where at least one
                       author matches ANY of the fragments (partial, case-insensitive) are
                       returned.  Can be combined with --tag, --all, or --author.

    Returns:
        List of book dictionaries with metadata and file paths
    """
    db_path = library_path / "metadata.db"

    if not db_path.exists():
        raise FileNotFoundError(f"Calibre database not found: {db_path}")

    conn = connect_readonly(db_path, row_factory=sqlite3.Row)

    # Build query with optional rating filter
    # Calibre stores rating in separate table (0-10 scale), we use 1-5 stars
    calibre_rating = min_rating * 2 if min_rating > 0 else 0

    query = """
    SELECT
        books.id,
        books.title,
        books.path,
        ratings.rating as rating,
        GROUP_CONCAT(authors.name, ' & ') as author
    FROM books
    INNER JOIN books_tags_link ON books.id = books_tags_link.book
    INNER JOIN tags ON books_tags_link.tag = tags.id
    LEFT JOIN books_authors_link ON books.id = books_authors_link.book
    LEFT JOIN authors ON books_authors_link.author = authors.id
    LEFT JOIN books_ratings_link ON books.id = books_ratings_link.book
    LEFT JOIN ratings ON books_ratings_link.rating = ratings.id
    WHERE LOWER(tags.name) = LOWER(?)
    """

    params = [tag_name]

    if calibre_rating > 0:
        query += " AND ratings.rating >= ?"
        params.append(calibre_rating)

    # Exact rating filter (--rating):
    # rating=0  → only books with no rating (Calibre stores NULL)
    # rating=N  → only books with exactly N stars (Calibre stores N*2 on a 0-10 scale)
    if rating is not None:
        if rating == 0:
            query += " AND ratings.rating IS NULL"
        else:
            query += " AND ratings.rating = ?"
            params.append(rating * 2)

    # Exclude books that have any of the excluded tags
    if exclude_tags:
        placeholders = ', '.join(['LOWER(?)' for _ in exclude_tags])
        query += f"""
        AND NOT EXISTS (
            SELECT 1 FROM books_tags_link btl_excl
            INNER JOIN tags t_excl ON btl_excl.tag = t_excl.id
            WHERE btl_excl.book = books.id
            AND LOWER(t_excl.name) IN ({placeholders})
        )
        """
        params.extend(exclude_tags)

    # Optional author filter: keep only books where at least one author matches
    # any of the given fragments (partial, case-insensitive).
    # Uses an EXISTS subquery so GROUP_CONCAT still shows all co-authors.
    if author_filter:
        or_clauses = ' OR '.join(['LOWER(af.name) LIKE LOWER(?)' for _ in author_filter])
        query += f"""
        AND EXISTS (
            SELECT 1 FROM books_authors_link bal_f
            INNER JOIN authors af ON bal_f.author = af.id
            WHERE bal_f.book = books.id
            AND ({or_clauses})
        )
        """
        params.extend([f'%{a}%' for a in author_filter])

    # Group by book to avoid duplicates when books have multiple authors
    query += " GROUP BY books.id"
    query += (
        " ORDER BY"
        " CASE WHEN ratings.rating >= 10 THEN 0"
        "      WHEN ratings.rating >= 8  THEN 1"
        "      WHEN ratings.rating >= 6  THEN 2"
        "      WHEN ratings.rating IS NULL THEN 3"
        "      WHEN ratings.rating >= 4  THEN 4"
        "      ELSE 5 END,"
        " author, books.title"
    )

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    books = []
    for row in rows:
        entry = _build_book_entry(row, library_path, include_rating=True)
        if entry:
            books.append(entry)

    conn.close()
    return books


def get_all_books(library_path: Path, author_filter: Optional[List[str]] = None, exclude_tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Get ALL books from Calibre database (no filtering).

    Uses GROUP_CONCAT to handle multi-author books correctly.

    Args:
        library_path: Path to Calibre library
        author_filter: Optional list of author name fragments; only books where at least one
                       author matches ANY of the fragments (partial, case-insensitive) are
                       returned.
        exclude_tags: List of tags to exclude (e.g., ['exclude', 'draft'])

    Returns:
        List of book dictionaries with metadata and file paths
    """
    db_path = library_path / "metadata.db"

    if not db_path.exists():
        raise FileNotFoundError(f"Calibre database not found: {db_path}")

    conn = connect_readonly(db_path, row_factory=sqlite3.Row)

    query = """
    SELECT
        books.id,
        books.title,
        books.path,
        GROUP_CONCAT(authors.name, ' & ') as author
    FROM books
    LEFT JOIN books_authors_link ON books.id = books_authors_link.book
    LEFT JOIN authors ON books_authors_link.author = authors.id
    LEFT JOIN books_ratings_link ON books.id = books_ratings_link.book
    LEFT JOIN ratings ON books_ratings_link.rating = ratings.id
    """

    params: List[Any] = []
    conditions = []

    if author_filter:
        or_clauses = ' OR '.join(['LOWER(af.name) LIKE LOWER(?)' for _ in author_filter])
        conditions.append(f"""EXISTS (
        SELECT 1 FROM books_authors_link bal_f
        INNER JOIN authors af ON bal_f.author = af.id
        WHERE bal_f.book = books.id AND ({or_clauses})
    )""")
        params.extend([f'%{a}%' for a in author_filter])

    if exclude_tags:
        placeholders = ', '.join(['LOWER(?)' for _ in exclude_tags])
        conditions.append(f"""NOT EXISTS (
        SELECT 1 FROM books_tags_link btl_excl
        INNER JOIN tags t_excl ON btl_excl.tag = t_excl.id
        WHERE btl_excl.book = books.id AND LOWER(t_excl.name) IN ({placeholders})
    )""")
        params.extend(exclude_tags)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += (
        " GROUP BY books.id ORDER BY"
        " CASE WHEN ratings.rating >= 10 THEN 0"
        "      WHEN ratings.rating >= 8  THEN 1"
        "      WHEN ratings.rating >= 6  THEN 2"
        "      WHEN ratings.rating IS NULL THEN 3"
        "      WHEN ratings.rating >= 4  THEN 4"
        "      ELSE 5 END,"
        " books.id"
    )

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    books = []
    for row in rows:
        entry = _build_book_entry(row, library_path)
        if entry:
            books.append(entry)

    conn.close()
    return books


def get_books_by_author(library_path: Path, author_name: str, min_rating: int = 0, rating: Optional[int] = None, exclude_tags: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Get all books by a specific author from Calibre database.

    Args:
        library_path: Path to Calibre library
        author_name: Author name to filter by (partial match, case-insensitive)
        min_rating: Minimum star rating (1-5, 0 = no filter). Mutually exclusive with rating.
        rating: Exact star rating to filter by (0 = no rating / NULL, 1-5 = exact star count).
                Mutually exclusive with min_rating.
        exclude_tags: List of tags to exclude (e.g., ['exclude', 'draft'])

    Returns:
        List of book dictionaries with metadata and file paths
    """
    db_path = library_path / "metadata.db"

    if not db_path.exists():
        raise FileNotFoundError(f"Calibre database not found: {db_path}")

    conn = connect_readonly(db_path, row_factory=sqlite3.Row)

    # First find all books that have the author we're looking for
    # Then get all authors for those books (to show co-authors)
    query = """
    SELECT
        books.id,
        books.title,
        books.path,
        ratings.rating as rating,
        GROUP_CONCAT(all_authors.name, ' & ') as author
    FROM books
    INNER JOIN books_authors_link bal ON books.id = bal.book
    INNER JOIN authors ON bal.author = authors.id
    LEFT JOIN books_authors_link all_bal ON books.id = all_bal.book
    LEFT JOIN authors all_authors ON all_bal.author = all_authors.id
    LEFT JOIN books_ratings_link ON books.id = books_ratings_link.book
    LEFT JOIN ratings ON books_ratings_link.rating = ratings.id
    WHERE LOWER(authors.name) LIKE LOWER(?)
    """

    params = [f'%{author_name}%']

    calibre_rating = min_rating * 2 if min_rating > 0 else 0
    if calibre_rating > 0:
        query += " AND ratings.rating >= ?"
        params.append(calibre_rating)

    if rating is not None:
        if rating == 0:
            query += " AND ratings.rating IS NULL"
        else:
            query += " AND ratings.rating = ?"
            params.append(rating * 2)

    if exclude_tags:
        placeholders = ', '.join(['LOWER(?)' for _ in exclude_tags])
        query += f"""
        AND NOT EXISTS (
            SELECT 1 FROM books_tags_link btl_excl
            INNER JOIN tags t_excl ON btl_excl.tag = t_excl.id
            WHERE btl_excl.book = books.id
            AND LOWER(t_excl.name) IN ({placeholders})
        )
        """
        params.extend(exclude_tags)

    query += (
        " GROUP BY books.id ORDER BY"
        " CASE WHEN ratings.rating >= 10 THEN 0"
        "      WHEN ratings.rating >= 8  THEN 1"
        "      WHEN ratings.rating >= 6  THEN 2"
        "      WHEN ratings.rating IS NULL THEN 3"
        "      WHEN ratings.rating >= 4  THEN 4"
        "      ELSE 5 END,"
        " books.title"
    )

    cursor = conn.execute(query, params)
    rows = cursor.fetchall()

    books = []
    for row in rows:
        entry = _build_book_entry(row, library_path, include_rating=True)
        if entry:
            books.append(entry)

    conn.close()
    return books


def get_books_by_ids(library_path: Path, calibre_ids: List[int]) -> List[Dict[str, Any]]:
    """
    Get specific books by their Calibre numeric IDs.

    Args:
        library_path: Path to Calibre library
        calibre_ids: List of Calibre book IDs (the 4-digit numbers shown in Calibre)

    Returns:
        List of book dictionaries with metadata and file paths
    """
    db_path = library_path / "metadata.db"

    if not db_path.exists():
        raise FileNotFoundError(f"Calibre database not found: {db_path}")

    conn = connect_readonly(db_path, row_factory=sqlite3.Row)

    placeholders = ', '.join(['?' for _ in calibre_ids])
    query = f"""
    SELECT
        books.id,
        books.title,
        books.path,
        GROUP_CONCAT(authors.name, ' & ') as author
    FROM books
    LEFT JOIN books_authors_link ON books.id = books_authors_link.book
    LEFT JOIN authors ON books_authors_link.author = authors.id
    WHERE books.id IN ({placeholders})
    GROUP BY books.id
    ORDER BY books.id
    """

    cursor = conn.execute(query, calibre_ids)
    rows = cursor.fetchall()
    conn.close()

    found_ids = {row['id'] for row in rows}
    missing = set(calibre_ids) - found_ids
    if missing:
        print(f"⚠️  IDs not found in Calibre: {', '.join(str(i) for i in sorted(missing))}")

    books = []
    for row in rows:
        entry = _build_book_entry(row, library_path)
        if entry:
            books.append(entry)
        else:
            print(f"⚠️  No supported format found for ID {row['id']}: {row['title']}")

    return books


def get_all_calibre_ids(library_path: Path) -> set:
    """
    Return the set of ALL Calibre book IDs (as strings) via direct SQLite.
    Legacy fallback used only when no adapter is available.
    """
    db_path = library_path / "metadata.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Calibre database not found: {db_path}")
    conn = connect_readonly(db_path)
    try:
        cursor = conn.execute("SELECT id FROM books")
        return {str(row[0]) for row in cursor.fetchall()}
    finally:
        conn.close()


def cleanup_orphans(
    rag: ArchillesRAG,
    library_path: Path,
    dry_run: bool = False,
    adapter=None,
    allow_large: bool = False,
) -> Dict[str, Any]:
    """
    Remove LanceDB entries for books that no longer exist in the library.

    Compares every book_id in the index against the full unfiltered library
    and deletes chunks for any book that has been removed.

    Uses the adapter (if provided) for library ID discovery so that
    non-Calibre backends (Folder, Obsidian, Zotero) are handled correctly.
    Falls back to direct SQLite access for legacy Calibre-only setups.

    Guarded, because this is the path the Lab routine runs daily, unattended
    and ``--non-interactive`` (review 1.1): the orphan set must pass
    ``check_orphan_bound``, an adapter that reports a partial scan blocks the
    deletion outright, and the doomed rows are written to Parquet before the
    delete — LanceDB's two-day version retention is shorter than the weekly
    mail that would surface a wrong deletion.

    Args:
        rag: Initialized ArchillesRAG instance
        library_path: Path to the library directory
        dry_run: If True, report orphans without deleting anything
        adapter: SourceAdapter instance (preferred over direct DB access)
        allow_large: Operator states an oversized deletion is intended
            (``--allow-large-orphan-cleanup``). Never overrides a failed scan.

    Returns:
        Dict with 'orphans_found', 'orphans_removed', 'bound' and, when rows
        were written, 'backup_path'.
    """
    print("\n🔍 Scanning for orphaned index entries...")

    if adapter is not None:
        docs = adapter.list_documents()
        library_ids = {doc.doc_id for doc in docs}
        print(f"   Library ({adapter.adapter_type}): {len(library_ids)} documents")
    else:
        library_ids = get_all_calibre_ids(library_path)
        print(f"   Calibre library: {len(library_ids)} books (legacy mode)")

    # alias for the rest of the function (was calibre_ids)
    calibre_ids = library_ids

    all_chunks = rag.store.get_book_ids_for_skip_check()
    indexed_ids = {chunk['book_id'] for chunk in all_chunks}
    print(f"   LanceDB index:   {len(indexed_ids)} books")

    orphan_ids = indexed_ids - calibre_ids

    if not orphan_ids:
        print("   ✅ No orphans found — index is clean")
        return {'orphans_found': 0, 'orphans_removed': 0}

    print(f"\n   ⚠️  Found {len(orphan_ids)} orphaned book(s):")
    for book_id in sorted(orphan_ids, key=lambda x: x.zfill(20)):
        chunks = rag.store.get_by_book_id(book_id, limit=1)
        title = chunks[0].get('book_title', '?') if chunks else '?'
        author = chunks[0].get('author', '?') if chunks else '?'
        print(f"      [{book_id}] {author}: {title}")

    bound = check_orphan_bound(
        orphan_count=len(orphan_ids),
        indexed_count=len(indexed_ids),
        allow_large=allow_large,
        scan_incomplete=bool(getattr(adapter, 'scan_incomplete', False)),
    )
    if not bound.allowed:
        print(f"\n   ⛔ {bound.reason}")
        print("      Nothing was deleted. The books listed above stay in the index.")
        return {
            'orphans_found': len(orphan_ids),
            'orphans_removed': 0,
            'refused': True,
            'bound': bound.as_dict(),
        }

    if dry_run:
        print(f"\n   ℹ️  DRY RUN — no deletions performed")
        return {
            'orphans_found': len(orphan_ids),
            'orphans_removed': 0,
            'bound': bound.as_dict(),
        }

    ordered = sorted(orphan_ids, key=lambda x: x.zfill(20))

    # Before the delete, never after: LanceDB keeps the superseded version for
    # two days, the weekly status mail is what would make a wrong deletion
    # visible, and the rows themselves cost megabytes.
    backup_dir = Path(rag.store.db_path).parent / "backups"
    backup_path = backup_orphan_chunks(rag.store, ordered, backup_dir)
    if backup_path is None:
        print("\n   ⛔ Could not back up the orphaned chunks — refusing to delete.")
        return {
            'orphans_found': len(orphan_ids),
            'orphans_removed': 0,
            'refused': True,
            'bound': bound.as_dict(),
        }
    print(f"\n   💾 Backed up orphaned chunks → {backup_path}")

    removed = 0
    for book_id in ordered:
        deleted = rag.store.delete_by_book_id(book_id)
        print(f"   🗑️  Deleted {deleted} chunks for book_id={book_id}")
        removed += 1

    print(f"\n   ✅ Cleanup complete: {removed} orphaned book(s) removed from index")
    print(f"      Rollback: {backup_path}")
    return {
        'orphans_found': len(orphan_ids),
        'orphans_removed': removed,
        'bound': bound.as_dict(),
        'backup_path': str(backup_path),
    }


def nominate_external_catchup(store) -> Tuple[set, set]:
    """Return ``(marked, derived)`` book_ids awaiting an external embed.

    ``marked`` carries the explicit ``pending_external`` flag. ``derived`` is
    finding 1.2's index-shape term: the marker alone is not the truth, because
    it is written only under ``mode: full-external`` — a title indexed under
    ``light`` carries no marker and would be invisible here, yet still waits
    for its hierarchical re-embed. The index knows it anyway: content chunks
    without PARENT/CHILD. That term is gated on the index actually holding
    hierarchical chunks, and the marked half is subtracted so the two sets are
    disjoint and countable on their own.

    Split out from :func:`discover_pending_external_books` so the size can be
    bounded before any book is resolved or prepared — the projected scan behind
    this reads every row's book_id and chunk_type and must not run twice.
    """
    marked = store.get_pending_external_book_ids()
    derived: set = set()
    if store.has_parent_chunks():
        derived = store.get_book_ids_without_parent_chunks() - marked
    return marked, derived


def discover_pending_external_books(
    rag, library_path: Path, adapter=None,
    nominated: Optional[Tuple[set, set]] = None,
) -> List[Dict[str, Any]]:
    """Find books marked ``pending_external`` and resolve them to book dicts.

    The discovery half of the full-external trickle lifecycle (Hardware-Tiers-V2
    §12): the watchdog indexes new titles *provisionally light* (flat, local) and
    marks them; this collects those books so they can be re-prepared hierarchically
    and embedded externally, replacing the provisional chunks.

    Calibre ids are numeric and resolved via ``metadata.db``. Non-Calibre adapters
    (finding 4.2) have non-numeric ids (Zotero keys, folder ids) — resolve those
    via the adapter's ``list_documents`` instead of dropping them. Any pending id
    that cannot be resolved is logged, never silently discarded (finding 2.7).
    ``nominated`` accepts an already-computed ``(marked, derived)`` pair so a
    caller that had to bound the set first (see
    :func:`archilles.external_catchup_guard.check_external_catchup_bound`)
    does not pay for the index scan twice.

    Returns book dicts ready for batch_prepare; an empty list short-circuits.
    """
    if nominated is None:
        nominated = nominate_external_catchup(rag.store)
    marked, derived = nominated
    pending = marked | derived

    if not pending:
        return []

    if derived:
        # The two halves have very different provenance, and the derived one
        # can be large. Say so before anything expensive starts.
        print(f"  ℹ️  {len(pending)} book(s) awaiting external embedding: "
              f"{len(marked)} marked pending_external, "
              f"{len(derived)} derived from the index (content chunks, no "
              f"PARENT/CHILD — indexed under a non-external mode).")

    use_adapter = adapter is not None and getattr(adapter, "adapter_type", "calibre") != "calibre"
    if use_adapter:
        # Resolve by matching the adapter's own ids against the pending set.
        all_books = _adapter_list_books(adapter)
        resolved = [b for b in all_books if str(b['id']) in pending]
        unresolved = pending - {str(b['id']) for b in resolved}
        if unresolved:
            print(f"  ⚠️  {len(unresolved)} pending_external id(s) could not be "
                  f"resolved via the {adapter.adapter_type} adapter (missing file / "
                  f"deleted item?): {sorted(unresolved)}")
        return resolved

    # Calibre path: numeric ids only.
    numeric_ids = sorted(int(b) for b in pending if str(b).isdigit())
    non_numeric = sorted(str(b) for b in pending if not str(b).isdigit())
    if non_numeric:
        print(f"  ⚠️  {len(non_numeric)} non-numeric pending_external id(s) skipped "
              f"(no adapter provided to resolve them): {non_numeric}")
    if not numeric_ids:
        return []
    return get_books_by_ids(library_path, numeric_ids)


def _adapter_list_books(
    adapter,
    tag_filter: Optional[str] = None,
    exclude_tags: Optional[List[str]] = None,
    author_filter: Optional[List[str]] = None,
    collection_filter: Optional[str] = None,
    item_type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List books via a SourceAdapter, returning dicts compatible with batch_index().

    This replaces the Calibre-specific get_all_books/get_books_by_tag/get_books_by_author
    for non-Calibre adapters.
    """
    docs = adapter.list_documents(tag_filter=tag_filter, collection_filter=collection_filter, item_type_filter=item_type_filter)

    # Apply exclude_tags (adapter only supports single exclude_tag)
    if exclude_tags:
        for etag in exclude_tags:
            docs = [d for d in docs if etag not in d.tags]

    # Apply author filter (partial, case-insensitive)
    if author_filter:
        def _author_matches(doc):
            for af in author_filter:
                af_lower = af.lower()
                for a in doc.authors:
                    if af_lower in a.lower():
                        return True
            return False
        docs = [d for d in docs if _author_matches(d)]

    books = []
    for doc in docs:
        # Skip documents without a file
        if not doc.file_path or not str(doc.file_path) or str(doc.file_path) == '.':
            continue
        if not doc.file_path.is_file():
            continue

        fmt = doc.file_format.upper() if doc.file_format else ""
        if not fmt:
            continue

        books.append({
            'id': doc.doc_id,
            'title': doc.title,
            'author': ' & '.join(doc.authors) if doc.authors else 'Unknown',
            'path': str(doc.file_path.parent),
            'formats': [{'format': fmt, 'path': str(doc.file_path)}],
            'best_format': {'format': fmt, 'path': str(doc.file_path)},
        })

    return books



def create_book_id(book: Dict[str, Any]) -> str:
    """
    Create a unique book ID for indexing.

    Uses the Calibre ID directly for easy cross-referencing.
    Example: "8127" (matches Calibre's "Id" column)
    """
    return str(book['id'])


def get_indexed_book_ids(
    rag: ArchillesRAG,
    reindex_before: datetime = None,
    reindex_missing_labels: bool = False,
    db_path: str = None,
    phase: str = 'phase2',
) -> set:
    """
    Get set of already indexed book IDs from RAG database.

    Args:
        rag: Initialized ArchillesRAG instance
        reindex_before: If provided, exclude books indexed before this date
                       (so they will be re-indexed)
        reindex_missing_labels: If True, exclude books with missing page labels
                               (so they will be re-indexed)
        db_path: Path to LanceDB database (for missing labels check)
        phase: 'phase1' counts ANY chunk as indexed (skip existing stubs);
               'phase2' (default) counts only CONTENT chunks, so phase1 stubs
               are re-processed into full text.

    Returns:
        Set of book_id strings that are already in the index
        (excluding books that should be re-indexed)
    """
    # Get books with missing labels if requested
    books_missing_labels = set()
    if reindex_missing_labels and db_path:
        print("🔍 Checking for books with missing page labels...")
        missing = find_books_missing_labels(db_path)
        books_missing_labels = set(missing.keys())
        if books_missing_labels:
            print(f"   Found {len(books_missing_labels)} books needing page label re-indexing")
        else:
            print("   All books have proper page labels!")

    try:
        # Load only the minimal columns required (book_id, chunk_type, indexed_at).
        # This avoids reading the large 'text' and 'vector' columns and is 10-50x
        # faster than loading the full table on large databases.
        all_chunks = rag.store.get_book_ids_for_skip_check()

        # Extract unique book_ids — only count books with actual CONTENT chunks
        # (not just phase1_metadata or calibre_comment)
        content_types = ChunkType.HIERARCHICAL_TYPES
        book_ids = set()
        for chunk in all_chunks:
            if not chunk or 'book_id' not in chunk:
                continue

            # Only count content chunks as "fully indexed" (except in phase1 where
            # any chunk — including phase1_metadata stubs — counts as done)
            chunk_type = chunk.get('chunk_type', ChunkType.CONTENT)
            if phase != 'phase1' and chunk_type not in content_types:
                continue

            book_id = chunk['book_id']

            # Check if this book should be re-indexed due to missing labels
            if reindex_missing_labels and book_id in books_missing_labels:
                continue  # Don't add to set (will be re-indexed)

            # Check if this book should be re-indexed based on date
            if reindex_before:
                indexed_at_str = chunk.get('indexed_at')
                if indexed_at_str:
                    try:
                        indexed_at = datetime.fromisoformat(indexed_at_str)
                        if indexed_at < reindex_before:
                            continue
                    except (ValueError, AttributeError):
                        continue
                else:
                    continue

            book_ids.add(book_id)

        return book_ids
    except Exception as e:
        print(f"⚠️  Could not read existing index: {e}")
        return set()


def batch_reindex_comments(
    books: List[Dict[str, Any]],
    rag: ArchillesRAG,
    dry_run: bool = False,
    log_file: Optional[Path] = None,
    checkpoint_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Re-index only calibre_comment chunks for the given books.

    Fast: reads Calibre metadata (no PDF/EPUB extraction, no new embeddings
    for content chunks). Deletes existing comment chunks and rebuilds them
    using the structured HTML parser (H1/H2 split, bold/H3/H4 as key_passages).

    Use after editing Calibre comments or after upgrading the comment indexing logic.

    Supports resuming an interrupted run via checkpoint_path. Each completed or
    skipped book_id is persisted to the IndexingCheckpoint immediately; the
    checkpoint is removed automatically when the run finishes with no failures.
    """
    import gc
    import numpy as np

    # ── Checkpoint setup ──────────────────────────────────────────
    if checkpoint_path is None:
        checkpoint_path = Path('.archilles_reindex_checkpoint.json')

    if not dry_run:
        all_ids = [create_book_id(b) for b in books]
        cp = IndexingCheckpoint.load_or_create(checkpoint_path, profile="", book_ids=all_ids)
        done_ids: set = set(cp.completed_books) | set(cp.skipped_books)
        if done_ids:
            print(f"  ↩  Resuming — {len(done_ids)} books already done (checkpoint: {checkpoint_path})")
    else:
        done_ids = set()

    stats = {'updated': 0, 'skipped': 0, 'failed': 0, 'errors': []}

    remaining = [b for b in books if create_book_id(b) not in done_ids]
    total = len(books)

    print(f"\n{'='*60}")
    print(f"  Re-indexing comment chunks only (no full-text re-extraction)")
    print(f"  Books total: {total}  |  Remaining: {len(remaining)}")
    if dry_run:
        print(f"  Mode: DRY RUN")
    print(f"{'='*60}\n")

    offset = total - len(remaining)  # for display numbering

    for i, book in enumerate(remaining, 1):
        book_id = create_book_id(book)
        file_info = book.get('best_format', {})
        book_path = Path(file_info.get('path', ''))
        print(f"[{offset + i}/{total}] {book['author']}: {book['title']}")

        if dry_run:
            print(f"         [dry-run] would rebuild comment chunks")
            stats['updated'] += 1
            continue

        if not book_path.exists():
            print(f"         ⚠️  File not found: {book_path}")
            stats['skipped'] += 1
            cp.skip_book(book_id)
            continue

        try:
            # Only read Calibre DB — skip PDF/EPUB file extraction entirely
            book_metadata = rag._extract_calibre_metadata(book_path)
            has_comments = bool(
                book_metadata.get('comments') or book_metadata.get('comments_html')
            )
            if not has_comments:
                print(f"         — No comments, skipping")
                stats['skipped'] += 1
                cp.skip_book(book_id)
                continue

            deleted = rag.store.delete_by_book_id_and_type(book_id, ChunkType.CALIBRE_COMMENT)
            meta_hash = rag._compute_metadata_hash(book_metadata)
            book_format = file_info.get('format', '').lower()

            comment_chunks, comment_embeddings = rag._build_comment_chunks(
                book_metadata=book_metadata,
                book_id=book_id,
                book_format=book_format,
                metadata_hash=meta_hash,
            )

            if comment_chunks:
                rag.store.add_chunks(comment_chunks, np.array(comment_embeddings))
                print(f"         ✓ {deleted} old → {len(comment_chunks)} new chunk(s)")
                stats['updated'] += 1
            else:
                print(f"         — No comment content after parsing")
                stats['skipped'] += 1

            cp.complete_book(book_id)

        except Exception as e:
            print(f"         ❌ {e}")
            stats['failed'] += 1
            stats['errors'].append({'title': book['title'], 'error': str(e)})

        if i % 20 == 0:
            gc.collect()
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    print(f"\n{'='*60}")
    print(f"  Updated: {stats['updated']}")
    print(f"  Skipped: {stats['skipped']}")
    print(f"  Failed:  {stats['failed']}")
    print(f"{'='*60}\n")

    if not dry_run and stats['failed'] == 0:
        cp.delete()
        print(f"  ✓ Checkpoint removed (run complete)")

    if log_file:
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"📝 Log written to: {log_file}")

    return stats


def batch_prepare(
    books: List[Dict[str, Any]],
    rag: ArchillesRAG,
    output_dir: str = './prepared_chunks',
    dry_run: bool = False,
    prefer_format: str = 'PDF',
    quality_select: bool = False,
) -> Dict[str, Any]:
    """
    Prepare multiple books (extract + chunk, no embedding).
    Writes one JSONL per book to output_dir.
    """
    stats = {
        'total': len(books),
        'prepared': 0,
        'skipped': 0,
        'failed': 0,
        'quality_selected': 0,
        'errors': [],
        'needs_ocr': [],
    }

    print(f"\n{'='*60}")
    print(f"  ARCHILLES BATCH PREPARE (no embedding)")
    print(f"{'='*60}")
    print(f"  Books to process: {len(books)}")
    print(f"  Output directory: {output_dir}")
    if quality_select:
        print(f"  Quality selection: ENABLED (multi-format books will be compared)")
    print(f"  Mode: {'DRY RUN' if dry_run else 'PREPARING'}")
    print(f"{'='*60}\n")

    for i, book in enumerate(books, 1):
        book_id = create_book_id(book)
        # Print the progress header exactly once per book (finding 3.2); the
        # skip / quality / format branches below only add their detail line.
        print(f"\n[{i}/{len(books)}] {book['author']}: {book['title']}")

        # Skip-existing check BEFORE quality comparison (avoid expensive
        # re-prepare). Must use the same book_id-based name prepare_book
        # writes (see prepared_jsonl_name, finding 5.1).
        if not dry_run:
            existing_jsonl = Path(output_dir) / prepared_jsonl_name(book_id)
            if existing_jsonl.exists():
                header = read_prepared_header(existing_jsonl)
                if header is not None:
                    stats['skipped'] += 1
                    print(f"         Already prepared ({header.get('chunk_count', '?')} chunks). Skipping.")
                    continue
                # Invalid header or a chunk-count mismatch (interrupted
                # prepare) — fall through and re-prepare.

        # Quality-based format selection for multi-format books. Not for a
        # book with a bundle: every candidate would be read from the bundle,
        # which passed its checks when it was made.
        has_multiple = (quality_select and len(book['formats']) > 1
                        and not book.get('bundle'))
        if has_multiple and not dry_run:
            best, scores = _select_best_format_by_quality(
                book['formats'], rag, book_id, prefer_format
            )
            if scores:
                stats['quality_selected'] += 1
                for fmt, s in scores.items():
                    marker = " ✓" if fmt == best['format'] else ""
                    print(f"         {fmt}: score={s['score']}"
                          f" (trunc={s.get('truncation_rate', '?')}, back={s.get('misplaced_back_rate', '?')}"
                          f", cv={s.get('cv', '?')}, short={s.get('short_rate', '?')}){marker}")
                print(f"         → Selected: {best['format']} | ID: {book_id}")
            else:
                print(f"         Format: {best['format']} | ID: {book_id}")
        else:
            best = _select_best_format(book['formats'], prefer_format)
            print(f"         Format: {_format_label(book, best)} | ID: {book_id}")

        if dry_run:
            print(f"         Would prepare: {best['path']}")
            continue

        try:
            result = rag.prepare_book(best['path'], book_id, output_dir=output_dir)
            if result.get('status') == 'already_prepared':
                stats['skipped'] += 1
            else:
                stats['prepared'] += 1
            if result.get('needs_ocr'):
                stats['needs_ocr'].append({'id': book_id, 'title': book['title']})
        except Exception as e:
            print(f"         FAILED: {e}")
            stats['failed'] += 1
            stats['errors'].append({'book_id': book_id, 'error': str(e)})

    print(f"\n{'='*60}")
    print(f"  Prepared: {stats['prepared']}, Skipped: {stats['skipped']}, Failed: {stats['failed']}")
    if stats['needs_ocr']:
        print(f"\n  🔍 Scanned PDFs (no/low text — OCR with PDF24, then re-prepare):")
        for b in stats['needs_ocr']:
            print(f"     - [{b['id']}] {b['title']}")
    print(f"{'='*60}\n")
    return stats


def batch_index(
    books: List[Dict[str, Any]],
    rag: ArchillesRAG,
    dry_run: bool = False,
    skip_existing: bool = False,
    reindex_before: datetime = None,
    reindex_missing_labels: bool = False,
    force: bool = False,
    log_file: Optional[Path] = None,
    safe_indexer: Optional[SafeIndexer] = None,
    phase: str = 'phase2',
    db_path: str = None,
    prefer_format: str = 'PDF',
    quality_select: bool = False,
) -> Dict[str, Any]:
    """
    Index multiple books into the RAG database.

    Args:
        books: List of book dictionaries from get_books_by_tag/author
        rag: Initialized ArchillesRAG instance
        dry_run: If True, only show what would be indexed
        skip_existing: If True, skip books that are already indexed
        reindex_before: If provided, re-index books indexed before this date
        reindex_missing_labels: If True, re-index books with missing page labels
        log_file: Optional path to write detailed log
        safe_indexer: Optional SafeIndexer for crash-safety and progress tracking
        phase: 'phase1' (metadata only) or 'phase2' (full content)
        db_path: Path to LanceDB database

    Returns:
        Dictionary with indexing statistics

    Note on very large files:
        For extremely large PDFs (>100 MB of dense text, e.g. 1500+ pages),
        indexing can take several hours depending on hardware. There is no
        timeout by design — the indexer will work through large files given
        enough time. If a book appears to hang indefinitely, move the
        problematic file temporarily into a subfolder (e.g. ``data/``) within
        the Calibre book directory so that a smaller format (EPUB) is picked
        up instead. After the batch run completes, move the file back and
        re-index that single book with ``--force``.
    """
    stats = {
        'total': len(books),
        'indexed': 0,
        'skipped': 0,
        'failed': 0,
        'errors': [],
        'needs_ocr': [],
        'start_time': datetime.now().isoformat(),
        'books_processed': []
    }

    # Get already indexed books if skip_existing is enabled
    # If reindex_before is set, exclude old books from the existing set
    # Check LanceDB for already-indexed books
    should_check = skip_existing or reindex_before or reindex_missing_labels
    existing_ids = get_indexed_book_ids(
        rag, reindex_before, reindex_missing_labels, db_path, phase=phase
    ) if should_check else set()

    # When --skip-existing is set (without reindex flags), pre-filter the book
    # list so already-indexed books are never passed to index_book() at all.
    # This lets the loop jump directly to unindexed books rather than checking
    # every single book via a LanceDB read.
    if skip_existing and existing_ids and not reindex_before and not reindex_missing_labels:
        books = [b for b in books if create_book_id(b) not in existing_ids]

    print(f"\n{'='*60}")
    print(f"📚 ARCHILLES BATCH INDEXER")
    print(f"{'='*60}")
    print(f"  Books to process: {len(books)}")
    if skip_existing:
        print(f"  Already indexed (skipped): {len(existing_ids)}")
    if reindex_before:
        reindex_count = len(books) - len(existing_ids)
        print(f"  📅 Re-indexing books indexed before: {reindex_before.strftime('%Y-%m-%d')}")
        print(f"     → {reindex_count} books will be re-indexed")
    if reindex_missing_labels:
        print(f"  📄 Re-indexing books with missing page labels")
    print(f"  Mode: {'DRY RUN' if dry_run else 'INDEXING'}")
    print(f"{'='*60}\n")

    for i, book in enumerate(books, 1):
        # Check for shutdown request
        if safe_indexer and safe_indexer.should_shutdown():
            print(f"\n⏸️  Shutdown requested - stopping after {stats['indexed']} books")
            break

        book_id = create_book_id(book)

        # Format selection: quality-based or static preference (a book with a
        # bundle is not compared -- see batch_prepare)
        has_multiple = (quality_select and len(book['formats']) > 1
                        and not book.get('bundle'))
        if has_multiple:
            best, scores = _select_best_format_by_quality(
                book['formats'], rag, book_id, prefer_format
            )
            if scores:
                print(f"\n[{i}/{len(books)}] {book['author']}: {book['title']}")
                for fmt, s in scores.items():
                    marker = " ✓" if fmt == best['format'] else ""
                    print(f"         {fmt}: score={s['score']}"
                          f" (trunc={s['truncation_rate']}, back={s['misplaced_back_rate']}"
                          f", cv={s['cv']}, short={s['short_rate']}){marker}")
                print(f"         → Selected: {best['format']} | ID: {book_id}")
            else:
                print(f"\n[{i}/{len(books)}] {book['author']}: {book['title']}")
                print(f"         Format: {best['format']} | ID: {book_id}")
        else:
            best = _select_best_format(book['formats'], prefer_format)
            print(f"\n[{i}/{len(books)}] {book['author']}: {book['title']}")
            print(f"         Format: {_format_label(book, best)} | ID: {book_id}")

        ordered_formats = [best] + [f for f in book['formats'] if f['path'] != best['path']]

        file_path = best['path']
        format_type = best['format']

        should_force = force or reindex_before is not None or reindex_missing_labels

        if dry_run:
            print(f"         🔍 Would index: {file_path}")
            stats['books_processed'].append({
                'id': book_id,
                'title': book['title'],
                'status': 'dry_run'
            })
            continue

        # Try each format in order, falling back on extraction failure
        result = None
        elapsed = 0.0
        last_exc = None

        for fmt_entry in ordered_formats:
            try:
                start_time = time.time()
                result = rag.index_book(fmt_entry['path'], book_id, force=should_force, phase=phase)
                elapsed = time.time() - start_time
                file_path = fmt_entry['path']
                format_type = fmt_entry['format']
                last_exc = None
                break  # success — stop trying further formats
            except Exception as e:
                last_exc = e
                remaining = ordered_formats[ordered_formats.index(fmt_entry) + 1:]
                if remaining:
                    print(f"         ⚠️  {fmt_entry['format']} failed ({e}), trying {remaining[0]['format']}...")

        # All formats exhausted without success
        if last_exc is not None:
            error_msg = str(last_exc)
            tried = ', '.join(f['format'] for f in ordered_formats)
            print(f"         ❌ FAILED (tried: {tried}): {error_msg}")

            stats['failed'] += 1
            stats['errors'].append({
                'book_id': book_id,
                'title': book['title'],
                'error': error_msg,
            })
            stats['books_processed'].append({
                'id': book_id,
                'title': book['title'],
                'status': 'failed',
                'error': error_msg,
            })
            continue

        # Handle already-indexed books (from LanceDB check)
        if result.get('status') == 'already_indexed':
            print(f"         ⏭️  SKIPPED (already in LanceDB: {result['chunks_indexed']} chunks)")
            stats['skipped'] += 1
            stats['books_processed'].append({
                'id': book_id,
                'title': book['title'],
                'status': 'skipped',
                'reason': 'already in LanceDB'
            })
            continue

        # Handle metadata-only updates (no full re-index needed)
        if result.get('status') == 'metadata_updated':
            print(f"         📝 Metadata updated in {result.get('total_time', 0):.1f}s (content unchanged)")
            stats['indexed'] += 1
            stats['books_processed'].append({
                'id': book_id,
                'title': book['title'],
                'status': 'metadata_updated',
                'time': result.get('total_time', 0)
            })
            continue

        # Regular indexing completed
        if should_force:
            print(f"         ♻️  Re-indexed {result['chunks_indexed']} chunks in {elapsed:.1f}s")
        else:
            print(f"         ✅ Indexed {result['chunks_indexed']} chunks in {elapsed:.1f}s")
        if format_type != best['format']:
            print(f"         ↩️  Used fallback format: {format_type} (primary {best['format']} failed)")

        stats['indexed'] += 1
        if result.get('needs_ocr'):
            stats['needs_ocr'].append({'id': book_id, 'title': book['title']})
        stats['books_processed'].append({
            'id': book_id,
            'title': book['title'],
            'status': 'success',
            'chunks': result['chunks_indexed'],
            'time': elapsed
        })

    # Final summary
    stats['end_time'] = datetime.now().isoformat()

    print(f"\n{'='*60}")
    print(f"📊 INDEXING COMPLETE")
    print(f"{'='*60}")
    print(f"  Total books:     {stats['total']}")
    print(f"  Successfully indexed: {stats['indexed']}")
    print(f"  Skipped:         {stats['skipped']}")
    print(f"  Failed:          {stats['failed']}")

    if stats['errors']:
        print(f"\n⚠️  Errors:")
        for err in stats['errors']:
            print(f"    - {err['title']}: {err['error'][:50]}...")

    if stats['needs_ocr']:
        print(f"\n🔍 Scanned PDFs (no text extracted — re-index with --enable-ocr):")
        for book in stats['needs_ocr']:
            print(f"    - [{book['id']}] {book['title']}")

    print(f"{'='*60}\n")

    # Write log file if specified
    if log_file:
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
        print(f"📝 Log written to: {log_file}")

    return stats


def build_parser() -> argparse.ArgumentParser:
    """Build the batch_index argument parser.

    Extracted from main() so the CLI defaults are unit-testable.
    """
    parser = argparse.ArgumentParser(
        description="Batch index books from Calibre library into ARCHILLES RAG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Index ALL books in the library (no filter required)
  python scripts/batch_index.py --all

  # Use a specific hardware profile (minimal/balanced/maximal)
  python scripts/batch_index.py --all --profile minimal
  python scripts/batch_index.py --all --profile balanced

  # Index all books tagged "must-read"
  python scripts/batch_index.py --tag "must-read"

  # Dry run (preview without indexing)
  python scripts/batch_index.py --tag "must-read" --dry-run

  # Index only first 5 books (for testing)
  python scripts/batch_index.py --tag "must-read" --limit 5

  # Skip already indexed books
  python scripts/batch_index.py --tag "must-read" --skip-existing

  # Index all books by an author
  python scripts/batch_index.py --author "Arendt"

  # Recover from corrupted database (after CTRL+C)
  python scripts/batch_index.py --tag "must-read" --reset-db

  # Re-index old books (e.g., indexed before Dec 1st with old code)
  python scripts/batch_index.py --tag "must-read" --reindex-before 2024-12-01

  # Exclude drafts and custom-tagged books
  python scripts/batch_index.py --tag "must-read" --exclude-tag "draft" --exclude-tag "DeepL"

  # Filter by minimum rating (1-5 stars, inclusive)
  python scripts/batch_index.py --tag "must-read" --min-rating 4

  # Filter by exact rating (0 = no rating / NULL in Calibre)
  python scripts/batch_index.py --tag "must-read" --rating 0   # only unrated
  python scripts/batch_index.py --tag "must-read" --rating 5   # only 5-star books

  # Filter by author(s) — combinable with --tag, --all, or --author (OR logic)
  python scripts/batch_index.py --tag "must-read" --filter-author "Arendt"
  python scripts/batch_index.py --tag "must-read" --rating 0 --filter-author "Arendt" --filter-author "Benjamin"
  python scripts/batch_index.py --all --filter-author "Tucholsky"

Profiles:
  minimal  - CPU-only, resource-efficient (laptops, <6GB VRAM)
  balanced - GPU-accelerated, good quality (6-12GB VRAM)
  maximal  - Full GPU, maximum quality (>12GB VRAM)
        """
    )

    # Selection criteria (mutually exclusive; not required when --cleanup-orphans is used alone)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--all', action='store_true',
                       help='Index ALL books in the library (no tag/author filter)')
    group.add_argument('--tag', help='Index books with this tag')
    group.add_argument('--author', help='Index books by this author (partial match)')
    group.add_argument('--ids', metavar='ID[,ID,...]',
                       help='Comma-separated Calibre book IDs (e.g. --ids 1234,5678,9012). '
                            'Use --force to re-index already indexed books.')
    group.add_argument('--collection', metavar='NAME',
                       help='Index items in this Zotero collection (and all sub-collections). '
                            'Zotero libraries only.')

    # Rating filters (mutually exclusive)
    rating_group = parser.add_mutually_exclusive_group()
    rating_group.add_argument('--min-rating', type=int, choices=[1, 2, 3, 4, 5], default=None,
                              help='Minimum star rating (1-5, inclusive)')
    rating_group.add_argument('--rating', type=int, choices=[0, 1, 2, 3, 4, 5],
                              help='Exact star rating to filter by. '
                                   '0 = only books without any rating (Calibre: NULL); '
                                   '1-5 = only books with exactly that many stars.')

    # Exclude filter (can be used multiple times)
    parser.add_argument('--exclude-tag', action='append', dest='exclude_tags', metavar='TAG',
                        help='Exclude books with this tag (can be used multiple times). '
                             'Added on top of the default excludes.')
    parser.add_argument('--include-excluded', action='store_true',
                        help='Override the configured tag exclusions '
                             '(see .archilles/config.json → excluded_tags, '
                             f'default: {", ".join(DEFAULT_EXCLUDED_TAGS)}) '
                             'and include those books. User-specified '
                             '--exclude-tag values still apply.')

    # Author filter — additional, not mutually exclusive with --tag / --all / --author
    parser.add_argument('--filter-author', action='append', dest='filter_authors', metavar='AUTHOR',
                        help='Only include books where at least one author matches this fragment '
                             '(partial, case-insensitive). Can be repeated for multiple authors '
                             '(OR logic). Works alongside --tag, --all, and --author. '
                             'Example: --filter-author "Arendt" --filter-author "Benjamin"')
    parser.add_argument('--type', dest='item_type', metavar='TYPE',
                        help='Filter by Zotero item type (case-insensitive). '
                             'Zotero libraries only. '
                             'Examples: book, journalArticle, thesis, report, conferencePaper')

    # Options
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be indexed without actually indexing')
    parser.add_argument('--force', action='store_true',
                        help='Force re-indexing of ALL books (delete old chunks first). '
                             'Use after extractor upgrades (e.g. pdfplumber → PyMuPDF).')
    parser.add_argument('--skip-existing', action='store_true',
                        help='Skip books that are already in the index')
    parser.add_argument('--reindex-before', metavar='DATE',
                        help='Re-index books indexed before this date (YYYY-MM-DD). '
                             'Useful for re-indexing old books with improved code.')
    parser.add_argument('--reindex-missing-labels', action='store_true',
                        help='Re-index books with missing page labels (indexed before '
                             'page label extraction was implemented)')
    parser.add_argument('--reindex-comments', action='store_true',
                        help='Re-index only calibre_comment chunks (no full-text re-extraction). '
                             'Use after editing Calibre comments or upgrading comment indexing.')
    parser.add_argument('--start-after', metavar='AUTHOR',
                        help='Skip all books until an author matching this fragment is found '
                             '(case-insensitive, last match wins across rating tiers). E.g. "Bauman".')
    parser.add_argument('--skip', type=int, metavar='N',
                        help='Skip the first N books (use displayed position minus 1 to resume).')
    parser.add_argument('--limit', type=int,
                        help='Limit number of books to index (for testing)')
    parser.add_argument('--log', metavar='FILE',
                        help='Write detailed log to JSON file')
    parser.add_argument('--db-path', default=None,
                        help='RAG database path (default: CALIBRE_LIBRARY/.archilles/rag_db)')
    parser.add_argument('--reset-db', action='store_true',
                        help='Reset corrupted database (WARNING: deletes all indexed data)')
    parser.add_argument('--phase1-only', action='store_true',
                        help='Phase 1: Quick indexing of metadata, comments, and annotations only (5-10 min)')
    parser.add_argument('--non-interactive', action='store_true',
                        help='Run in non-interactive mode (auto-resume sessions, no prompts)')
    parser.add_argument('--enable-ocr', action='store_true',
                        help='Enable OCR for scanned PDFs (auto-detect)')
    parser.add_argument('--force-ocr', action='store_true',
                        help='Force OCR even for digital PDFs')
    parser.add_argument('--mode', choices=['auto', 'light', 'full-local', 'full-external'],
                        default=None,
                        help='Hardware-Tiers-V2 mode (overrides .archilles/config.json "mode"; '
                             'default: auto). auto picks the path from detected hardware: '
                             'capable GPU/Apple Silicon -> full-local (hierarchical, local), '
                             'weak hardware -> light (flat, local, free). full-external prepares '
                             'locally and embeds via the separate two-phase flow.')
    parser.add_argument('--profile', choices=['minimal', 'balanced', 'maximal'],
                        help='Advanced override: pin a legacy hardware profile '
                             '(minimal/balanced/maximal), bypassing --mode/auto detection.')
    parser.add_argument('--show-profiles', action='store_true',
                        help='Show available profiles and exit')
    parser.add_argument('--hierarchical', action='store_true',
                        help='Enable parent-child chunking (parents ~2048, children ~512 tokens)')
    parser.add_argument('--use-modular-pipeline', action='store_true',
                        help='EXPERIMENTAL - not integrated with ExecutionPlan/hierarchical/languages, '
                             'loads a second embedder, and skips metadata_hash/annotation chunks (invisible '
                             'to watchdog change detection). Use ModularPipeline architecture '
                             '(parser -> chunker -> embedder).')
    parser.add_argument('--prefer-format', choices=['pdf', 'epub', 'mobi', 'azw3'],
                        default='pdf',
                        help='Preferred file format when a book has multiple formats '
                             '(default: pdf). Use "epub" to prefer EPUB over PDF — '
                             'faster indexing, no page-number citations.')
    parser.add_argument('--cleanup-orphans', action='store_true',
                        help='Remove index entries for books deleted from Calibre. '
                             'Can be used standalone or combined with an indexing run. '
                             'Use --dry-run to preview orphans without deleting.')
    parser.add_argument('--allow-large-orphan-cleanup', action='store_true',
                        help='Authorise an orphan cleanup that exceeds the safety bound '
                             f'(>{int(ORPHAN_SHARE_LIMIT * 100)}%% of the index AND '
                             f'>{ORPHAN_COUNT_LIMIT} books). For deliberate bulk deletions; '
                             'a scan that reported errors is still refused.')
    parser.add_argument('--allow-large-external-catchup', action='store_true',
                        help='Authorise an external catch-up that exceeds the safety '
                             f'bound (>{DERIVED_COUNT_LIMIT} books derived from the index '
                             'shape rather than carrying the pending_external marker). '
                             'For a deliberate bulk catch-up; the marked half is never '
                             'bounded.')
    parser.add_argument('--prepare-pending-external', action='store_true',
                        help='Discover books indexed provisionally light (full-external '
                             'mode, marked pending_external) and re-prepare them '
                             'hierarchically into --output-dir. Standalone operation; '
                             'follow with `rag_demo.py embed --mode remote` to replace '
                             'the provisional flat chunks with externally embedded ones.')
    parser.add_argument('--quality-select', action='store_true',
                        help='When a book has multiple formats (e.g. PDF+EPUB), '
                             'prepare both, compare chunk quality, and pick the better one. '
                             'Adds a few seconds per multi-format book.')
    parser.add_argument('--prepare-only', action='store_true',
                        help='Extract and chunk books without embedding (Phase 1 of two-phase indexing). '
                             'Writes JSONL files to --output-dir. No GPU required.')
    parser.add_argument('--output-dir', default='./prepared_chunks',
                        help='Output directory for --prepare-only JSONL files (default: ./prepared_chunks). '
                             'For --prepare-pending-external, use a dedicated (smaller) directory rather than '
                             "sharing it with a large corpus prepare run — the trickle's embed checkpoint "
                             're-scans every file in --input-dir on each run.')
    parser.add_argument('--prepare-chunk-size', type=int, default=None, metavar='TOKENS',
                        help='Chunk size in tokens for --prepare-only '
                             '(default: from the index recipe, 512). '
                             'Larger values = fewer chunks, less DB volume, slightly broader retrieval. '
                             'Has no effect on the live --skip-existing path.')
    parser.add_argument('--prepare-overlap', type=int, default=None, metavar='TOKENS',
                        help='Chunk overlap in tokens for --prepare-only '
                             '(default: from the index recipe, 64). '
                             'Has no effect on the live --skip-existing path.')

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Handle --show-profiles
    if args.show_profiles:
        list_profiles()
        sys.exit(0)

    # Validate: require --all/--tag/--author/--ids unless --cleanup-orphans is the sole operation
    if (not args.all and not args.tag and not args.author and not args.ids
            and not args.collection and not args.cleanup_orphans
            and not args.prepare_pending_external):
        parser.error("one of the arguments --all --tag --author --collection "
                     "--cleanup-orphans --prepare-pending-external is required")

    # Parse reindex-before date if specified
    reindex_before = None
    if args.reindex_before:
        try:
            reindex_before = datetime.strptime(args.reindex_before, '%Y-%m-%d')
        except ValueError:
            print(f"❌ Invalid date format: {args.reindex_before}")
            print("   Use YYYY-MM-DD format (e.g., 2024-12-01)")
            sys.exit(1)

    # Get library path and create adapter
    library_path = get_library_path()
    adapter = None
    try:
        from src.adapters import create_adapter
        adapter = create_adapter(library_path)
        print(f"📚 Library: {library_path} (adapter: {adapter.adapter_type})")
    except Exception:
        print(f"📚 Library: {library_path} (no adapter — legacy mode)")

    # Determine the execution plan (Hardware-Tiers-V2 §7/§10.3): a single `mode`
    # variable (CLI over config.json, default auto) feeds plan() with the detected
    # hardware. auto picks the path from the hardware class; `--profile` stays as a
    # legacy advanced override. No interactive profile prompt anymore — the normal
    # user makes zero-to-one decision and the machine does the rest.
    resolution = resolve_indexing_plan(
        mode_cli=args.mode,
        mode_config=get_mode(get_library_path(required=False)),
        profile_override=args.profile,
        hierarchical_flag=args.hierarchical,
        prepare_only_flag=getattr(args, 'prepare_only', False),
        hw=detect_hardware(),
        recipe=default_recipe(),
    )

    if resolution.profile_name:
        profile = get_profile(resolution.profile_name)
        print(f"⚙️  Profile (advanced override): {resolution.profile_name.upper()}")
        print(f"    Model: {profile.embedding_model}    Device: {profile.embedding_device}")
    else:
        ep = resolution.execution_plan
        print(f"⚙️  Mode: {resolution.resolved_mode}  →  hardware class: {ep.hardware_class}, "
              f"resolved: {ep.mode}")
        print(f"    Embedding device: {ep.embedding_device}    Batch: {ep.batch_size}    "
              f"Hierarchical: {resolution.hierarchical}    Reranker: {ep.rerank_device}")
        if resolution.prepare_only and not getattr(args, 'prepare_only', False):
            print("    ℹ️  full-external has no local-embedding index path — switching to "
                  "prepare-only.\n        Prepared chunks go to --output-dir; run the separate "
                  "`embed` step to embed them externally.")

    # full-external forces prepare-only (prepare locally, embed elsewhere — §8).
    args.prepare_only = resolution.prepare_only

    # --prepare-pending-external re-prepares provisionally-light books (§12): it
    # needs no embedding model (prepare semantics) and always produces hierarchical
    # chunks — the upgrade target that replaces the flat provisional ones.
    if args.prepare_pending_external:
        args.prepare_only = True
        resolution = dataclasses.replace(resolution, hierarchical=True)

    # Get books based on criteria
    min_rating = args.min_rating or 0
    rating = args.rating
    filter_authors = args.filter_authors

    # Build effective exclude list: config-based tags + user additions,
    # unless --include-excluded drops the configured defaults entirely.
    base_excludes = [] if args.include_excluded else get_excluded_tags(library_path)
    effective_excludes = base_excludes + (args.exclude_tags or [])

    if args.include_excluded:
        print(f"  Default excludes overridden (--include-excluded)")
    if effective_excludes:
        print(f"  Excluding tags: {', '.join(effective_excludes)}")

    # Determine RAG database path
    if args.db_path is None:
        args.db_path = str(library_path / ".archilles" / "rag_db")

    print(f"💾 RAG database: {args.db_path}")

    # Initialize RAG
    # DummyRAG only for pure dry-run without skip-existing and without cleanup-orphans
    needs_real_rag = (args.skip_existing or args.cleanup_orphans
                      or args.prepare_pending_external or not args.dry_run)
    if not needs_real_rag:
        class DummyRAG:
            def __init__(self):
                self.collection = type('obj', (object,), {'count': lambda: 0})()
        rag = DummyRAG()
    else:
        try:
            rag = ArchillesRAG(
                db_path=args.db_path,
                reset_db=args.reset_db,
                enable_ocr=args.enable_ocr,
                force_ocr=args.force_ocr,
                languages=get_languages(get_library_path(required=False)),
                profile=resolution.profile_name,
                execution_plan=resolution.execution_plan,
                hierarchical=resolution.hierarchical,
                use_modular_pipeline=args.use_modular_pipeline,
                adapter=adapter,
                skip_model=getattr(args, 'prepare_only', False),
                prepare_chunk_size=args.prepare_chunk_size,
                prepare_overlap=args.prepare_overlap,
            )
        except LanceDBError as e:
            print(f"\n{'='*60}")
            print(f"❌ DATABASE ERROR")
            print(f"{'='*60}\n")
            print(str(e))
            print(f"\n{'='*60}\n")
            sys.exit(1)

    # Operational hint (finding 1.1): the resolved plan is `light` but the index
    # already holds hierarchical chunks → new titles would be flat and unmarked.
    if resolution.execution_plan is not None and getattr(rag, 'store', None) is not None:
        warn_if_light_plan_hides_hierarchy(resolution.execution_plan, rag.store)

    # --prepare-pending-external: standalone discovery + re-prepare (§12).
    # Find provisionally-light books, re-prepare them hierarchically; the user
    # then runs `embed --mode remote` to replace the flat chunks externally.
    if args.prepare_pending_external:
        # Bound the set before a single book is resolved or parsed: this feeds
        # a metered external embedding run, and the index-derived half can be
        # the whole corpus (the has_parent_chunks gate is satisfied by as few
        # as one hierarchical book).
        nominated = nominate_external_catchup(rag.store)
        bound = check_external_catchup_bound(
            marked_count=len(nominated[0]),
            derived_count=len(nominated[1]),
            allow_large=args.allow_large_external_catchup,
        )
        if not bound.allowed:
            print(f"\n   ⛔ {bound.reason}")
            sys.exit(1)
        books = discover_pending_external_books(
            rag, library_path, adapter=adapter, nominated=nominated
        )
        if not books:
            print("✅ No books awaiting external embedding")
            return
        print(f"🔎 {len(books)} book(s) awaiting external embedding")
        if args.output_dir == './prepared_chunks':
            print("  ℹ️  Using the default --output-dir. If this is shared with a large "
                  "corpus prepare run, consider a dedicated (smaller) directory for the "
                  "trickle — embed re-scans every file in --input-dir on each run.")
        stats = batch_prepare(
            books=books,
            rag=rag,
            output_dir=args.output_dir,
            dry_run=args.dry_run,
            prefer_format=args.prefer_format,
            quality_select=getattr(args, 'quality_select', False),
        )
        print("\n  ▶ Next: embed externally to replace the provisional flat chunks:")
        print(f"      python scripts/rag_demo.py embed --input-dir {args.output_dir} "
              f"--mode remote")
        print("      (embed refreshes the FTS index itself — no separate "
              "create-index run needed)")
        if stats['failed'] > 0:
            sys.exit(1)
        return

    # Book selection + indexing (skipped when --cleanup-orphans is used standalone)
    use_adapter = adapter is not None and adapter.adapter_type != "calibre"
    stats = {'indexed': 0, 'failed': 0}
    if args.all or args.tag or args.author or args.ids or args.collection:
        if use_adapter:
            # Non-Calibre adapter: use adapter for book discovery
            tag = args.tag if args.tag else None
            collection = args.collection if args.collection else None
            print(f"📚 Listing documents via {adapter.adapter_type} adapter")
            item_type = args.item_type if args.item_type else None
            if collection:
                print(f"  Collection filter: {collection} (incl. sub-collections)")
            elif tag:
                print(f"  Tag filter: {tag}")
            if item_type:
                print(f"  Item type filter: {item_type}")
            if filter_authors:
                print(f"  Author filter: {', '.join(filter_authors)}")
            books = _adapter_list_books(
                adapter,
                tag_filter=tag,
                exclude_tags=effective_excludes or None,
                author_filter=filter_authors if (filter_authors or args.author) else None,
                collection_filter=collection,
                item_type_filter=item_type,
            )
            # --author mode: filter by author name
            if args.author and not filter_authors:
                books = _adapter_list_books(
                    adapter,
                    exclude_tags=effective_excludes or None,
                    author_filter=[args.author],
                    collection_filter=collection,
                    item_type_filter=item_type,
                )
        elif args.all:
            print(f"📚 Indexing ALL books in the library")
            if filter_authors:
                print(f"  Author filter: {', '.join(filter_authors)}")
            books = get_all_books(library_path, author_filter=filter_authors, exclude_tags=effective_excludes or None)
        elif args.tag:
            print(f"  Filtering by tag: {args.tag}")
            if min_rating > 0:
                print(f"  Minimum rating: {'*' * min_rating} ({min_rating}+ stars)")
            if rating is not None:
                if rating == 0:
                    print(f"  Exact rating: no rating (NULL)")
                else:
                    print(f"  Exact rating: {'*' * rating} ({rating} stars)")
            if filter_authors:
                print(f"  Author filter: {', '.join(filter_authors)}")
            books = get_books_by_tag(library_path, args.tag, min_rating=min_rating, exclude_tags=effective_excludes or None, rating=rating, author_filter=filter_authors)
        elif args.author:
            print(f"  Filtering by author: {args.author}")
            if min_rating > 0:
                print(f"  Minimum rating: {'*' * min_rating} ({min_rating}+ stars)")
            if rating is not None:
                if rating == 0:
                    print(f"  Exact rating: no rating (NULL)")
                else:
                    print(f"  Exact rating: {'*' * rating} ({rating} stars)")
            if filter_authors:
                print(f"  Additional author filter: {', '.join(filter_authors)}")
            books = get_books_by_author(library_path, args.author, min_rating=min_rating, rating=rating, exclude_tags=effective_excludes or None)
        elif args.ids:
            try:
                calibre_ids = [int(i.strip()) for i in args.ids.split(',') if i.strip()]
            except ValueError:
                print(f"❌ Invalid --ids value: '{args.ids}' — use comma-separated integers, e.g. --ids 1234,5678")
                sys.exit(1)
            print(f"  Indexing {len(calibre_ids)} specific book(s) by Calibre ID: {calibre_ids}")
            books = get_books_by_ids(library_path, calibre_ids)

        if not books:
            print("❌ No books found matching criteria")
            if not args.cleanup_orphans:
                return

        else:
            print(f"📖 Found {len(books)} books")

            if args.limit:
                books = books[:args.limit]
                print(f"📊 Limited to first {args.limit} books")

            # --reindex-comments: fast path, no full-text extraction
            if args.reindex_comments:
                log_file = Path(args.log) if args.log else None
                reindex_books = books
                if getattr(args, 'skip', None):
                    n = args.skip
                    if n >= len(books):
                        print(f"⚠️  --skip {n} exceeds total books ({len(books)}). Nothing to do.")
                        return
                    print(f"  ↩  Skipping {n} books, starting at [{n+1}] {books[n]['author']}: {books[n]['title']}")
                    reindex_books = books[n:]
                elif getattr(args, 'start_after', None):
                    fragment = args.start_after.lower()
                    # Use last match so the author is found in the correct rating tier
                    matches = [i for i, b in enumerate(books)
                               if fragment in b.get('author', '').lower()]
                    if not matches:
                        print(f"⚠️  --start-after: no author matching '{args.start_after}' found. Processing all books.")
                    else:
                        idx = matches[-1]
                        print(f"  ↩  Skipping {idx} books, starting at [{idx+1}] {books[idx]['author']}: {books[idx]['title']}")
                        reindex_books = books[idx:]
                batch_reindex_comments(
                    books=reindex_books,
                    rag=rag,
                    dry_run=args.dry_run,
                    log_file=log_file,
                )
                return

            # --prepare-only: extract and chunk without embedding
            if getattr(args, 'prepare_only', False):
                stats = batch_prepare(
                    books=books,
                    rag=rag,
                    output_dir=args.output_dir,
                    dry_run=args.dry_run,
                    prefer_format=args.prefer_format,
                    quality_select=getattr(args, 'quality_select', False),
                )
                if stats['failed'] > 0:
                    sys.exit(1)
                return

            # Initialize SafeIndexer for signal handling only
            safe_indexer = None
            phase = 'phase1' if args.phase1_only else 'phase2'
            if not args.dry_run:
                safe_indexer = SafeIndexer(db_path=Path(args.db_path))

            # Run batch indexing
            log_file = Path(args.log) if args.log else None
            stats = batch_index(
                books=books,
                rag=rag,
                dry_run=args.dry_run,
                skip_existing=args.skip_existing,
                reindex_before=reindex_before,
                reindex_missing_labels=args.reindex_missing_labels,
                force=args.force,
                log_file=log_file,
                safe_indexer=safe_indexer,
                phase=phase,
                db_path=args.db_path,
                prefer_format=args.prefer_format,
                quality_select=getattr(args, 'quality_select', False),
            )

            # Create FTS index if we indexed something new
            if not args.dry_run and stats['indexed'] > 0:
                print("\n📇 Creating full-text search index...")
                try:
                    rag.store.create_fts_index()
                    print("   ✅ FTS index created - keyword search now available")
                except Exception as e:
                    print(f"   ⚠️  FTS index creation failed: {e}")
                    print("   Keyword search may not work. Run: python scripts/rag_demo.py create-index")
                # Ensure the ANN index exists — without it semantic/hybrid
                # search brute-force-scans the vector column.
                try:
                    if rag.store.ensure_vector_index():
                        print("   ✅ Vector index present - semantic search fast")
                except Exception as e:
                    print(f"   ⚠️  Vector index check failed: {e}")
                    print("   Semantic search may be slow. Run: python scripts/rag_demo.py create-index")

            if stats['failed'] > 0:
                sys.exit(1)

    # Orphan cleanup (after indexing, or standalone)
    if args.cleanup_orphans:
        cleanup_orphans(rag, library_path, dry_run=args.dry_run, adapter=adapter,
                        allow_large=args.allow_large_orphan_cleanup)


if __name__ == '__main__':
    main()
