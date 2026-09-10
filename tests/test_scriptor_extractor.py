"""ScriptorExtractor: a Scriptor bundle read into Archilles chunks.

The fixture is a small volume in the prepared format (PREPARED_FORMAT_SPEC) with
its pagination sidecar: a roman front matter, a table of contents, a preface,
two chapters with a page break inside a paragraph, a footnote and a hanging one,
book text that looks like a page marker, a bibliography, and a region this reader
does not know. Expected values are written out by hand from the fixture.
"""
import json

import pytest

from src.archilles.constants import SectionType
from src.extractors.exceptions import ExtractionError
from src.extractors.scriptor_extractor import ScriptorExtractor, region_to_section_type
from src.extractors.universal_extractor import UniversalExtractor

HEAD = ("---\nformat_version: {version}\nchunking_strategy: {strategy}\n"
        "pagination: bottom edge, 80% of pages attested\n---\n\n")

BODY = r"""[region: front-matter]

[p. iii] Titelei des Bandes
Verlag und Ort

[region: contents]

## Inhalt

- [Einleitung](#p-1) — p. 1

[region: preface]

[p. v] Das Vorwort erklärt, warum dieses Buch geschrieben wurde.

[region: main]

# Einleitung

[p. 1]{#p-1} Der erste Absatz trägt eine Note [^1] und läuft über die Seite [p. 2] auf die zweite.

## Ein Abschnitt

Der zweite Absatz nennt \*Abbasiden und A\_B und endet mit einer hängenden Note. [^2] Midrash Tanhuma B 1 [p. 73, ed. Buber] ist Buchtext, kein Seitenwechsel.

# Zweites Kapitel

Das zweite Kapitel beginnt ohne neue Seite.

[region: bibliography]

[p. 3] Müller, Hans: Ein Titel. Berlin 1999.

[region: glossary]

Glossar: ein Eintrag, den dieser Leser nicht kennt.

[^1]: Die erste Note.

[^2]: Die hängende Note,
mit einer Fortsetzungszeile.
"""

PAGES = [
    {"pos": 3, "label": "iii", "source": "printed", "confidence": 1.0},
    {"pos": 5, "label": "v", "source": "printed", "confidence": 1.0},
    {"pos": 7, "label": "1", "source": "printed", "confidence": 1.0},
    {"pos": 8, "label": "2", "source": "computed", "confidence": 0.5},
    {"pos": 9, "label": "3", "source": "printed", "confidence": 1.0},
]


