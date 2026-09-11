"""EPUB text extractor."""

import logging
import re
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Optional

try:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    EBOOKLIB_AVAILABLE = True
except ImportError:
    EBOOKLIB_AVAILABLE = False

from src.archilles.constants import SectionType
from src.archilles.text_match import contains_keyword
from src.archilles.i18n import (
    get_toc_back_matter_keywords,
    get_toc_front_matter_keywords,
)
from .base import BaseExtractor
from .models import ExtractedText, ChunkMetadata
from .exceptions import EPUBExtractionError

logger = logging.getLogger(__name__)

# Compiled patterns for section number extraction
_SECTION_NUM_START = re.compile(r'^(\d+(?:\.\d+)*)\s+')
_SECTION_NUM_LABEL = re.compile(r'(?:Chapter|Section)\s+(\d+(?:\.\d+)*)', re.IGNORECASE)

# Section type classification patterns — sourced from the central corpus-
# language data (i18n); not language-filtered. This also gives EPUBs German
# section detection, which the previous English-only set lacked.
# NB: "introduction"/"einleitung" are deliberately NOT classified here —
# introductions are substantive content and belong to main_content.
_FRONT_MATTER_PATTERNS = get_toc_front_matter_keywords()
_BACK_MATTER_PATTERNS = get_toc_back_matter_keywords()

# Filename conventions for sections that carry no heading and no TOC entry.
# Deliberately narrow and English-only: these are EPUB packaging conventions
# produced by conversion tools, not prose titles, so the multilingual TOC
# vocabulary above does not apply. Only closed compounds are listed —
# 'title page' as two words would swallow 'chapter-title-page-3.html'.
# 'index' is deliberately absent: in EPUB filenames it means the book's
# index about as often as it means the converter's source document, and a
# sampled check found `index_split_*.html` files carrying ordinary prose.
# Hiding main content is worse than leaving an index visible, and real
# indexes are usually caught by their TOC title anyway.
_FILENAME_BACK_MATTER = frozenset({
    'note', 'notes', 'footnote', 'footnotes',
    'endnote', 'endnotes', 'bibliography', 'biblio', 'references',
    'glossary', 'appendix', 'colophon',
})
_FILENAME_FRONT_MATTER = frozenset({
    'cover', 'titlepage', 'halftitle', 'frontmatter',
    'toc', 'copyright', 'imprint', 'dedication',
})
_FILENAME_PARATEXT = _FILENAME_BACK_MATTER | _FILENAME_FRONT_MATTER


def _normalize_item_name(filename: str) -> str:
    """Reduce an EPUB item name to bare words for keyword matching.

    ``Text/index_split_033.html`` becomes ``index split``. Digits and
    separators go because they carry the sequence, not the meaning, and
    word-boundary matching would otherwise fail on ``index_split``.
    """
    if not filename:
        return ""
    stem = filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
    stem = re.sub(r'\.(x?html?|ncx|xml)$', '', stem, flags=re.IGNORECASE)
    return re.sub(r'[\d_\-.]+', ' ', stem).strip().lower()


