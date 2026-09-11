"""Kanonische Hash-Funktionen fuer Change-Detection (Befund 7.15).

Eine Stelle fuer die Calibre-Metadata-Hash- und die Annotation-Content-Hash-
Logik. Wird von Watchdog, Engine, Calibre-Adapter und patch_comments genutzt.
Die erzeugten Hashes sind byte-identisch zu den bisherigen verteilten Kopien
(Invarianten I1-I3 in PLAN_2026-06-14_HASH_MODUL.md) -- Aenderungen hier loesen
einen Reindex aller Buecher aus.
"""
import hashlib
import json
from typing import Any, Dict, List


def compute_metadata_hash(book_metadata: Dict[str, Any]) -> str:
    """MD5 ueber comments/tags/title/author/publisher (Calibre-Change-Detection).

    Tags werden unabhaengig vom Eingabetyp sortiert, damit Calibres interne
    Tag-Reihenfolge den Hash nicht aendert. Ein leeres/None-Dict ergibt ''.
    """
    if not book_metadata:
        return ''
    tags = book_metadata.get('tags', [])
    if isinstance(tags, str):
        tags = sorted(t.strip() for t in tags.split(',') if t.strip())
    elif isinstance(tags, list):
        tags = sorted(tags)
    relevant = {
        'comments': book_metadata.get('comments', ''),
        'tags': tags,
        'title': book_metadata.get('title', ''),
        'author': book_metadata.get('author', ''),
        'publisher': book_metadata.get('publisher', ''),
    }
    return hashlib.md5(
        json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def compute_zotero_metadata_hash(data: Dict[str, Any]) -> str:
    """MD5 ueber title/authors/tags/abstract/date (Zotero-Change-Detection).

    Authors und Tags werden sortiert, damit ihre Reihenfolge in Zoteros UI den
    Hash nicht aendert. Byte-identisch zu den bisherigen Kopien in
    watchdog._compute_zotero_metadata_hash und ZoteroAdapter.compute_metadata_hash
    (Sortieren ist idempotent, daher auch fuer pre-sorted Eingaben unveraendert).
    """
    relevant = {
        'title':    data.get('title', ''),
        'authors':  sorted(data.get('authors') or []),
        'tags':     sorted(data.get('tags') or []),
        'abstract': data.get('abstract', ''),
        'date':     data.get('date', ''),
    }
    return hashlib.md5(
        json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def compute_annotation_hash(annotations: List[Dict[str, Any]]) -> str:
    """MD5 ueber alle Annotationen eines Buchs (Change-Detection).

    Sortiert nach 'highlighted_text|notes|type', join mit '\\n'. Leere Liste -> ''.
    """
    if not annotations:
        return ''
    texts = sorted(
        f"{a.get('highlighted_text', '')}|{a.get('notes', '')}|{a.get('type', '')}"
        for a in annotations
    )
    return hashlib.md5('\n'.join(texts).encode('utf-8')).hexdigest()


def file_sha256(path) -> str | None:
    """SHA-256 of a file's bytes, or ``None`` if it cannot be read.

    Used to tell a file's versions apart -- a Scriptor master as the tool wrote
    it, the same master after handwork, the version that reached the index.
    Unlike the hashes above this one has no corpus-wide invariant: it is
    compared only against another reading of the same file.
    """
    from pathlib import Path

    digest = hashlib.sha256()
    try:
        with open(Path(path), "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()
