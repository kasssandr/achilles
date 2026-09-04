"""Annotations must come from the adapter for non-Calibre sources (review 1.4).

The indexer resolved *metadata* through the adapter but *annotations* through a
hard-coded ``get_combined_annotations``, which reads Calibre-viewer sidecars and
PDF-embedded annotations only. Zotero's reader writes highlights into
``zotero.sqlite``; ``ZoteroAdapter.get_annotations`` reads them and was never
called from indexing. Meanwhile the scanner's change proxy fires on
``attachment_modified_at``, triggers a re-index, adds nothing, counts a
``delta_update`` and exits 0 — a wiring gap reporting success.

The gate on the fix is the Calibre side. ``CalibreAdapter.get_annotations``
calls ``get_combined_annotations`` with *different* arguments (no
``include_pdf`` / ``exclude_toc_markers`` / ``min_length``) and drops the
``source`` field when it maps into ``DocumentAnnotation``. Routing Calibre
through the adapter would therefore change its annotation hashes and re-index
the whole library. So the adapter route is for non-Calibre sources only, and
these tests pin that.
"""

from types import SimpleNamespace

import pytest

from src.adapters.base import DocumentAnnotation


class _Recorder:
    """Stands in for get_combined_annotations, recording how it was called."""

    def __init__(self, annotations=None):
        self.calls = []
        self._annotations = annotations if annotations is not None else [
            {"highlighted_text": "legacy highlight", "notes": "legacy note",
             "type": "highlight", "page": 12, "source": "calibre_viewer"},
        ]

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"annotations": list(self._annotations)}


class _Adapter:
    def __init__(self, adapter_type, annotations=None, raises=None):
        self.adapter_type = adapter_type
        self._annotations = annotations or []
        self._raises = raises
        self.calls = []

    def get_annotations(self, doc_id):
        self.calls.append(doc_id)
        if self._raises:
            raise self._raises
        return list(self._annotations)


def _indexer(adapter):
    """An Indexer bound to a stub RAG that carries only the adapter."""
    from src.archilles.engine.indexing import Indexer

    return Indexer(SimpleNamespace(_adapter=adapter))


@pytest.fixture
def legacy(monkeypatch):
    """Replace the hard-coded legacy call and hand back the recorder."""
    from src.archilles.engine import indexing

    rec = _Recorder()
    monkeypatch.setattr(indexing, "get_combined_annotations", rec)
    return rec


class TestNonCalibreGoesThroughTheAdapter:
    def test_zotero_annotations_are_read_from_the_adapter(self, legacy):
        adapter = _Adapter("zotero", [
            DocumentAnnotation(text="a highlight", note="my note",
                               annotation_type="highlight", page=7),
            DocumentAnnotation(text="", note="standalone note",
                               annotation_type="note"),
        ])
        result = _indexer(adapter)._resolve_annotations("ZKEY0001", "/nonexistent.pdf")

        assert adapter.calls == ["ZKEY0001"]
        assert legacy.calls == [], "the Calibre-only reader must not run here"
        assert len(result) == 2

    def test_fields_are_mapped_onto_the_legacy_shape(self, legacy):
        """_build_annotation_chunks reads highlighted_text/notes/type/page/source."""
        adapter = _Adapter("zotero", [
            DocumentAnnotation(text="a highlight", note="my note",
                               annotation_type="underline", page=7,
                               created="2026-09-01"),
        ])
        first = _indexer(adapter)._resolve_annotations("ZKEY0001", "/x.pdf")[0]

        assert first["highlighted_text"] == "a highlight"
        assert first["notes"] == "my note"
        assert first["type"] == "underline"
        assert first["page"] == 7
        assert first["source"] == "zotero"
        assert first["timestamp"] == "2026-09-01"

    def test_mapped_annotations_build_real_chunks(self, legacy):
        """End of the chain: the mapping must survive into chunk text."""
        adapter = _Adapter("zotero", [
            DocumentAnnotation(text="Sein und Zeit", note="cf. §7",
                               annotation_type="highlight", page=3),
        ])
        idx = _indexer(adapter)
        annots = idx._resolve_annotations("ZKEY0001", "/x.pdf")
        chunks, texts = idx._build_annotation_chunks(
            annots, book_id="ZKEY0001", book_title="T", annotation_hash="h",
            book_format="pdf", metadata_hash="m", book_metadata=None,
        )

        assert len(chunks) == 1
        assert texts[0] == "[ANNOTATION] Sein und Zeit | Note: cf. §7"
        assert chunks[0]["annotation_source"] == "zotero"
        assert chunks[0]["annotation_type"] == "highlight"
        assert chunks[0]["page_number"] == 3

    def test_a_failing_adapter_does_not_abort_indexing(self, legacy):
        adapter = _Adapter("zotero", raises=RuntimeError("database is locked"))
        result = _indexer(adapter)._resolve_annotations("ZKEY0001", "/x.pdf")

        assert result == []
        assert legacy.calls == [], "a locked Zotero DB must not silently fall back"

    def test_folder_sources_use_the_adapter_too(self, legacy):
        adapter = _Adapter("folder", [DocumentAnnotation(text="note")])
        result = _indexer(adapter)._resolve_annotations("notes/a.md", "/x.md")

        assert len(result) == 1
        assert legacy.calls == []