class EPUBExtractor(BaseExtractor):
    """
    Extract text from EPUB files.

    Handles:
    - EPUB 2 and EPUB 3
    - Table of contents extraction
    - Chapter/section preservation
    - Metadata extraction (author, title, etc.)
    """

    SUPPORTED_EXTENSIONS = {'.epub'}

    def supports(self, file_path: Path) -> bool:
        """Check if file is EPUB."""
        return file_path.suffix.lower() in self.SUPPORTED_EXTENSIONS

    def extract(self, file_path: Path) -> ExtractedText:
        """
        Extract text from EPUB file.

        Args:
            file_path: Path to EPUB file

        Returns:
            ExtractedText object

        Raises:
            EPUBExtractionError: If extraction fails
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if not EBOOKLIB_AVAILABLE:
            return self._extract_manual(file_path)

        try:
            return self._extract_with_ebooklib(file_path)
        except Exception as e:
            try:
                return self._extract_manual(file_path)
            except Exception as e2:
                raise EPUBExtractionError(
                    f"EPUB extraction failed: {e}\nFallback also failed: {e2}"
                ) from e

    def _extract_with_ebooklib(self, file_path: Path) -> ExtractedText:
        """Extract using ebooklib library."""
        book = epub.read_epub(str(file_path))

        title = self._get_dc_metadata(book, 'title')
        author = self._get_dc_metadata(book, 'creator')
        language = self._get_dc_metadata(book, 'language')

        toc = self._extract_toc_ebooklib(book)
        toc_map = self._build_toc_map(toc)

        # Extract text from all document items
        chapters_text = []
        chapters_metadata = []

        _sub_heading_re = re.compile(r'^h[2-6]$')

        doc_items = [
            i for i in book.get_items() if i.get_type() == ebooklib.ITEM_DOCUMENT
        ]
        use_filenames = self._filename_signals_usable(
            [i.get_name() for i in doc_items]
        )

        for item in doc_items:
            content = item.get_content()

            # Parse HTML once — extract text, h1, and sub-headings
            soup = BeautifulSoup(content, 'html.parser')
            for el in soup(['script', 'style']):
                el.decompose()

            h1 = soup.find('h1')
            chapter_title = h1.get_text(strip=True) if h1 else None

            text = self._clean_text(soup.get_text(separator='\n\n'))
            if not text.strip():
                continue

            item_name = item.get_name()
            toc_info = toc_map.get(item_name, {})

            # Section type determined at chapter level — sub-sections inherit.
            # Untitled items fall back to the filename: index and note files
            # routinely ship without a heading or TOC entry, and assuming
            # main content for those puts index entries into search results.
            display_title = chapter_title or toc_info.get('title') or ''
            chapter_section_type = self._detect_section_type(
                display_title, item_name if use_filenames else ''
            )

            # Split by sub-sections.  Prefer anchor-based splitting (uses
            # the unique element IDs from the TOC hrefs), then h2-h6 tags,
            # then TOC title text matching as last resort.
            toc_subs = toc_info.get('sub_sections', [])
            has_anchors = any(s.get('anchor') for s in toc_subs)

            if has_anchors:
                sections = self._split_html_by_anchors(soup, toc_subs)
            else:
                sub_headings = [
                    h.get_text(strip=True)
                    for h in soup.find_all(_sub_heading_re)
                ]
                if not sub_headings:
                    sub_headings = [s['title'] for s in toc_subs]
                sections = self._split_text_by_headings(text, sub_headings)

            for section in sections:
                if not section['text'].strip():
                    continue

                section_title = section['heading'] or toc_info.get('title')

                chapters_text.append(section['text'])
                chapters_metadata.append({
                    'chapter': display_title or item_name,
                    'section': toc_info.get('section'),
                    'section_title': section_title,
                    'section_type': chapter_section_type,
                    'file': item_name,
                })

        full_text = '\n\n---\n\n'.join(chapters_text)

        chunks = self._create_chunks_with_chapters(
            chapters_text, chapters_metadata, file_path, title, author
        )

        extraction_metadata = self._create_extraction_metadata(
            file_path=file_path,
            format_name='epub',
            extraction_time=0,
            total_chars=len(full_text),
            total_words=len(full_text.split()),
            total_chunks=len(chunks),
        )
        extraction_metadata.warnings.append("Extracted with ebooklib")

        return ExtractedText(
            full_text=full_text,
            chunks=chunks,
            metadata=extraction_metadata,
            toc=toc,
        )

    def _extract_manual(self, file_path: Path) -> ExtractedText:
        """
        Fallback: Manual extraction by unzipping EPUB.

        EPUB is a ZIP file containing HTML/XHTML files.
        """
        chapters_text = []

        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            html_files = [
                name for name in zip_ref.namelist()
                if name.endswith(('.html', '.xhtml', '.htm'))
                and not name.startswith('__MACOSX')
            ]

            for html_file in sorted(html_files):
                try:
                    text = self._extract_html_text(zip_ref.read(html_file))
                    if text.strip():
                        chapters_text.append(text)
                except Exception:
                    continue

        if not chapters_text:
            raise EPUBExtractionError("No readable content found in EPUB")

        full_text = '\n\n---\n\n'.join(chapters_text)

        base_metadata = ChunkMetadata(
            source_file=str(file_path),
            format='epub',
        )
        chunks = self._create_chunks(full_text, base_metadata)

        extraction_metadata = self._create_extraction_metadata(
            file_path=file_path,
            format_name='epub',
            extraction_time=0,
            total_chars=len(full_text),
            total_words=len(full_text.split()),
            total_chunks=len(chunks),
        )
        extraction_metadata.warnings.append("Extracted with manual ZIP method (fallback)")

        return ExtractedText(
            full_text=full_text,
            chunks=chunks,
            metadata=extraction_metadata,
        )

    def _extract_toc_ebooklib(self, book) -> List[Dict[str, Any]]:
        """
        Extract table of contents from EPUB.

        Returns list of TOC entries with:
        - title: Chapter/section title
        - level: Nesting level (1, 2, 3...)
        - section: Section number if present (e.g., "19.20")
        - href: Link to file in EPUB
        """
        toc = []

        def make_toc_entry(title: str, href: Optional[str], level: int) -> Dict[str, Any]:
            """Build a TOC entry dict, including section number if found."""
            entry = {'title': title, 'level': level, 'href': href}
            section_num = self._extract_section_number(title)
            if section_num:
                entry['section'] = section_num
            return entry

        def parse_toc_item(item, level=1, parent_section=''):
            if isinstance(item, tuple):
                section_obj = item[0]
                children = item[1] if len(item) > 1 else []

                title = section_obj.title if hasattr(section_obj, 'title') else str(section_obj)
                href = section_obj.href if hasattr(section_obj, 'href') else None

                entry = make_toc_entry(title, href, level)
                toc.append(entry)

                section_num = entry.get('section', parent_section)
                for child in children:
                    parse_toc_item(child, level + 1, section_num)

            elif isinstance(item, list):
                for sub_item in item:
                    parse_toc_item(sub_item, level, parent_section)

            elif hasattr(item, 'href') and hasattr(item, 'title'):
                # ebooklib Link object (child of a tuple section)
                title = item.title if item.title else str(item)
                href = item.href if item.href else None
                toc.append(make_toc_entry(title, href, level))

        try:
            toc_items = book.toc
            if not toc_items:
                return toc

            if isinstance(toc_items, list):
                for item in toc_items:
                    if hasattr(item, 'href') and hasattr(item, 'title'):
                        # Link object (most common format)
                        title = item.title if item.title else str(item)
                        href = item.href if item.href else None
                        toc.append(make_toc_entry(title, href, level=1))
                    elif isinstance(item, tuple):
                        parse_toc_item(item, 1, '')
            elif isinstance(toc_items, tuple):
                parse_toc_item(toc_items, 1, '')
        except Exception as e:
            logger.warning("TOC extraction failed: %s: %s", type(e).__name__, e)

        return toc

    @staticmethod
    def _get_dc_metadata(book, field: str) -> Optional[str]:
        """Extract a Dublin Core metadata field from an EPUB book."""
        values = book.get_metadata('DC', field)
        return values[0][0] if values else None

    @staticmethod
    def _build_toc_map(toc: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Build a mapping from href (without anchor) to TOC entry info.

        Each file entry contains the chapter-level title plus a list of
        sub-section dicts ``[{'title': ..., 'anchor': ...}, ...]`` for
        level-2+ TOC entries that point into the same file.
        """
        toc_map: Dict[str, Dict[str, Any]] = {}
        for entry in toc:
            href = entry.get('href')
            if not href:
                continue
            parts = href.split('#', 1)
            href_base = parts[0]
            anchor = parts[1] if len(parts) > 1 else None
            if href_base not in toc_map:
                toc_map[href_base] = {
                    'section': entry.get('section'),
                    'title': entry.get('title'),
                    'level': entry.get('level', 1),
                    'sub_sections': [],
                }
            elif entry.get('level', 1) > 1:
                toc_map[href_base]['sub_sections'].append({
                    'title': entry.get('title', ''),
                    'anchor': anchor,
                })
        return toc_map

    @staticmethod
    def _extract_section_number(title: str) -> Optional[str]:
        """
        Extract section number from TOC title.

        Handles formats like:
        - "19.20 Land Warfare" -> "19.20"
        - "Chapter 3" -> "3"
        - "3.4.2 Tactics" -> "3.4.2"
        """
        match = _SECTION_NUM_START.match(title)
        if match:
            return match.group(1)

        match = _SECTION_NUM_LABEL.search(title)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _filename_signals_usable(item_names: List[str]) -> bool:
        """True when paratext filenames actually distinguish anything.

        Some conversion tools name every file after the source document, so a
        whole book arrives as ``index_split_000.html`` … ``index_split_412.html``.
        There the word describes the converter, not the section, and honouring
        it would file the entire book under back matter — measured against the
        live library, that was 45 books, one of them losing 1088 of 1090 chunks.

        Paratext is a demarcation: where no file lacks the marker, nothing is
        being demarcated, so the signal is discarded for that book.
        """
        if not item_names:
            return False
        return any(
            not contains_keyword(_normalize_item_name(name), _FILENAME_PARATEXT)
            for name in item_names
        )

    @staticmethod
    def _detect_section_type(title: str, filename: str = "") -> str:
        """
        Detect if section is front matter, main content, or back matter.

        Args:
            title: Chapter heading or TOC title. Authoritative when present.
            filename: EPUB item name, used only when there is no title —
                many EPUBs ship their index, notes and front matter without
                a heading or TOC entry, and the filename is then the only
                identifier left (``index_split_033.html``).

        Returns:
            'front_matter', 'main_content', or 'back_matter'
        """
        # Word-boundary matching (finding 2.2): substring checks turned
        # 'protocol' into front matter ('toc') and excluded chapters.
        if contains_keyword(title, _FRONT_MATTER_PATTERNS):
            return SectionType.FRONT_MATTER
        if contains_keyword(title, _BACK_MATTER_PATTERNS):
            return SectionType.BACK_MATTER
        if title:
            # A readable heading outranks a packaging convention: a chapter
            # that merely lives in index_split_*.html is still a chapter.
            return SectionType.MAIN_CONTENT

        normalized = _normalize_item_name(filename)
        if contains_keyword(normalized, _FILENAME_FRONT_MATTER):
            return SectionType.FRONT_MATTER
        if contains_keyword(normalized, _FILENAME_BACK_MATTER):
            return SectionType.BACK_MATTER
        return SectionType.MAIN_CONTENT

    @staticmethod
    def _split_text_by_headings(
        text: str, heading_texts: List[str]
    ) -> List[Dict[str, Any]]:
        """Split extracted text into sections at sub-heading boundaries.

        Args:
            text: Full extracted text from one HTML item.
            heading_texts: Texts of h2-h6 headings found in the HTML.

        Returns:
            List of dicts with 'heading' (str or None) and 'text' (str).
            The intro before the first heading gets heading=None.
        """
        if not heading_texts:
            return [{'heading': None, 'text': text}]

        normalized_headings = {' '.join(h.split()) for h in heading_texts}
        paragraphs = text.split('\n\n')
        sections: List[Dict[str, Any]] = []
        current_heading: Optional[str] = None
        current_paras: List[str] = []

        for para in paragraphs:
            normalized = ' '.join(para.strip().split())
            if normalized in normalized_headings:
                if current_paras:
                    sections.append({
                        'heading': current_heading,
                        'text': '\n\n'.join(current_paras),
                    })
                current_heading = para.strip()
                current_paras = [para]  # include heading in section text
            else:
                current_paras.append(para)

        if current_paras:
            sections.append({
                'heading': current_heading,
                'text': '\n\n'.join(current_paras),
            })

        return sections

    def _split_html_by_anchors(
        self,
        soup: 'BeautifulSoup',
        sub_sections: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Split parsed HTML into sections using TOC anchor IDs.

        Each sub-section dict must have ``'title'`` and ``'anchor'`` keys.
        The anchor is looked up as an ``id`` attribute in the HTML tree.
        Text between consecutive anchors forms one section.

        Returns:
            List of ``{'heading': str|None, 'text': str}`` dicts.
        """
        # Collect (element, title) pairs for anchors that exist in the HTML
        markers: List[tuple] = []
        for sub in sub_sections:
            anchor = sub.get('anchor')
            if not anchor:
                continue
            el = soup.find(id=anchor)
            if el:
                markers.append((el, sub['title']))

        if not markers:
            text = self._clean_text(soup.get_text(separator='\n\n'))
            return [{'heading': None, 'text': text}]

        # Walk the body's descendants in document order.  For each marker
        # element, record where it appears so we can split the flat text.
        body = soup.find('body') or soup
        all_strings = list(body.stripped_strings)
        full_text = '\n\n'.join(all_strings)

        # Build character offsets for each marker in the joined text.
        # We find each marker's text content and locate it sequentially.
        marker_texts = [(el.get_text(strip=True), title) for el, title in markers]
        split_points: List[tuple] = []  # (char_offset, title)
        search_from = 0
        for marker_text, title in marker_texts:
            idx = full_text.find(marker_text, search_from)
            if idx >= 0:
                split_points.append((idx, title))
                search_from = idx + len(marker_text)

        if not split_points:
            return [{'heading': None, 'text': self._clean_text(full_text)}]

        sections: List[Dict[str, Any]] = []

        # Intro text before the first marker
        intro = full_text[:split_points[0][0]].strip()
        if intro:
            sections.append({'heading': None, 'text': self._clean_text(intro)})

        # Sections between markers
        for i, (offset, title) in enumerate(split_points):
            end = split_points[i + 1][0] if i + 1 < len(split_points) else len(full_text)
            section_text = full_text[offset:end].strip()
            if section_text:
                sections.append({
                    'heading': title,
                    'text': self._clean_text(section_text),
                })

        return sections

    def _extract_html_text(self, content: bytes) -> str:
        """Extract and clean text from HTML/XHTML content."""
        soup = BeautifulSoup(content, 'html.parser')
        for element in soup(['script', 'style']):
            element.decompose()
        text = soup.get_text(separator='\n\n')
        return self._clean_text(text)

    def _create_chunks_with_chapters(
        self,
        chapters_text: List[str],
        chapters_metadata: List[Dict],
        file_path: Path,
        title: str,
        author: str
    ) -> List[Dict[str, Any]]:
        """Create chunks with chapter and section information."""
        chunks = []

        for chapter_text, chapter_meta in zip(chapters_text, chapters_metadata):
            base_metadata = ChunkMetadata(
                source_file=str(file_path),
                format='epub',
                title=title,
                author=author,
                chapter=chapter_meta.get('chapter'),
                section=chapter_meta.get('section'),
                section_title=chapter_meta.get('section_title'),
                section_type=chapter_meta.get('section_type', SectionType.MAIN_CONTENT),
            )

            chapter_chunks = self._create_chunks(chapter_text, base_metadata)
            chunks.extend(chapter_chunks)

        return chunks

    @staticmethod
    def _clean_text(text: str) -> str:
        """Clean extracted text by collapsing whitespace and blank lines."""
        lines = [line.strip() for line in text.split('\n')]
        lines = [line for line in lines if line]
        return '\n\n'.join(lines)
