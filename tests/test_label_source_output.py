"""The way out reads label_source: what a citation rests on reaches the reader.

A page label from a Scriptor bundle carries its witness (PREPARED_FORMAT_SPEC
§6.3): ``printed`` and ``link`` corroborate it, ``toc`` and ``catalogue`` assert
it from a named source, ``computed`` only follows from the numbering, and a
value this code does not know counts as asserted. A citation resting on an
inferred label is usable but weaker, so the reader is told.

``_resolve_page_info`` used to read ``printed_page``/``printed_page_confidence``,
which nothing ever wrote; that branch is gone.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.archilles.engine.core import ArchillesRAG

REPO_ROOT = Path(__file__).resolve().parent.parent
INFERRED = "page label inferred, not printed — verify against the volume"


@pytest.mark.parametrize("label_source, warning", [
    ("printed", None),
    ("link", None),
    ("toc", None),
    ("catalogue", None),
    ("computed", INFERRED),
    ("ocr-verified", INFERRED),     # unknown: asserted, per spec §6.3
    ("", None),                     # every row not read from a bundle
    (None, None),
])
def test_a_printed_label_is_cited_with_what_it_rests_on(label_source, warning):
    meta = {"page_label": "xiv", "page_number": 17, "label_source": label_source}
    assert ArchillesRAG._resolve_page_info(meta) == ("xiv", False, warning)


def test_without_a_label_the_physical_page_is_cited_as_such():
    assert ArchillesRAG._resolve_page_info({"page_number": 17}) == (17, True, None)


def test_without_any_page_there_is_nothing_to_cite():
    assert ArchillesRAG._resolve_page_info({}) == (None, False, None)


def test_nothing_reads_the_printed_page_keys_any_more():
    """Quelltext-Ratsche: the keys had a consumer and never a producer. A
    reader coming back would be a docstring again."""
    key = re.compile(r"""['"]printed_page(?:_confidence)?['"]""")
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{n}"
        for path in list((REPO_ROOT / "src").rglob("*.py")) + list((REPO_ROOT / "scripts").rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if key.search(line)
    ]
    assert hits == []


# the MCP clients ----------------------------------------------------------------

ROW = {"rank": 1, "score": 0.9, "rerank_score": 0.9, "text": "Ein Satz.",
       "metadata": {"author": "Bauer", "book_title": "Aneignung", "year": 2023,
                    "page_label": "88", "page_number": 104, "label_source": "printed"}}


def _unified(tmp_path, rows):
    from src.calibre_mcp.unified_server import UnifiedMCPServer
    service = SimpleNamespace(search=lambda **kw: [dict(r) for r in rows],
                              build_claude_prompt=lambda **kw: {"system": "", "user": ""})
    inner = SimpleNamespace(_ensure_rag_initialized=lambda: True, service=service,
                            _archilles_dir=None, citation_config=None)
    return UnifiedMCPServer(servers={"lib": inner}, default_source="lib", master_dir=tmp_path)


def _single(rows):
    from src.calibre_mcp.server import CalibreMCPServer
    server = object.__new__(CalibreMCPServer)
    server._ensure_rag_initialized = lambda: True
    server.service = SimpleNamespace(search_with_citations=lambda **kw: {
        "results": rows, "num_results": len(rows), "system_prompt": "", "user_prompt": ""})
    return server


@pytest.mark.parametrize("meta, page, label_source", [
    ({"page_label": "88", "page_number": 104, "label_source": "printed"}, "88", "printed"),
    ({"page_label": "", "page_number": 104, "label_source": ""}, 104, None),
    ({}, None, None),
])
def test_the_unified_server_gives_clients_the_page_and_its_witness(tmp_path, meta, page, label_source):
    response = _unified(tmp_path, [ROW | {"metadata": meta}]).search_books_with_citations_tool(query="q")
    got = response["raw_results"][0]["metadata"]
    assert (got["page"], got["label_source"]) == (page, label_source)


def test_the_single_source_server_does_the_same():
    response = _single([ROW]).search_books_with_citations_tool(query="q")
    got = response["raw_results"][0]["metadata"]
    assert (got["page"], got["label_source"]) == ("88", "printed")


# the Markdown export --------------------------------------------------------------

def test_the_calibre_link_opens_the_physical_page(tmp_path):
    """Search rows carry page_number; the link read 'page', which no row has."""
    rag = ArchillesRAG(db_path=str(tmp_path / "db"), skip_model=True)
    results = [{"rank": 1, "similarity": 0.8, "text": "Ein Satz.",
                "metadata": {"book_title": "Aneignung", "calibre_id": 10593,
                             "page_label": "88", "page_number": 104}}]
    out = rag.export_to_markdown(results, "Satz", str(tmp_path / "export.md"))
    assert "calibre://view/10593#page=104" in Path(out).read_text(encoding="utf-8")


# the prompt Claude gets -----------------------------------------------------------

def _prompt_builder():
    from src.archilles.engine.prompting import PromptBuilder
    rag = SimpleNamespace(
        _resolve_page_info=ArchillesRAG._resolve_page_info,
        _format_section_meta=lambda meta: '',
    )
    return PromptBuilder(rag)


@pytest.mark.parametrize("label_source, expected", [
    ("printed", "Page: 88"),
    ("toc", "Page: 88"),
    ("computed", "Page: 88 (inferred)"),
    ("ocr-verified", "Page: 88 (inferred)"),
    ("", "Page: 88"),
])
def test_an_inferred_label_reaches_the_answering_model_as_such(label_source, expected):
    meta = {"page_label": "88", "page_number": 104, "label_source": label_source}
    builder = _prompt_builder()

    assert builder._page_meta_part(meta) == expected
    assert expected in builder._build_inline_metadata(meta, "doc_1")


def test_the_physical_page_is_never_marked_inferred():
    assert _prompt_builder()._page_meta_part({"page_number": 104}) == "Page: 104"


def test_a_document_block_carries_the_mark_too():
    rows = [{"rank": 1, "similarity": 0.8, "text": "Ein Satz.",
             "metadata": {"page_label": "88", "label_source": "computed"}}]
    xml = _prompt_builder().format_results_as_xml(rows, "Satz")
    assert "Page: 88 (inferred)" in xml


def test_the_system_prompt_tells_claude_what_the_mark_means():
    from src.archilles.engine.prompting import PromptBuilder
    prompt = PromptBuilder.get_system_prompt()
    assert "(inferred)" in prompt
    assert "verify" in prompt