class TestLegacyReaderServesTheAdapterlessPath:
    """The direct reader survives for setups constructed without an adapter.
    Its arguments are the reindex-storm gate: they must match what
    CalibreAdapter.get_annotations passes, or the two routes disagree about
    which annotations exist."""

    def test_no_adapter_uses_the_legacy_reader(self, legacy):
        result = _indexer(None)._resolve_annotations("42", "/book.epub")

        assert len(legacy.calls) == 1
        assert result[0]["source"] == "calibre_viewer"

    def test_legacy_reader_keeps_its_exact_arguments(self, legacy):
        """include_pdf / exclude_toc_markers / min_length decide which
        annotations exist, and therefore the annotation hash."""
        _indexer(None)._resolve_annotations("42", "/book.epub")

        _args, kwargs = legacy.calls[0]
        assert kwargs["book_path"] == "/book.epub"
        assert kwargs["include_pdf"] is True
        assert kwargs["exclude_toc_markers"] is True
        assert kwargs["min_length"] == 20

    def test_legacy_reader_failure_is_swallowed_as_before(self, monkeypatch):
        from src.archilles.engine import indexing

        def boom(**kwargs):
            raise OSError("sidecar unreadable")

        monkeypatch.setattr(indexing, "get_combined_annotations", boom)
        assert _indexer(None)._resolve_annotations("42", "/book.epub") == []


# ── the annotation payload must be usable, not merely present ────────

class TestZoteroAnnotationSemantics:
    """Routing highlights through the adapter is only half the job: with the
    path dead, nobody noticed that the type stayed a raw Zotero integer and the
    page was always 0. A citation system cannot use either."""

    def test_numeric_type_is_mapped_to_a_name(self):
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_annotation_type,
        )

        assert zotero_annotation_type(1) == "highlight"
        assert zotero_annotation_type(2) == "note"

    def test_string_type_still_maps(self):
        """Zotero has used string types too; both forms must resolve."""
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_annotation_type,
        )

        assert zotero_annotation_type("highlight") == "highlight"
        assert zotero_annotation_type("underline") == "highlight"

    def test_unknown_type_falls_back_to_highlight(self):
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_annotation_type,
        )

        assert zotero_annotation_type(99) == "highlight"
        assert zotero_annotation_type(None) == "highlight"
        assert zotero_annotation_type("") == "highlight"

    def test_printed_page_label_wins_over_the_physical_index(self):
        """pageLabel is the page a reader would cite; sortIndex is the physical
        page in the file, and on real data the two differ (label 159 at
        sortIndex 00000)."""
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_page_number,
        )

        assert zotero_page_number("00000|000633|00355", "159") == 159

    def test_falls_back_to_the_sort_index_when_no_label(self):
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_page_number,
        )

        assert zotero_page_number("00030|000545|00302", "") == 31  # 0-based + 1

    def test_non_numeric_label_does_not_crash(self):
        """Roman numerals and the like are real page labels."""
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_page_number,
        )

        assert zotero_page_number("00007|000001|00001", "xii") == 8

    def test_no_page_information_yields_none(self):
        from src.archilles.annotation_providers.zotero_provider import (
            zotero_page_number,
        )

        assert zotero_page_number("", "") is None


