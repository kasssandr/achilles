"""Data models for extracted text and metadata."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime


@dataclass
class ChunkMetadata:
    """Metadata for a single text chunk."""

    # Source identification
    book_id: Optional[int] = None
    title: Optional[str] = None
    author: Optional[str] = None
    year: Optional[int] = None

    # Position in document
    page: Optional[int] = None
    page_label: Optional[str] = None
    chapter: Optional[str] = None
    section: Optional[str] = None
    section_title: Optional[str] = None
    section_type: Optional[str] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None

    # PDF-specific coordinates for clickable citations
    pdf_coords: Optional[Dict[str, float]] = None

    # Source file
    source_file: Optional[str] = None
    format: Optional[str] = None

    # Provenance, set by ScriptorExtractor only: the producer's signal beside
    # Archilles' reading of it -- the region name next to section_type, the
    # witness behind page_label, and the spec version the master follows.
    region: Optional[str] = None
    label_source: Optional[str] = None
    producer_version: Optional[str] = None

    # Calibre integration
    calibre_uri: Optional[str] = None

    # Additional metadata
    language: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    custom_fields: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionMetadata:
    """Metadata about the extraction process."""

    # File information
    file_path: Path
    file_size: int
    file_format: str
    detected_format: str

    # Extraction details
    extraction_method: str
    extraction_time: float
    extracted_at: datetime = field(default_factory=datetime.now)

    # Quality indicators
    success: bool = True
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # Statistics
    total_pages: Optional[int] = None
    total_chars: int = 0
    total_words: int = 0
    total_chunks: int = 0

    # OCR information
    ocr_applied: bool = False
    ocr_pages: List[int] = field(default_factory=list)
    ocr_confidence: Optional[float] = None


@dataclass
class ExtractedText:
    """Complete extracted text with metadata."""

    full_text: str
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Optional[ExtractionMetadata] = None
    toc: List[Dict[str, Any]] = field(default_factory=list)
    footnotes: List[Dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        """Auto-calculate statistics if not provided."""
        if self.metadata and self.metadata.total_chars == 0:
            self.metadata.total_chars = len(self.full_text)
            self.metadata.total_words = len(self.full_text.split())
            self.metadata.total_chunks = len(self.chunks)

