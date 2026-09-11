"""The ``chapter`` field of an EPUB chunk (Gliederung B1).

Order of evidence: the printed ``<h1>`` of the file, then the title the table
of contents gives the file, then the file name. Before B1 the TOC title was
skipped, so two thirds of EPUB text carried a file name as its chapter.
"""

import pytest

epub = pytest.importorskip("ebooklib.epub")

from src.extractors.epub_extractor import EPUBExtractor


def _document(file_name: str, body: str) -> "epub.EpubHtml":
    item = epub.EpubHtml(title=file_name, file_name=file_name, lang="de")
    item.content = f"<html><body>{body}</body></html>"
    return item


@pytest.fixture(scope="module")
def chapter_by_file(tmp_path_factory):
    book = epub.EpubBook()
    book.set_identifier("b1-fixture")
    book.set_title("Fixture")
    book.set_language("de")
    book.add_author("Anonymus")

    documents = [
        _document("listed.xhtml", "<p>Absatz listed ohne Überschrift.</p>"),
        _document(
            "printed.xhtml",
            "<h1>Gedruckte Überschrift</h1><p>Absatz printed.</p>",
        ),
        _document("bare.xhtml", "<p>Absatz bare ohne Überschrift und Eintrag.</p>"),
    ]
    for document in documents:
        book.add_item(document)
    book.toc = [
        epub.Link("listed.xhtml", "Titel aus dem Verzeichnis", "listed"),
        epub.Link("printed.xhtml", "Abweichender Verzeichnistitel", "printed"),
    ]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = documents

    path = tmp_path_factory.mktemp("b1") / "fixture.epub"
    epub.write_epub(str(path), book)

    chunks = EPUBExtractor().extract(path).chunks
    return {
        name: chunk["metadata"]["chapter"]
        for chunk in chunks
        for name in ("listed", "printed", "bare")
        if f"Absatz {name}" in chunk["text"]
    }


def test_toc_title_names_a_file_without_h1(chapter_by_file):
    assert chapter_by_file["listed"] == "Titel aus dem Verzeichnis"


def test_printed_h1_outranks_the_toc_title(chapter_by_file):
    assert chapter_by_file["printed"] == "Gedruckte Überschrift"


def test_file_name_remains_the_last_resort(chapter_by_file):
    assert chapter_by_file["bare"] == "bare.xhtml"
