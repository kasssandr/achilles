"""Composition of ``calibre_comment`` chunks — the single implementation.

Finding 1.8. This text used to exist twice: once in
``Indexer._build_comment_chunks`` and once, hand-copied, in
``scripts/patch_comments.py``. Same constants, same wording, no coupling — and
that is the mechanism behind the ``Kernaussagen:`` / ``Key points:`` split in
the corpus, where two generations of the same comment answer the same query
with different vocabulary.

The rule the review draws from it, worth stating where the code lives:

    **Any function that composes chunk *text* has exactly one implementation,
    because its output is frozen into vectors.** A divergence here is not a
    bug that a later run repairs; it is two populations in one index, and only
    a re-embed removes it.

Merging the two copies surfaced a divergence nobody had noticed: the duplicate
joined tags with ``" / "`` where the indexer used ``", "``. Measured in the
live Calibre index at the time of the merge: 11 rows carried the slash form
against 27 268 with the comma form, and ``tags`` is a filterable field.

Embedding is deliberately *not* here. This module composes text and metadata;
the caller decides whether and how to encode, which is what let the two copies
drift apart in the first place — one embedded, the other did not, so they
looked like different concerns.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.archilles.constants import ChunkType
from src.calibre_db import CalibreDB

__all__ = ["build_comment_chunks", "format_tags", "MAX_COMMENT_WORDS"]

# BGE-M3 retrieval quality degrades significantly beyond ~500 words, so long
# headline-less sections are split below this.
MAX_COMMENT_WORDS = 400


def format_tags(tags: Any) -> str:
    """Tags as one string, comma-separated.

    ``", "`` is canonical — it is what the indexer has always written and what
    27 268 of the 27 279 tagged comment rows in the live index carry.
    """
    if not tags:
        return ""
    return ", ".join(tags) if isinstance(tags, list) else tags


def _sections_from_metadata(book_metadata: dict) -> list[dict]:
    """Structured sections from HTML comments, or one section from plain text."""
    comments_html = book_metadata.get('comments_html', '')
    if comments_html:
        return CalibreDB.parse_html_comment(comments_html)
    plain = book_metadata.get('comments', '')
    if not plain:
        return []
    return [{
        'headline': None, 'headline_level': None,
        'text': plain, 'key_passages': [],
    }]


def _split_section(section: dict) -> list[dict]:
    """Split an over-long section at sentence boundaries, never mid-sentence.

    Key passages stay with the first part only: repeating them into every
    sub-chunk would weight the same sentence several times in the index.
    """
    words = section['text'].split()
    if len(words) <= MAX_COMMENT_WORDS:
        return [section]

    sentences = re.split(r'(?<=[.!?])\s+', section['text'])
    sub_sections: list[dict] = []
    current_words = 0
    current_sents: list[str] = []
    first = True

    def flush() -> None:
        nonlocal first
        sub_sections.append({
            'headline': section['headline'],
            'headline_level': section['headline_level'],
            'text': ' '.join(current_sents),
            'key_passages': section['key_passages'] if first else [],
        })
        first = False

    for sent in sentences:
        sent_words = len(sent.split())
        if current_sents and current_words + sent_words > MAX_COMMENT_WORDS:
            flush()
            current_sents = [sent]
            current_words = sent_words
        else:
            current_sents.append(sent)
            current_words += sent_words
    if current_sents:
        flush()
    return sub_sections


def _apply_book_metadata(chunk: dict, book_metadata: dict) -> None:
    """Copy standard book metadata onto a chunk (in place)."""
    if not book_metadata:
        return
    if book_metadata.get('author'):
        chunk['author'] = book_metadata['author']
    if book_metadata.get('title'):
        chunk['book_title'] = book_metadata['title']
    if book_metadata.get('year'):
        chunk['year'] = book_metadata['year']
    if book_metadata.get('publisher'):
        chunk['publisher'] = book_metadata['publisher']
    if book_metadata.get('calibre_id'):
        chunk['calibre_id'] = book_metadata['calibre_id']
    if book_metadata.get('source_id'):
        chunk['source_id'] = book_metadata['source_id']
    if book_metadata.get('tags'):
        chunk['tags'] = format_tags(book_metadata['tags'])


def build_comment_chunks(
    book_metadata: dict,
    book_id: str,
    book_format: str,
    metadata_hash: str,
    indexed_at: str | None = None,
) -> list[dict]:
    """Build the ``calibre_comment`` chunks for one book. No embeddings.

    H2–H4 headlines become separate chunks, and bold / ``<strong>`` /
    ``!!!…!!!`` passages are hoisted as ``Key points:`` so they carry extra
    weight in the embedding.

    Every string this function emits — the ``[CALIBRE_COMMENT]`` prefix, the
    ``## headline ##`` wrapper, ``Key points:``, the ``|`` separator — is
    already frozen into vectors in existing indexes. Changing one changes what
    a re-indexed book means relative to a book that was not re-indexed.
    """
    sections = _sections_from_metadata(book_metadata)
    if not sections:
        return []

    flat_sections: list[dict] = []
    for section in sections:
        flat_sections.extend(_split_section(section))

    stamp = indexed_at or datetime.now().isoformat()
    title = book_metadata.get('title', book_id)
    chunks: list[dict] = []

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
            'indexed_at': stamp,
            'metadata_hash': metadata_hash,
        }
        if section['headline']:
            chunk['section_title'] = section['headline']
        _apply_book_metadata(chunk, book_metadata)
        chunks.append(chunk)

    return chunks
