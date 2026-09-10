"""Extractor for Scriptor bundles: a prepared master and its pagination sidecar.

A master is Markdown in the prepared format (archilles-scriptor,
docs/PREPARED_FORMAT_SPEC.md): page markers carrying the printed label, region
markers naming front matter and apparatus, footnote anchors with their
definitions collected at the end. Scriptor has done what the PDF path guesses
at -- running heads removed, paragraphs reflowed across pages, notes bound to
their anchors -- so this extractor reads and cuts, nothing more.

The grammar is Scriptor's and is read through ``scriptor.document``; nothing
here parses a page or region marker itself. What is Archilles' own is the
mapping onto its chunk metadata: region to section_type, sidecar page to
page_number, and how a chunk is composed under the chunking strategy the master
declares (spec §4.1).
"""

from __future__ import annotations

import bisect
import itertools
import re
import time
from dataclasses import dataclass
from pathlib import Path

from scriptor.document import Bundle, ParsedDoc, load_bundle, parse_prepared, region_at
from scriptor.reflow.pagelabel import PAGE_MARKER_RE
from scriptor.reflow.regions import APPARATUS, read_metadata_block

from src.archilles.constants import SectionType
from .base import BaseExtractor
from .exceptions import ExtractionError
from .language_detector import LanguageDetector
from .models import ChunkMetadata, ExtractedText

# The spec's major version this reader understands. Minor versions are additive
# and read under the spec's tolerance rules (unknown region -> running text,
# unknown label source passed through, unknown field ignored); a new major is
# refused, never indexed on a guess (spec §11).
SUPPORTED_SPEC_MAJOR = 0

_MASTER_SUFFIXES = frozenset({'.md', '.markdown'})
# Enough for the metadata block, which opens the master (spec §4.1).
_SNIFF_BYTES = 4096

_FRONT_MATTER_REGIONS = frozenset({'front-matter', 'contents'})

# The anchor after the first marker of a page a TOC links to (spec §4.2).
_PAGE_ANCHOR_RE = re.compile(r"\{#p-[^}]*\}")
# Literal * and _ are backslash-escaped in the master (spec §4.5).
_ESCAPE_RE = re.compile(r"\\([*_])")
_HEADING_RE = re.compile(r"(#{1,6})[ \t]+(?=\S)")
# A footnote anchor standing as a word of its own (spec §4.3).
_ANCHOR_WORD_RE = re.compile(r"\[\^\d+\]")
_PARA_BREAK_RE = re.compile(r"\n[ \t]*\n\s*")
_WORD_RE = re.compile(r"\S+")


def region_to_section_type(region: str) -> str:
    """Archilles' reading of a region name (spec §4.4).

    Apparatus leaves the default search, and so do front matter and the table
    of contents, as on the EPUB and PDF paths. Everything else is running text:
    ``main``, unmarked text, ``preface`` -- the spec's rule, confirmed by the
    user on 2026-09-10, acknowledgements under a preface included -- and any
    name this reader does not know, which the spec says to read as running text.
    """
    if region in APPARATUS:
        return SectionType.BACK_MATTER
    if region in _FRONT_MATTER_REGIONS:
        return SectionType.FRONT_MATTER
    return SectionType.MAIN_CONTENT


@dataclass(frozen=True)
class _Page:
    """The address a stretch of text is cited under."""
    label: str | None = None
    pos: int = 0                  # physical page; 0 where no sidecar says
    source: str | None = None     # the sidecar's witness for the label


@dataclass(frozen=True)
class _Unit:
    """One paragraph of the full text, with the definitions bound to it."""
    start: int
    end: int
    region: str
    chapter: str | None
    section_title: str | None

    @property
    def section(self) -> tuple:
        return self.region, self.chapter, self.section_title


