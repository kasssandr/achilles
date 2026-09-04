#!/usr/bin/env python3
"""
Patch existing prepared JSONL files to include Calibre comment chunks.

Reads each JSONL in the prepared_chunks directory, checks if it already
contains calibre_comment chunks.  If not, fetches comments from Calibre's
metadata.db, builds comment chunks (without embeddings), and rewrites the
JSONL with the additional chunks and an updated header.

Usage:
    python scripts/patch_comments.py [--input-dir ./prepared_chunks] [--dry-run]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── project imports ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.archilles.comment_chunks import build_comment_chunks
from src.archilles.config import get_library_path
from src.archilles.constants import ChunkType
from src.archilles.sqlite_ro import connect_readonly
from src.calibre_db import CalibreDB          # parse_html_comment, clean_html


# ── helpers (mirrored from rag_demo.py to stay standalone) ───────────

def _compute_metadata_hash(book_metadata: dict) -> str:
    """Delegiert an src.archilles.hashing (Befund 7.15)."""
    from src.archilles.hashing import compute_metadata_hash
    return compute_metadata_hash(book_metadata)


def _format_tags(tags) -> str:
    if isinstance(tags, list):
        return ' / '.join(tags)
    return str(tags) if tags else ''


def _build_comment_chunks_standalone(
    book_metadata: dict,
    book_id: str,
    book_format: str,
    metadata_hash: str,
) -> list:
    """Build calibre_comment chunk dicts (no embeddings).

    Thin wrapper over the one implementation in
    ``src/archilles/comment_chunks`` (finding 1.8). This function used to be a
    hand-copied duplicate of ``Indexer._build_comment_chunks`` — identical
    wording, identical constants, no coupling — and it had already drifted:
    it joined tags with " / " where the indexer used ", ", leaving 11 rows in
    the live index that answer a tag filter differently from their neighbours.

    Chunk text is frozen into vectors, so it gets exactly one implementation.
    """
    return build_comment_chunks(book_metadata, book_id, book_format, metadata_hash)

def fetch_calibre_comments(calibre_db_path: Path, calibre_id: int) -> dict:
    """Fetch comments_html and comments_text for a book from Calibre."""
    conn = connect_readonly(calibre_db_path, row_factory=sqlite3.Row)
    try:
        row = conn.execute(
            "SELECT text FROM comments WHERE book = ?", (calibre_id,)
        ).fetchone()
        if not row or not row['text']:
            return {}
        html = row['text']
        return {
            'comments_html': html,
            'comments': CalibreDB.clean_html(html),
        }
    finally:
        conn.close()


def fetch_calibre_book_metadata(calibre_db_path: Path, calibre_id: int) -> dict:
    """Fetch full book metadata needed for comment chunk building."""
    conn = connect_readonly(calibre_db_path, row_factory=sqlite3.Row)
    try:
        # Basic book info
        book = conn.execute(
            "SELECT id, title, path FROM books WHERE id = ?", (calibre_id,)
        ).fetchone()
        if not book:
            return {}

        meta = {
            'calibre_id': calibre_id,
            'title': book['title'],
        }

        # Author
        author_row = conn.execute(
            "SELECT a.name FROM authors a "
            "JOIN books_authors_link bal ON a.id = bal.author "
            "WHERE bal.book = ?", (calibre_id,)
        ).fetchone()
        if author_row:
            meta['author'] = author_row['name']

        # Publisher
        pub_row = conn.execute(
            "SELECT p.name FROM publishers p "
            "JOIN books_publishers_link bpl ON p.id = bpl.publisher "
            "WHERE bpl.book = ?", (calibre_id,)
        ).fetchone()
        if pub_row:
            meta['publisher'] = pub_row['name']

        # Tags
        tag_rows = conn.execute(
            "SELECT t.name FROM tags t "
            "JOIN books_tags_link btl ON t.id = btl.tag "
            "WHERE btl.book = ?", (calibre_id,)
        ).fetchall()
        if tag_rows:
            meta['tags'] = [r['name'] for r in tag_rows]

        # Comments
        comment_data = fetch_calibre_comments(calibre_db_path, calibre_id)
        meta.update(comment_data)

        return meta
    finally:
        conn.close()


# ── main ─────────────────────────────────────────────────────────────

def patch_jsonl_files(input_dir: Path, calibre_db_path: Path, dry_run: bool = False):
    jsonl_files = sorted(input_dir.glob('*.jsonl'))
    if not jsonl_files:
        print("No JSONL files found.")
        return

    stats = {
        'total': len(jsonl_files),
        'patched': 0,
        'already_has_comments': 0,
        'no_comments_in_calibre': 0,
        'no_calibre_id': 0,
        'errors': 0,
    }

    for jsonl_file in jsonl_files:
        try:
            with open(jsonl_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if not lines:
                continue

            header = json.loads(lines[0])
            if not header.get('_header'):
                continue

            calibre_id = header.get('calibre_id')
            book_id = header.get('book_id', jsonl_file.stem)

            if not calibre_id:
                stats['no_calibre_id'] += 1
                continue

            # Check if comment chunks already exist
            has_comment_chunks = False
            for line in lines[1:]:
                chunk = json.loads(line)
                if chunk.get('chunk_type') == ChunkType.CALIBRE_COMMENT:
                    has_comment_chunks = True
                    break

            if has_comment_chunks:
                stats['already_has_comments'] += 1
                continue

            # Fetch metadata from Calibre
            book_metadata = fetch_calibre_book_metadata(calibre_db_path, calibre_id)
            if not book_metadata.get('comments') and not book_metadata.get('comments_html'):
                stats['no_comments_in_calibre'] += 1
                continue

            # Determine format from existing chunks or header
            book_format = 'pdf'
            for line in lines[1:]:
                chunk = json.loads(line)
                if chunk.get('format'):
                    book_format = chunk['format']
                    break

            # Build comment chunks
            meta_hash = _compute_metadata_hash(book_metadata)
            comment_chunks = _build_comment_chunks_standalone(
                book_metadata=book_metadata,
                book_id=book_id,
                book_format=book_format,
                metadata_hash=meta_hash,
            )

            if not comment_chunks:
                stats['no_comments_in_calibre'] += 1
                continue

            title = book_metadata.get('title', book_id)
            author = book_metadata.get('author', '?')
            print(f"  {author}: {title} — +{len(comment_chunks)} comment chunk(s)", end='')

            if dry_run:
                print(" [dry-run]")
                stats['patched'] += 1
                continue

            # Update header
            header['chunk_count'] = header.get('chunk_count', 0) + len(comment_chunks)
            # Also add comments to header metadata if missing
            hdr_meta = header.get('book_metadata', {})
            if 'comments' not in hdr_meta and book_metadata.get('comments'):
                hdr_meta['comments'] = book_metadata['comments']
            if 'comments_html' not in hdr_meta and book_metadata.get('comments_html'):
                hdr_meta['comments_html'] = book_metadata['comments_html']
            header['book_metadata'] = hdr_meta
            header['patched_at'] = datetime.now().isoformat()

            # Rewrite file: header + existing chunks + new comment chunks
            with open(jsonl_file, 'w', encoding='utf-8') as f:
                f.write(json.dumps(header, ensure_ascii=False) + '\n')
                for line in lines[1:]:
                    f.write(line)  # original chunk lines (already have newline)
                for chunk in comment_chunks:
                    f.write(json.dumps(chunk, ensure_ascii=False) + '\n')

            print(" ✓")
            stats['patched'] += 1

        except Exception as e:
            print(f"  ERROR {jsonl_file.name}: {e}")
            stats['errors'] += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"  Total JSONL files:          {stats['total']}")
    print(f"  Patched (comments added):   {stats['patched']}")
    print(f"  Already had comments:       {stats['already_has_comments']}")
    print(f"  No comments in Calibre:     {stats['no_comments_in_calibre']}")
    print(f"  No calibre_id in header:    {stats['no_calibre_id']}")
    print(f"  Errors:                     {stats['errors']}")


def main():
    parser = argparse.ArgumentParser(
        description="Patch existing prepared JSONL files with Calibre comment chunks."
    )
    parser.add_argument(
        '--input-dir', default='./prepared_chunks',
        help='Directory containing JSONL files (default: ./prepared_chunks)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show what would be patched without writing files'
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        print(f"Input directory not found: {input_dir}")
        sys.exit(1)

    # Resolve Calibre library path
    library_path = get_library_path()

    calibre_db_path = library_path / 'metadata.db'
    if not calibre_db_path.exists():
        print(f"Calibre database not found: {calibre_db_path}")
        sys.exit(1)

    print(f"Input:    {input_dir.resolve()}")
    print(f"Calibre:  {calibre_db_path}")
    if args.dry_run:
        print("Mode:     DRY RUN\n")
    else:
        print()

    patch_jsonl_files(input_dir, calibre_db_path, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
