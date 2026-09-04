"""
Folder adapter — indexes any directory of files.

This is the adapter for the Archilles Lab and for users without Calibre/Zotero.
Reads a directory recursively. Metadata comes from:
1. Sidecar JSON (`<library>/.archilles/metadata/<relative-path>.json`)
2. Filename parsing (date, author, title from naming conventions)
3. Filesystem attributes (timestamps)
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from src.adapters.base import (
    DocumentAnnotation,
    DocumentMetadata,
    DocumentTimestamps,
    SourceAdapter,
)

logger = logging.getLogger(__name__)

# Formats the pipeline can actually extract text from
SUPPORTED_EXTENSIONS = {
    '.pdf', '.epub', '.mobi', '.azw3', '.djvu',
    '.txt', '.text', '.md', '.markdown', '.rst', '.txtz',
    '.html', '.htm', '.xhtml',
}

# Directories to skip during recursive scan
IGNORED_DIRS = {'.archilles', '.git', '.obsidian', '__pycache__', 'node_modules', '.venv', 'venv'}


def _load_exclude_patterns(library_path: Path) -> list[str]:
    """Read ``exclude_patterns`` from the library's ``.archilles/config.json``.

    Operational notes, drafts and other housekeeping files often live in the
    same tree as research material. Indexing them contaminates the corpus:
    they match on any query, carry no scholarly content, and can surface
    private context in a search result.
    """
    config_path = library_path / ".archilles" / "config.json"
    if not config_path.is_file():
        return []
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read {config_path}: {e}")
        return []
    patterns = config.get('exclude_patterns', [])
    if not isinstance(patterns, list):
        logger.warning(f"exclude_patterns in {config_path} is not a list — ignoring")
        return []
    return [str(p) for p in patterns]


def _stable_doc_id(library_path: Path, file_path: Path) -> str:
    """Derive a stable, unique doc_id from the relative path.

    Uses a short SHA-256 prefix of the relative POSIX path so that
    IDs are filesystem-safe and constant across runs.
    """
    rel = file_path.relative_to(library_path).as_posix()
    h = hashlib.sha256(rel.encode('utf-8')).hexdigest()[:12]
    return f"folder:{h}"


def _parse_filename(stem: str) -> dict:
    """Extract metadata hints from filename conventions.

    Supported patterns:
      - ``2026-03-01_claude_topic-slug`` → date, platform, title
      - ``7570_author_title_detail``     → source_id, author, title
      - Plain ``my document``            → title only
    """
    result = {}

    # Pattern 1: date_platform_topic (chat imports)
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_([a-zA-Z0-9]+)_(.+)$', stem)
    if m:
        result['date'] = m.group(1)
        result['platform'] = m.group(2)
        result['title'] = m.group(3).replace('-', ' ').replace('_', ' ').strip()
        return result

    # Pattern 2: id_author_title (excerpts)
    m = re.match(r'^(\d+)_([^_]+)_(.+)$', stem)
    if m:
        result['ref_id'] = m.group(1)
        result['author'] = m.group(2).replace('-', ' ').strip()
        result['title'] = m.group(3).replace('-', ' ').replace('_', ' ').strip()
        return result

    # Fallback: use whole stem as title
    result['title'] = stem.replace('-', ' ').replace('_', ' ').strip()
    return result


def _file_timestamps(file_path: Path) -> DocumentTimestamps:
    """Build timestamps from filesystem stat."""
    try:
        st = file_path.stat()
        # st_ctime is birth time on Windows, metadata change on Unix
        created = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat()
        modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        return DocumentTimestamps(created_at=created, modified_at=modified)
    except OSError:
        return DocumentTimestamps()


class FolderAdapter(SourceAdapter):
    """Adapter for plain directory structures.

    Reads a directory recursively.  Metadata comes from:
    1. Sidecar JSON (``<library>/.archilles/metadata/<relative-path>.json``)
    2. Filename parsing
    3. Filesystem attributes (timestamps)
    """

    def __init__(self, library_path: Path, exclude_patterns: list[str] | None = None):
        self._library_path = Path(library_path)
        if not self._library_path.is_dir():
            raise NotADirectoryError(f"Not a directory: {self._library_path}")
        self._sidecar_dir = self._library_path / ".archilles" / "metadata"
        # Cache: relative posix path → DocumentMetadata
        self._cache: dict[str, DocumentMetadata] | None = None
        if exclude_patterns is None:
            exclude_patterns = _load_exclude_patterns(self._library_path)
        self._exclude_patterns = [p.lower() for p in exclude_patterns]
        if self._exclude_patterns:
            logger.info(
                f"Excluding {len(self._exclude_patterns)} pattern(s) from "
                f"{self._library_path}: {self._exclude_patterns}"
            )

    @property
    def adapter_type(self) -> str:
        return "folder"

    @property
    def _ignored_dirs(self) -> set[str]:
        """Directory names pruned during the recursive scan."""
        return IGNORED_DIRS

    def is_excluded(self, rel_posix: str) -> bool:
        """True when a relative path matches one of the exclude patterns.

        Patterns are matched case-insensitively against both the full
        relative path (``notes/scratch.md``) and the bare filename
        (``scratch.md``), so ``*.tmp`` and ``Betrieb/*`` both work.
        """
        if not self._exclude_patterns:
            return False
        rel = rel_posix.lower()
        name = rel.rsplit('/', 1)[-1]
        return any(
            fnmatchcase(rel, pat) or fnmatchcase(name, pat)
            for pat in self._exclude_patterns
        )

    @property
    def library_path(self) -> Path:
        return self._library_path

    def _sidecar_path(self, rel_posix: str) -> Path:
        """Path to the sidecar JSON for a given relative file path."""
        return self._sidecar_dir / f"{rel_posix}.json"

    def _load_sidecar(self, rel_posix: str) -> dict:
        """Load sidecar metadata JSON, or return empty dict."""
        sp = self._sidecar_path(rel_posix)
        if not sp.is_file():
            return {}
        try:
            with open(sp, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read sidecar %s: %s", sp, e)
            return {}

    def _build_metadata(self, file_path: Path) -> DocumentMetadata:
        """Build DocumentMetadata for a single file."""
        rel = file_path.relative_to(self._library_path)
        rel_posix = rel.as_posix()
        doc_id = _stable_doc_id(self._library_path, file_path)

        sidecar = self._load_sidecar(rel_posix)
        parsed = _parse_filename(file_path.stem)

        # Timestamps: sidecar overrides filesystem
        fs_ts = _file_timestamps(file_path)
        sc_ts = sidecar.get('timestamps', {})
        timestamps = DocumentTimestamps(
            created_at=sc_ts.get('created_at') or sidecar.get('created') or fs_ts.created_at,
            modified_at=sc_ts.get('modified_at') or fs_ts.modified_at,
            imported_at=sc_ts.get('imported_at') or sidecar.get('imported_at'),
            indexed_at=None,  # set by ARCHILLES during indexing
        )

        # Year from sidecar or parsed date
        year = sidecar.get('year')
        if not year and parsed.get('date'):
            try:
                year = int(parsed['date'][:4])
            except (ValueError, IndexError):
                pass

        # Authors: sidecar > parsed > folder name
        authors = sidecar.get('authors', [])
        if not authors and parsed.get('author'):
            authors = [parsed['author']]
        if not authors:
            # Use parent folder name as fallback author hint
            parent = rel.parent.name if rel.parent != rel else ""
            if parent and parent not in IGNORED_DIRS:
                authors = [parent]

        # Custom fields from sidecar
        custom = {}
        for key in ('source_llm', 'source_platform', 'ref_id'):
            val = sidecar.get(key) or parsed.get(key)
            if val:
                custom[key] = val
        if parsed.get('platform'):
            custom.setdefault('source_platform', parsed['platform'])

        return DocumentMetadata(
            doc_id=doc_id,
            title=sidecar.get('title') or parsed.get('title', file_path.stem),
            authors=authors,
            file_path=file_path,
            file_format=file_path.suffix.lstrip('.').lower(),
            tags=sidecar.get('tags', []),
            comments=sidecar.get('comments', ''),
            language=sidecar.get('language', ''),
            year=year,
            publisher=sidecar.get('publisher', ''),
            series=sidecar.get('series', ''),
            identifiers=sidecar.get('identifiers', {}),
            custom_fields=custom,
            timestamps=timestamps,
        )

    def _scan(self) -> dict[str, DocumentMetadata]:
        """Recursively scan library for supported files. Returns rel_posix → metadata.

        ``os.walk`` swallows errors silently by default: a locked, syncing or
        unreadable subtree yields a partial — and non-empty — tree, which the
        orphan-cleanup path would read as "those documents were deleted".
        The scan still returns what it found (indexing a partial library is
        fine), but records that it was partial in ``scan_incomplete`` so the
        deletion paths can refuse it (review 1.1b).
        """
        result = {}
        excluded = 0
        walk_errors: list[OSError] = []
        ignored_dirs = self._ignored_dirs
        for dirpath, dirnames, filenames in os.walk(
            self._library_path, onerror=walk_errors.append
        ):
            # Prune ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in ignored_dirs]

            for fname in filenames:
                fp = Path(dirpath) / fname
                if fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                rel_posix = fp.relative_to(self._library_path).as_posix()
                if self.is_excluded(rel_posix):
                    excluded += 1
                    continue
                result[rel_posix] = self._build_metadata(fp)
        if excluded:
            logger.info(f"Excluded {excluded} file(s) by exclude_patterns")

        self.scan_incomplete = bool(walk_errors)
        if walk_errors:
            for err in walk_errors[:5]:
                logger.warning(
                    "Scan of %s could not be completed: %s (%s)",
                    self._library_path, err.strerror or err, getattr(err, "filename", "?"),
                )
            logger.warning(
                "%d directory error(s) during scan of %s — this scan must not be "
                "used as a basis for deleting index entries",
                len(walk_errors), self._library_path,
            )
        return result

    def _ensure_cache(self) -> dict[str, DocumentMetadata]:
        if self._cache is None:
            self._cache = self._scan()
        return self._cache

    def invalidate_cache(self):
        """Force a re-scan on next access."""
        self._cache = None

    def list_documents(
        self,
        tag_filter: str | None = None,
        exclude_tag: str | None = None,
        collection_filter: str | None = None,
        item_type_filter: str | None = None,
    ) -> list[DocumentMetadata]:
        docs = list(self._ensure_cache().values())

        if tag_filter:
            docs = [d for d in docs if tag_filter in d.tags]
        if exclude_tag:
            docs = [d for d in docs if exclude_tag not in d.tags]

        return docs

    def get_metadata(self, doc_id: str) -> DocumentMetadata | None:
        for doc in self._ensure_cache().values():
            if doc.doc_id == doc_id:
                return doc
        return None

    def get_file_path(self, doc_id: str) -> Path | None:
        meta = self.get_metadata(doc_id)
        return meta.file_path if meta else None

    def get_annotations(self, doc_id: str) -> list[DocumentAnnotation]:
        return []

    def get_comments(self, doc_id: str) -> str:
        meta = self.get_metadata(doc_id)
        return meta.comments if meta else ""

    def get_metadata_by_path(self, file_path: Path) -> DocumentMetadata | None:
        """Efficient lookup by file path using the cache."""
        file_path = Path(file_path).resolve()
        try:
            rel_posix = file_path.relative_to(self._library_path.resolve()).as_posix()
        except ValueError:
            return None
        return self._ensure_cache().get(rel_posix)

    def compute_metadata_hash(self, doc_id: str) -> str:
        """Hash over file mtime and size — no file content opened."""
        meta = self.get_metadata(doc_id)
        if not meta or not meta.file_path:
            return ""
        try:
            st = meta.file_path.stat()
            payload = f"{st.st_mtime_ns}:{st.st_size}"
            return hashlib.md5(payload.encode()).hexdigest()
        except OSError:
            return ""

    def compute_orphan_ids(self, lancedb_ids: set[str]) -> set[str]:
        """Diff against the cached scan — no extra disk traversal."""
        current = {doc.doc_id for doc in self._ensure_cache().values()}
        return {str(x) for x in lancedb_ids} - current

    def get_changed_files(self, since: str) -> list[DocumentMetadata]:
        """Return documents modified after the given ISO timestamp.

        Used for delta indexing: only re-index files whose mtime is
        newer than the last indexing run.
        """
        try:
            cutoff = datetime.fromisoformat(since)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Invalid timestamp for delta check: %s", since)
            return list(self._ensure_cache().values())

        changed = []
        for doc in self._ensure_cache().values():
            if doc.timestamps.modified_at:
                try:
                    mod = datetime.fromisoformat(doc.timestamps.modified_at)
                    if mod.tzinfo is None:
                        mod = mod.replace(tzinfo=timezone.utc)
                    if mod > cutoff:
                        changed.append(doc)
                except ValueError:
                    changed.append(doc)  # can't parse → be safe, include it
            else:
                changed.append(doc)  # no timestamp → include
        return changed