class _Composition:
    """The full text as the index sees it, and the address of every position in it."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.length = 0
        self.units: list[_Unit] = []
        self.headings: list[tuple[int, int, str]] = []   # (offset, level, title)
        self._offsets: list[int] = []
        self._pages: list[_Page] = []

    def add(self, text: str) -> int:
        """Append a paragraph; return where it starts."""
        if self._parts:
            self._parts.append('\n\n')
            self.length += 2
        start = self.length
        self._parts.append(text)
        self.length += len(text)
        return start

    def mark(self, offset: int, page: _Page) -> None:
        """From ``offset`` on, the text is on ``page``."""
        i = bisect.bisect_right(self._offsets, offset)
        self._offsets.insert(i, offset)
        self._pages.insert(i, page)

    def page_at(self, offset: int) -> _Page:
        i = bisect.bisect_right(self._offsets, offset) - 1
        return self._pages[i] if i >= 0 else _Page()

    @property
    def text(self) -> str:
        return ''.join(self._parts)


class ScriptorExtractor(BaseExtractor):
    """Read a Scriptor master, and the pagination sidecar beside it, into chunks."""

    def supports(self, file_path: Path) -> bool:
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

    def extract(self, file_path: Path) -> ExtractedText:
        file_path = Path(file_path)
        started = time.time()
        try:
            bundle = load_bundle(file_path)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise ExtractionError(f"Cannot read Scriptor bundle {file_path.name}: {e}") from e
        if bundle is None:
            raise ExtractionError(f"{file_path.name} is not a Scriptor master: no format_version")
        _check_version(bundle.format_version, file_path)

        doc = parse_prepared(bundle.text)
        comp, orphans = _compose(bundle, doc, scientific=bundle.chunking_strategy == 'scientific')
        full_text = comp.text
        chunks = self._chunks(comp, full_text, file_path, bundle.format_version)

        metadata = self._create_extraction_metadata(
            file_path=file_path,
            format_name='scriptor',
            extraction_time=time.time() - started,
            total_chars=len(full_text),
            total_words=len(full_text.split()),
            total_chunks=len(chunks),
        )
        if orphans:
            metadata.warnings.append(
                f"{orphans} footnote definition(s) without an anchor, appended at the end")
        toc = [
            {'level': level, 'title': title,
             'page': comp.page_at(offset).pos, 'page_label': comp.page_at(offset).label}
            for offset, level, title in comp.headings
        ]
        return ExtractedText(full_text=full_text, chunks=chunks, metadata=metadata, toc=toc)

    def _chunks(self, comp: _Composition, full_text: str, file_path: Path,
                producer_version: str) -> list[dict]:
        chunks = []
        for units in _sections(comp.units):
            first = units[0]
            for start, end in self._spans(full_text, units):
                page = comp.page_at(start)
                meta = ChunkMetadata(
                    source_file=str(file_path),
                    format='scriptor',
                    page=page.pos,
                    page_label=page.label,
                    label_source=page.source,
                    chapter=first.chapter,
                    section_title=first.section_title,
                    section_type=region_to_section_type(first.region),
                    region=first.region,
                    producer_version=producer_version,
                    char_start=start,
                    char_end=end,
                )
                chunks.append({'text': full_text[start:end], 'metadata': meta.__dict__})

        self._add_window_text(chunks, full_text, 500)
        if LanguageDetector.is_available():
            chunks = LanguageDetector.detect_for_chunks(chunks)
        return chunks

    def _spans(self, text: str, units: list[_Unit]) -> list[tuple[int, int]]:
        """Chunk boundaries over one section, as (start, end) offsets into ``text``.

        Whole paragraphs are gathered up to chunk_size, as ``_create_chunks``
        does; a paragraph longer than that is cut at the last sentence end that
        keeps 40 % of the window, as ``_split_para_by_words`` does. Each chunk
        after the first opens with the last sentences of the one before, up to
        ``overlap`` tokens, as on the PDF path. Offsets rather than strings: every
        chunk is a slice of the full text, so its first character -- the one
        whose page is the chunk's address -- is known exactly.
        """
        words = [m.span() for m in _WORD_RE.finditer(text, units[0].start, units[-1].end)]
        if not words:
            return []
        starts = [w[0] for w in words]
        breaks = [bisect.bisect_left(starts, u.end) for u in units]   # word index after each unit
        max_words = max(1, int(self.chunk_size / 1.3))
        overlap_words = max(0, int(self.overlap / 1.3))

        spans = []
        first = new = 0          # first word of the chunk; first word not in any chunk yet
        while new < len(words):
            i = bisect.bisect_right(breaks, new + max_words) - 1
            if i >= 0 and breaks[i] > new:
                end = breaks[i]
            else:
                end = _cut(text, words, new, min(len(words), new + max_words))
            spans.append((words[first][0], words[end - 1][1]))
            if end >= len(words):
                break
            first = _overlap_start(text, words, new, end, overlap_words)
            new = end
        return spans


def _check_version(version: str, path: Path) -> None:
    major = version.split('.', 1)[0]
    if not major.isdigit() or int(major) != SUPPORTED_SPEC_MAJOR:
        raise ExtractionError(
            f"{path.name}: prepared-format version {version} is not readable here "
            f"(this reader supports {SUPPORTED_SPEC_MAJOR}.x) -- refused, not guessed")


def _compose(bundle: Bundle, doc: ParsedDoc, *, scientific: bool) -> tuple[_Composition, int]:
    """Clean the body paragraph by paragraph and note where every page begins.

    Returns the composition and the number of definitions without an anchor.
    """
    body = doc.body
    marks = _effective_marks(bundle, doc)
    notes = sorted((f for f in doc.footnotes if f.anchor_offset is not None),
                   key=lambda f: f.anchor_offset)
    comp = _Composition()
    mi = ni = 0
    region: str | None = None
    chapter = section_title = None
    heading_run: int | None = None     # start of the headings still waiting for their page

    for ps, pe in _paragraphs(body):
        if (here := region_at(doc, ps)) != region:
            # A region is a new frame; the last chapter heading does not govern it.
            region, chapter, section_title = here, None, None

        inner = []
        while mi < len(marks) and marks[mi][0] < pe:
            inner.append(marks[mi])
            mi += 1
        text, changes = _clean(body, ps, pe, inner)

        heading = _HEADING_RE.match(text)
        if heading:
            cut = heading.end()
            text = text[cut:]
            changes = [(max(0, off - cut), page) for off, page in changes]
            level, title = len(heading.group(1)), text.split('\n', 1)[0].strip()
            if level == 1:
                chapter, section_title = title, None
            else:
                section_title = title

        if scientific:
            while ni < len(notes) and notes[ni].anchor_offset < pe:
                text += f"\n\n[^{notes[ni].ident}]: {_plain(notes[ni].definition)}"
                ni += 1

        if not text:
            # A paragraph that was nothing but a marker: its page still turns.
            for _, page in changes:
                comp.mark(comp.length, page)
            continue

        start = comp.add(text)
        comp.units.append(_Unit(start, comp.length, region, chapter, section_title))
        for i, (off, page) in enumerate(changes):
            # Scriptor keeps a page marker out of a heading and sets it before
            # the next block instead ("A pending page marker is NOT pulled into
            # the heading", reflow/core.py): a heading that opens a page stands
            # before that page's marker. The marker opening the block after a
            # heading therefore addresses the heading too.
            at = heading_run if (i == 0 and off == 0 and heading_run is not None) else start + off
            comp.mark(at, page)
        if heading:
            comp.headings.append((start, level, title))
            if heading_run is None:
                heading_run = start
        else:
            heading_run = None

    orphans = [f for f in doc.footnotes if f.anchor_offset is None] if scientific else []
    if orphans:
        # Only a user edit leaves a definition without its anchor (Scriptor gives
        # hanging notes a synthetic one, spec §4.3). Kept rather than dropped.
        start = comp.add('\n\n'.join(f"[^{f.ident}]: {_plain(f.definition)}" for f in orphans))
        comp.units.append(_Unit(start, comp.length, region or '', chapter, section_title))
    return comp, len(orphans)


def _effective_marks(bundle: Bundle, doc: ParsedDoc) -> list[tuple[int, int, _Page]]:
    """The page markers that turn the page, as (start, end, page) in body offsets.

    With sidecar pages, a marker the sidecar does not resolve is book text that
    looks like one ("Midrash Tanhuma B 1 [p. 73, ed. Buber]") and turns nothing;
    it stays in the text. Without them the two cannot be told apart, and every
    marker counts. Labels repeat in volumes in parts, so markers are resolved by
    position, not by label.
    """
    body = doc.body
    entries = bundle.resolve_marks(doc) if bundle.pages else [None] * len(doc.page_marks)
    marks = []
    for (label, start), entry in zip(doc.page_marks, entries):
        if bundle.pages and entry is None:
            continue
        end = PAGE_MARKER_RE.match(body, start).end()
        anchor = _PAGE_ANCHOR_RE.match(body, end)
        if anchor:
            end = anchor.end()
        marks.append((start, end, _Page(label, entry.pos, entry.source) if entry else _Page(label)))
    return marks


def _paragraphs(text: str):
    """(start, end) of every paragraph, surrounding whitespace excluded."""
    start = 0
    for brk in itertools.chain(_PARA_BREAK_RE.finditer(text), [None]):
        end = brk.start() if brk else len(text)
        chunk = text[start:end]
        if chunk.strip():
            yield start + len(chunk) - len(chunk.lstrip()), start + len(chunk.rstrip())
        if brk:
            start = brk.end()


def _clean(body: str, start: int, end: int,
           marks: list[tuple[int, int, _Page]]) -> tuple[str, list[tuple[int, _Page]]]:
    """body[start:end] without its page markers, and where each of them took effect.

    A removed marker takes the space beside it along, so the words on either
    side keep a single one between them.
    """
    text = ''
    changes = []
    cursor = start
    for m_start, m_end, page in marks:
        text = _join(text, _plain(body[cursor:m_start]))
        changes.append((len(text), page))
        cursor = m_end
    text = _join(text, _plain(body[cursor:end])).rstrip()
    return text, [(min(off, len(text)), page) for off, page in changes]


def _join(text: str, piece: str) -> str:
    if not text or text[-1].isspace():
        piece = piece.lstrip(' \t')
    return text + piece


def _plain(text: str) -> str:
    return _ESCAPE_RE.sub(r'\1', _PAGE_ANCHOR_RE.sub('', text))


def _sections(units: list[_Unit]) -> list[list[_Unit]]:
    """Runs of paragraphs under one region, chapter and section: a chunk never
    spans two, so its section_type holds for all of its text."""
    return [list(run) for _, run in itertools.groupby(units, key=lambda u: u.section)]


def _starts_sentence(text: str, words: list[tuple[int, int]], k: int) -> bool:
    """Whether a sentence begins at word k. Scriptor sets an anchor after the
    full stop it belongs to ("... Satz. [^276] Außerdem"), so no sentence
    begins at an anchor, and one begins after it where the word before closed."""
    if _ANCHOR_WORD_RE.fullmatch(text, *words[k]):
        return False
    j = k - 1
    while j > 0 and _ANCHOR_WORD_RE.fullmatch(text, *words[j]):
        j -= 1
    return j >= 0 and BaseExtractor._SENTENCE_END_RE.match(text[words[j][1] - 1] + ' ') is not None


def _cut(text: str, words: list[tuple[int, int]], start: int, limit: int) -> int:
    """End of a window over a paragraph too long for one chunk: before the
    last sentence that begins late enough to keep 40 % of the window, else at
    the word limit."""
    shortest = start + max(1, int(0.4 * (limit - start)))
    for end in range(limit, shortest - 1, -1):
        if end >= len(words) or _starts_sentence(text, words, end):
            return end
    return limit


def _overlap_start(text: str, words: list[tuple[int, int]], new: int, end: int,
                   overlap_words: int) -> int:
    """First word of the next chunk: the tail of words[new:end], at most
    ``overlap_words`` long, beginning at a sentence where one keeps 40 % of it."""
    if overlap_words <= 0:
        return end
    tail = max(new, end - overlap_words)
    for k in range(tail, end):
        if k > new and _starts_sentence(text, words, k):
            return k if end - k >= 0.4 * overlap_words else tail
    return tail
