"""The files a book consists of in a library: its formats, and its Scriptor bundle.

``discover_formats`` is the one place that lists a Calibre book folder; the
watchdog and batch_index each kept a copy of it.

``bundle_master`` finds a book's prepared text. A Scriptor bundle lies in the
library's extension zone (ADR-005), ``<library>/.archilles/scriptor/<key>/``,
keyed like the book's prepared JSONL. It is not another format of the book: the
book file stays the book's identity -- Calibre metadata, viewer annotations
(keyed by the file's path) and links follow it -- and only the text is read
from the bundle, where indexing extracts (Naht S4).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from scriptor.reflow.regions import read_metadata_block

from src.archilles.constants import PREFERRED_FORMATS

logger = logging.getLogger(__name__)

_PREFERRED_FORMAT_SET = frozenset(PREFERRED_FORMATS)

# Characters that are invalid in Windows filenames (superset of POSIX).
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

BUNDLE_FOLDER = 'scriptor'
_MASTER_SUFFIXES = frozenset({'.md', '.markdown'})
_REVIEW_SUFFIX = '.review.md'
# Enough for the metadata block, which opens a master (spec §4.1).
_SNIFF_BYTES = 4096


def prepared_jsonl_name(book_id: str) -> str:
    """Filename for a book's prepared-chunks JSONL (one file per book).

    Keyed by the adapter-unique ``book_id``, not ``calibre_id`` — non-Calibre
    sources (Zotero keys, folder ids) have no Calibre id, so every item used
    to collide on ``0.jsonl`` and only one book per library ever got prepared
    (review 2026-07-03, finding 5.1). For Calibre books ``book_id`` is the
    numeric id as a string, so existing ``{calibre_id}.jsonl`` corpora keep
    matching.

    Filesystem-unsafe characters are replaced; whenever sanitisation changes
    the name (or empties it), a short hash of the original id is appended so
    distinct ids ("a/b" vs "a_b") cannot map to the same file.
    """
    original = str(book_id)
    safe = _UNSAFE_FILENAME_CHARS.sub('_', original).strip(' .')
    if not safe or safe != original:
        digest = hashlib.md5(original.encode('utf-8')).hexdigest()[:8]
        safe = f"{safe or 'book'}-{digest}"
    return f"{safe}.jsonl"


def discover_formats(book_dir: Path) -> list[dict[str, str]]:
    """The book files in ``book_dir``, in PREFERRED_FORMATS order.

    One directory scan instead of one glob per extension -- this runs for
    every library book on every watchdog scan.
    """
    by_ext: dict[str, list[str]] = {}
    try:
        with os.scandir(book_dir) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix in _PREFERRED_FORMAT_SET:
                    by_ext.setdefault(suffix, []).append(entry.path)
    except OSError:
        return []
    return [
        {'format': ext[1:].upper(), 'path': path}
        for ext in PREFERRED_FORMATS
        for path in sorted(by_ext.get(ext, ()))
    ]


def is_scriptor_master(file_path: Path) -> bool:
    """A Markdown file whose metadata block names a ``format_version``.

    The detected format cannot tell: python-magic calls a master text/plain,
    like any note. The block can, and it opens the file (spec §4.1).
    """
    file_path = Path(file_path)
    if file_path.suffix.lower() not in _MASTER_SUFFIXES:
        return False
    try:
        with open(file_path, 'rb') as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return False
    block = read_metadata_block(head.decode('utf-8', errors='replace').replace('\r\n', '\n'))
    return bool(block and block.get('format_version'))


def bundle_dir(archilles_dir: Path, book_id: str) -> Path:
    """Where the bundle of ``book_id`` lies, whether or not it exists."""
    return Path(archilles_dir) / BUNDLE_FOLDER / prepared_jsonl_name(book_id)[:-len('.jsonl')]


def bundle_master(archilles_dir: Path, book_id: str) -> Path | None:
    """The master of the book's bundle, or None.

    A master is a Markdown file there that declares a format_version and is
    not the review copy (which declares one too). Two of them are refused and
    named rather than chosen between: which of them was meant is not this
    function's to guess.
    """
    folder = bundle_dir(archilles_dir, book_id)
    if not folder.is_dir():
        return None
    masters = [
        p for p in sorted(folder.iterdir())
        if p.is_file() and not p.name.lower().endswith(_REVIEW_SUFFIX) and is_scriptor_master(p)
    ]
    if len(masters) > 1:
        logger.warning("Scriptor bundle %s holds %d masters (%s) -- none is used",
                       folder, len(masters), ", ".join(p.name for p in masters))
        return None
    return masters[0] if masters else None
