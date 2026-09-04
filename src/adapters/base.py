"""
SourceAdapter ABC and adapter-agnostic data classes.

This module defines the interface that all library backends must implement.
The Core (pipeline, storage, retriever, service) only depends on this interface,
never on a specific backend like Calibre or Zotero.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DocumentTimestamps:
    """Four-timestamp model for heterogeneous documents.

    All timestamps are ISO 8601 strings, all optional.
    ``created_at`` and ``modified_at`` come from the source adapter.
    ``imported_at`` and ``indexed_at`` are set by ARCHILLES itself.
    """

    created_at: str | None = None
    modified_at: str | None = None
    imported_at: str | None = None
    indexed_at: str | None = None


@dataclass
class DocumentMetadata:
    """Adapter-agnostic document metadata."""

    doc_id: str
    title: str
    authors: list[str]
    file_path: Path
    file_format: str
    tags: list[str] = field(default_factory=list)
    comments: str = ""
    comments_html: str = ""  # Raw HTML from Calibre (for structured indexing)
    language: str = ""
    year: int | None = None
    publisher: str = ""
    series: str = ""
    identifiers: dict = field(default_factory=dict)
    custom_fields: dict = field(default_factory=dict)
    timestamps: DocumentTimestamps = field(default_factory=DocumentTimestamps)


@dataclass
class DocumentAnnotation:
    """A single annotation (highlight, note, bookmark)."""

    text: str
    note: str = ""
    annotation_type: str = "highlight"
    page: int | None = None
    chapter: str = ""
    created: str = ""


class SourceAdapter(ABC):
    """Interface for all library backends.

    Every adapter provides read access to a document collection.  The Core
    never accesses Calibre, Zotero or the filesystem directly — it always
    goes through this interface.
    """

    # ── Required ────────────────────────────────────────────────

    @abstractmethod
    def list_documents(
        self,
        tag_filter: str | None = None,
        exclude_tag: str | None = None,
        collection_filter: str | None = None,
        item_type_filter: str | None = None,
    ) -> list[DocumentMetadata]:
        """List all documents, optionally filtered by tag, collection, or item type."""
        ...

    @abstractmethod
    def get_metadata(self, doc_id: str) -> DocumentMetadata | None:
        """Metadata for a single document, or ``None`` if not found."""
        ...

    @abstractmethod
    def get_file_path(self, doc_id: str) -> Path | None:
        """Absolute path to the primary file (PDF > EPUB > other)."""
        ...

    @abstractmethod
    def get_annotations(self, doc_id: str) -> list[DocumentAnnotation]:
        """Annotations (highlights, notes) for a document."""
        ...

    @abstractmethod
    def get_comments(self, doc_id: str) -> str:
        """Free-text comment / description for a document."""
        ...

    @property
    @abstractmethod
    def adapter_type(self) -> str:
        """Short identifier: ``'calibre'``, ``'zotero'``, ``'folder'``, etc."""
        ...

    @property
    @abstractmethod
    def library_path(self) -> Path:
        """Root directory of the library."""
        ...

    # ── Optional (stubs) ────────────────────────────────────────

    #: Whether the most recent library scan was known to be incomplete — an
    #: I/O or permission error the adapter could see but not resolve. Adapters
    #: that cannot detect a partial read leave this False, which is what every
    #: caller assumed before it existed. The orphan-deletion paths refuse when
    #: it is True: a book missing from a partial scan has not been shown to be
    #: gone (review 1.1b, see ``src/archilles/orphan_guard.py``).
    scan_incomplete: bool = False

    def watch_inbox(self, callback=None) -> None:
        """Watch the inbox/ subdirectory for new files.

        No-op by default.  Implementations may use ``watchdog`` or similar.
        """

    def get_metadata_by_path(self, file_path: Path) -> DocumentMetadata | None:
        """Look up metadata by file path instead of doc_id.

        Useful during indexing when only the file path is known.
        Default implementation iterates ``list_documents()`` — adapters
        should override with an efficient lookup.
        """
        file_path = Path(file_path).resolve()
        for doc in self.list_documents():
            if doc.file_path.resolve() == file_path:
                return doc
        return None

    def compute_metadata_hash(self, doc_id: str) -> str:
        """Stable hash over a document's key metadata fields.

        Used by the Watchdog to detect changes without opening book files.
        Returns ``""`` if the document is not found or the adapter does not
        support hash-based change detection.  Adapters should override this
        with an efficient, source-specific implementation.
        """
        return ""

    def compute_orphan_ids(self, lancedb_ids: set[str]) -> set[str]:
        """Return the subset of ``lancedb_ids`` no longer present in the source.

        Given the set of doc_ids currently held in LanceDB, the adapter compares
        them against the live source and returns the difference — these are
        orphan entries that should be removed during cleanup.

        Returns the empty set if the adapter does not support orphan detection;
        callers should interpret that as "no cleanup performed", not "all
        clean".  Adapters should override this with an efficient,
        source-specific implementation.
        """
        return set()