def _master(tmp_path, *, body=BODY, strategy="scientific", pages=PAGES, version="0.3.0"):
    master = tmp_path / "book.md"
    master.write_text(HEAD.format(version=version, strategy=strategy) + body, encoding="utf-8")
    if pages is not None:
        sidecar = {"version": 1, "profile": {"edge": "bottom", "attested": 0.8, "band": None},
                   "segments": [], "pages": pages, "rejected": []}
        (tmp_path / "book.md.pagination.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return master


def _extract(master, **kwargs):
    return ScriptorExtractor(**kwargs).extract(master)


def _chunk(result, first_words):
    found = [c for c in result.chunks if c["text"].startswith(first_words)]
    assert len(found) == 1, [c["text"][:30] for c in result.chunks]
    return found[0]


def _address(chunk):
    m = chunk["metadata"]
    return m["page_label"], m["page"], m["label_source"]


# the page ---------------------------------------------------------------------

def test_the_printed_label_is_kept_verbatim_and_the_physical_page_comes_from_the_sidecar(tmp_path):
    result = _extract(_master(tmp_path))
    assert _address(_chunk(result, "Titelei des Bandes")) == ("iii", 3, "printed")
    assert _address(_chunk(result, "Das Vorwort")) == ("v", 5, "printed")


def test_a_page_break_inside_a_paragraph_addresses_the_text_after_it(tmp_path):
    # [p. 2] stands mid-paragraph; the next block starts on page 2.
    result = _extract(_master(tmp_path))
    assert _address(_chunk(result, "Ein Abschnitt")) == ("2", 8, "computed")


def test_a_heading_takes_the_page_of_the_marker_that_opens_the_next_block(tmp_path):
    """Scriptor keeps a page marker out of a heading and sets it before the
    following paragraph, so read strictly every chapter opening would be cited
    as the page before it."""
    result = _extract(_master(tmp_path))
    assert _address(_chunk(result, "Einleitung")) == ("1", 7, "printed")


def test_book_text_that_looks_like_a_marker_stays_text_and_moves_no_page(tmp_path):
    result = _extract(_master(tmp_path))
    assert "Midrash Tanhuma B 1 [p. 73, ed. Buber] ist Buchtext" in _chunk(result, "Ein Abschnitt")["text"]
    assert _address(_chunk(result, "Zweites Kapitel")) == ("2", 8, "computed")


def test_without_a_sidecar_every_marker_counts_and_the_physical_page_is_unknown(tmp_path):
    result = _extract(_master(tmp_path, pages=None))
    assert _address(_chunk(result, "Titelei des Bandes")) == ("iii", 0, None)
    assert _address(_chunk(result, "Einleitung")) == ("1", 0, None)
    # Nothing tells book text from a marker here, so the look-alike moves the page.
    assert "[p. 73" not in _chunk(result, "Ein Abschnitt")["text"]
    assert _address(_chunk(result, "Zweites Kapitel")) == ("73, ed. Buber", 0, None)


def test_a_volume_without_page_markers_has_no_address(tmp_path):
    # Seven of thirty library volumes in M1 carry no marker at all.
    result = _extract(_master(tmp_path, body="Ein Text ohne Seitenzahl.\n", pages=[]))
    assert [_address(c) for c in result.chunks] == [(None, 0, None)]


# the region -------------------------------------------------------------------

@pytest.mark.parametrize("region, section_type", [
    ("front-matter", SectionType.FRONT_MATTER),
    ("contents", SectionType.FRONT_MATTER),
    ("preface", SectionType.MAIN_CONTENT),       # user decision 2026-09-10
    ("main", SectionType.MAIN_CONTENT),
    ("", SectionType.MAIN_CONTENT),              # unmarked text is main (spec §4.4)
    ("bibliography", SectionType.BACK_MATTER),
    ("index", SectionType.BACK_MATTER),
    ("abbreviations", SectionType.BACK_MATTER),
    ("notes", SectionType.BACK_MATTER),
    ("appendix", SectionType.BACK_MATTER),
    ("glossary", SectionType.MAIN_CONTENT),      # unknown is running text (spec §4.4)
])
def test_region_to_section_type(region, section_type):
    assert region_to_section_type(region) == section_type


def test_each_chunk_carries_its_region_verbatim_beside_the_section_type(tmp_path):
    result = _extract(_master(tmp_path))
    got = [(c["metadata"]["region"], c["metadata"]["section_type"]) for c in result.chunks]
    assert got == [
        ("front-matter", SectionType.FRONT_MATTER),
        ("contents", SectionType.FRONT_MATTER),
        ("preface", SectionType.MAIN_CONTENT),
        ("main", SectionType.MAIN_CONTENT),
        ("main", SectionType.MAIN_CONTENT),
        ("main", SectionType.MAIN_CONTENT),
        ("bibliography", SectionType.BACK_MATTER),
        ("glossary", SectionType.MAIN_CONTENT),
    ]


def test_text_before_any_region_marker_is_main(tmp_path):
    result = _extract(_master(tmp_path, body="[p. 1] Ein Satz ohne Region.\n", pages=None))
    meta = result.chunks[0]["metadata"]
    assert (meta["region"], meta["section_type"]) == ("", SectionType.MAIN_CONTENT)


def test_a_chunk_never_spans_two_regions(tmp_path):
    result = _extract(_master(tmp_path), chunk_size=100_000)
    assert _chunk(result, "Müller")["text"] == "Müller, Hans: Ein Titel. Berlin 1999."


# headings ---------------------------------------------------------------------

@pytest.mark.parametrize("first_words, chapter, section_title", [
    ("Titelei des Bandes", None, None),
    ("Inhalt", None, "Inhalt"),
    ("Das Vorwort", None, None),                   # a new region opens a new frame
    ("Einleitung", "Einleitung", None),
    ("Ein Abschnitt", "Einleitung", "Ein Abschnitt"),
    ("Zweites Kapitel", "Zweites Kapitel", None),  # a chapter ends the section
    ("Müller", None, None),
    ("Glossar", None, None),
])
def test_headings_set_chapter_and_section(tmp_path, first_words, chapter, section_title):
    meta = _chunk(_extract(_master(tmp_path)), first_words)["metadata"]
    assert (meta["chapter"], meta["section_title"]) == (chapter, section_title)


def test_toc_lists_the_headings_with_their_address(tmp_path):
    result = _extract(_master(tmp_path))
    assert result.toc == [
        {"level": 2, "title": "Inhalt", "page": 3, "page_label": "iii"},
        {"level": 1, "title": "Einleitung", "page": 7, "page_label": "1"},
        {"level": 2, "title": "Ein Abschnitt", "page": 8, "page_label": "2"},
        {"level": 1, "title": "Zweites Kapitel", "page": 8, "page_label": "2"},
    ]


# footnotes --------------------------------------------------------------------

def test_scientific_attaches_each_definition_to_the_paragraph_of_its_anchor(tmp_path):
    result = _extract(_master(tmp_path))
    assert _chunk(result, "Einleitung")["text"] == (
        "Einleitung\n\n"
        "Der erste Absatz trägt eine Note [^1] und läuft über die Seite auf die zweite.\n\n"
        "[^1]: Die erste Note.")
    # The hanging note binds like any other, continuation line included.
    assert _chunk(result, "Ein Abschnitt")["text"].endswith(
        "[^2] Midrash Tanhuma B 1 [p. 73, ed. Buber] ist Buchtext, kein Seitenwechsel.\n\n"
        "[^2]: Die hängende Note,\nmit einer Fortsetzungszeile.")


def test_basic_indexes_no_definition_but_keeps_the_anchors(tmp_path):
    result = _extract(_master(tmp_path, strategy="basic"))
    text = "\n".join(c["text"] for c in result.chunks)
    assert "Die erste Note" not in text and "Die hängende Note" not in text
    assert "[^1]" in text and "[^2]" in text


def test_a_definition_without_an_anchor_is_kept_and_reported(tmp_path):
    # A user edit can delete an anchor; Pandoc would drop the note silently.
    body = "[p. 1] Ein Absatz ohne Anker.\n\n[^1]: Eine verwaiste Note.\n"
    result = _extract(_master(tmp_path, body=body, pages=None))
    assert result.chunks[-1]["text"].endswith("\n\n[^1]: Eine verwaiste Note.")
    assert any("without an anchor" in w for w in result.metadata.warnings)


# what reaches the index -------------------------------------------------------

def test_no_marker_reaches_the_chunk_text_and_escapes_are_resolved(tmp_path):
    result = _extract(_master(tmp_path))
    text = "\n".join(c["text"] for c in result.chunks)
    for token in ("[p. iii]", "[p. v]", "[p. 1]", "[p. 2]", "[p. 3]", "{#p-", "[region:", "\\*", "\\_"):
        assert token not in text
    assert "nennt *Abbasiden und A_B und" in text


def test_offsets_address_the_cleaned_full_text(tmp_path):
    result = _extract(_master(tmp_path))
    assert "[p. 1]" not in result.full_text
    for c in result.chunks:
        m = c["metadata"]
        assert result.full_text[m["char_start"]:m["char_end"]] == c["text"]
        assert c["text"] in c["window_text"]


def test_every_chunk_names_the_format_and_the_spec_version(tmp_path):
    result = _extract(_master(tmp_path))
    assert result.metadata.detected_format == "scriptor"
    assert {(c["metadata"]["format"], c["metadata"]["producer_version"])
            for c in result.chunks} == {("scriptor", "0.3.0")}


def test_a_major_version_this_reader_does_not_know_is_refused(tmp_path):
    with pytest.raises(ExtractionError, match="1.0.0"):
        _extract(_master(tmp_path, version="1.0.0"))


# cutting ----------------------------------------------------------------------

def _sentences(first, last):
    return " ".join(f"W{i} zwei drei vier fünf." for i in range(first, last + 1))


def test_an_oversized_paragraph_is_cut_at_sentences_and_each_part_keeps_its_own_page(tmp_path):
    # 60 words, page 11 from W7 on; a chunk holds 20 words (27 tokens / 1.3).
    body = f"[p. 10] {_sentences(1, 6)} [p. 11] {_sentences(7, 12)}\n"
    result = _extract(_master(tmp_path, body=body, pages=None), chunk_size=27, overlap=0)
    assert [(c["text"][:3], c["metadata"]["page_label"]) for c in result.chunks] == [
        ("W1 ", "10"), ("W5 ", "10"), ("W9 ", "11")]


def test_the_next_chunk_opens_with_the_last_sentence_of_the_one_before(tmp_path):
    # Overlap 7 tokens = 5 words: exactly the last sentence of the first paragraph.
    body = f"[p. 20] {_sentences(1, 4)}\n\n[p. 21] {_sentences(5, 8)}\n"
    result = _extract(_master(tmp_path, body=body, pages=None), chunk_size=27, overlap=7)
    assert [c["text"] for c in result.chunks] == [
        _sentences(1, 4),
        _sentences(4, 4) + "\n\n" + _sentences(5, 8),
    ]
    # A chunk is addressed by its first character, which is still on page 20.
    assert result.chunks[1]["metadata"]["page_label"] == "20"


def test_an_anchor_after_a_full_stop_stays_with_its_sentence_when_overlapping(tmp_path):
    # The last five words of the first paragraph begin with the anchor that
    # closes W3's sentence; the overlap starts after it.
    body = (f"{_sentences(1, 3)} [^1] Vier Wörter ohne Ende\n\n{_sentences(5, 8)}\n\n"
            "[^1]: Die Note.\n")
    result = _extract(_master(tmp_path, body=body, strategy="basic", pages=None),
                      chunk_size=27, overlap=7)
    assert result.chunks[1]["text"].startswith("Vier Wörter ohne Ende\n\nW5 ")


def test_an_anchor_after_a_full_stop_stays_with_its_sentence_when_cutting(tmp_path):
    body = f"{_sentences(1, 4)} [^1] {_sentences(5, 8)}\n\n[^1]: Die Note.\n"
    result = _extract(_master(tmp_path, body=body, strategy="basic", pages=None),
                      chunk_size=27, overlap=0)
    assert [c["text"][:3] for c in result.chunks] == ["W1 ", "W4 ", "W7 "]
    assert "W4 zwei drei vier fünf. [^1] W5" in result.chunks[1]["text"]


# the way in -------------------------------------------------------------------

def test_universal_extractor_hands_a_bundle_to_the_scriptor_extractor(tmp_path):
    result = UniversalExtractor().extract(_master(tmp_path))
    assert result.metadata.detected_format == "scriptor"
    assert _address(_chunk(result, "Einleitung")) == ("1", 7, "printed")


@pytest.mark.parametrize("text", [
    "# Notizen\n\nEin Absatz ohne Frontmatter.\n",
    "---\ntags: [lektuere]\n---\n\nEine Obsidian-Notiz mit Frontmatter.\n",
])
def test_markdown_without_a_format_version_stays_with_the_txt_extractor(tmp_path, text):
    note = tmp_path / "note.md"
    note.write_text(text, encoding="utf-8")
    result = UniversalExtractor().extract(note)
    assert result.metadata.detected_format == "txt"