def _build_zotero_db(path, *, with_page_label=True):
    """A Zotero database shaped like the real one.

    ``pageLabel`` and ``sortIndex`` are deliberately optional: the existing
    fixture in tests/test_zotero_adapter.py has neither column, and real
    databases from older Zotero versions may not either — the adapter must read
    both shapes rather than raising "no such column".
    """
    import sqlite3

    db = path / "zotero.sqlite"
    conn = sqlite3.connect(db)
    extra = ", sortIndex TEXT, pageLabel TEXT" if with_page_label else ""
    conn.executescript(f"""
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INTEGER,
                            key TEXT, dateAdded TEXT, dateModified TEXT);
        CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY,
                                      parentItemID INTEGER, linkMode INTEGER,
                                      path TEXT, contentType TEXT);
        CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY,
                                parentItemID INTEGER, note TEXT, title TEXT);
        CREATE TABLE itemAnnotations (itemID INTEGER PRIMARY KEY,
                                      parentItemID INTEGER, type INTEGER,
                                      text TEXT, comment TEXT{extra});
        CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
    """)
    conn.execute("INSERT INTO items VALUES (1, 2, 'ZKEY0001', '', '')")
    conn.execute("INSERT INTO items VALUES (2, 3, 'ATT00001', '', '')")
    conn.execute("INSERT INTO itemAttachments VALUES (2, 1, 2, 'x.pdf', 'application/pdf')")
    conn.execute("INSERT INTO items VALUES (3, 1, 'ANN00001', '', '')")
    if with_page_label:
        conn.execute(
            "INSERT INTO itemAnnotations VALUES (3, 2, 1, ?, ?, ?, ?)",
            ("Marcion of Sinope", "a note", "00000|000633|00355", "159"),
        )
    else:
        conn.execute(
            "INSERT INTO itemAnnotations VALUES (3, 2, 1, ?, ?)",
            ("Marcion of Sinope", "a note"),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def zotero_lib(tmp_path):
    return _build_zotero_db(tmp_path)


class TestZoteroAdapterCarriesTypeAndPage:
    """Measured against the real library, PU8IFBUA's highlights arrived as
    type=1 / page=0. Both are pinned here."""

    def test_adapter_returns_named_type_and_printed_page(self, zotero_lib):
        from src.adapters.zotero_adapter import ZoteroAdapter

        annots = ZoteroAdapter(zotero_lib).get_annotations("ZKEY0001")

        assert annots, "the fixture item carries one annotation"
        assert annots[0].annotation_type == "highlight"
        assert annots[0].page == 159

    def test_indexer_chunk_carries_them_through(self, zotero_lib):
        from src.adapters.zotero_adapter import ZoteroAdapter

        adapter = ZoteroAdapter(zotero_lib)
        idx = _indexer(adapter)
        annots = idx._resolve_annotations("ZKEY0001", "/x.pdf")
        chunks, _texts = idx._build_annotation_chunks(
            annots, book_id="ZKEY0001", book_title="T", annotation_hash="h",
            book_format="pdf", metadata_hash="m", book_metadata=None,
        )

        assert chunks[0]["annotation_type"] == "highlight"
        assert chunks[0]["page_number"] == 159
        assert chunks[0]["annotation_source"] == "zotero"

    def test_older_schema_without_page_columns_still_works(self, tmp_path):
        """The existing test fixture — and older Zotero databases — have no
        sortIndex/pageLabel. Selecting them blindly would raise."""
        from src.adapters.zotero_adapter import ZoteroAdapter

        lib = _build_zotero_db(tmp_path, with_page_label=False)
        annots = ZoteroAdapter(lib).get_annotations("ZKEY0001")

        assert len(annots) == 1
        assert annots[0].annotation_type == "highlight"
        assert annots[0].page is None
        assert annots[0].text == "Marcion of Sinope"


# ── one route for every source (user decision 2026-09-04) ────────────

class TestCalibreGoesThroughTheAdapterToo:
    """The Calibre exception is gone: instead of routing around the adapter,
    the adapter was made equivalent. `source` is now part of DocumentAnnotation
    and CalibreAdapter passes the indexer's filter arguments, so the adapter
    route yields byte-identical annotations — no reindex, no special case.

    ARCHILLES is meant to be used by other people; a permanent "except for
    Calibre" in the abstraction is the shape that produced findings 1.4 and
    1.15 in the first place.
    """

    LEGACY_ROWS = [
        {"highlighted_text": "a highlight", "notes": "a note",
         "type": "highlight", "page": 12, "source": "calibre_viewer",
         "timestamp": "2026-01-02T03:04:05"},
        {"highlighted_text": "from the pdf", "notes": "",
         "type": "underline", "page": 3, "source": "pdf"},
        {"highlighted_text": "", "notes": "note without highlight",
         "type": "note", "source": "calibre_viewer"},
    ]

    def test_round_trip_through_the_adapter_loses_nothing(self, monkeypatch, tmp_path):
        """The equivalence the decision rests on, asserted rather than assumed:
        every field the indexer reads survives dict -> DocumentAnnotation ->
        dict."""
        from src.adapters import calibre_adapter as ca

        monkeypatch.setattr(
            ca, "_calibre_get_combined_annotations",
            lambda **kwargs: {"annotations": list(self.LEGACY_ROWS)},
            raising=False,
        )
        adapter = ca.CalibreAdapter.__new__(ca.CalibreAdapter)
        monkeypatch.setattr(type(adapter), "get_file_path",
                            lambda self, doc_id: tmp_path / "b.epub")

        doc_annots = adapter.get_annotations("42")
        idx = _indexer(_Adapter("calibre", doc_annots))
        # Route it the same way the indexer does for any adapter.
        mapped = idx._map_adapter_annotations(doc_annots, "calibre")

        for original, produced in zip(self.LEGACY_ROWS, mapped):
            for field in ("highlighted_text", "notes", "type", "source"):
                assert produced[field] == original.get(field, ""), field
            assert produced["page"] == original.get("page")

    def test_adapter_uses_the_indexers_filter_arguments(self, monkeypatch, tmp_path):
        """min_length and exclude_toc_markers decide which annotations exist.
        Different arguments here would mean different annotation hashes — the
        reindex the equivalence is meant to avoid."""
        from src.adapters import calibre_adapter as ca

        seen = {}

        def spy(**kwargs):
            seen.update(kwargs)
            return {"annotations": []}

        monkeypatch.setattr(ca, "_calibre_get_combined_annotations", spy,
                            raising=False)
        adapter = ca.CalibreAdapter.__new__(ca.CalibreAdapter)
        monkeypatch.setattr(type(adapter), "get_file_path",
                            lambda self, doc_id: tmp_path / "b.epub")

        adapter.get_annotations("42")

        assert seen["include_pdf"] is True
        assert seen["exclude_toc_markers"] is True
        assert seen["min_length"] == 20

    def test_indexer_no_longer_special_cases_calibre(self, legacy):
        """A Calibre adapter is used like any other."""
        adapter = _Adapter("calibre", [
            DocumentAnnotation(text="from the adapter", note="",
                               annotation_type="highlight", page=5,
                               source="calibre_viewer"),
        ])
        result = _indexer(adapter)._resolve_annotations("42", "/book.epub")

        assert adapter.calls == ["42"], "the adapter must be asked, like every source"
        assert legacy.calls == [], "no second route around it"
        assert result[0]["source"] == "calibre_viewer"
        assert result[0]["page"] == 5

    def test_adapterless_path_is_the_only_fallback(self, legacy):
        """Legacy setups without an adapter still work — that path is
        unchanged and is now the *only* direct use of the old reader."""
        result = _indexer(None)._resolve_annotations("42", "/book.epub")

        assert len(legacy.calls) == 1
        assert result[0]["source"] == "calibre_viewer"

    def test_adapter_source_wins_over_the_adapter_type(self, legacy):
        """"calibre_viewer" and "pdf" are finer than "calibre" and must not be
        flattened; the adapter type only fills in when a source is absent."""
        adapter = _Adapter("zotero", [
            DocumentAnnotation(text="x", source=""),
            DocumentAnnotation(text="y", source="explicit"),
        ])
        result = _indexer(adapter)._resolve_annotations("K", "/x.pdf")

        assert result[0]["source"] == "zotero"
        assert result[1]["source"] == "explicit"
