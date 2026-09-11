"""Making a bundle: the four conditions, and the script that applies them.

A bundle replaces a book's text source, so a bad Scriptor run would silently
replace a whole volume with a worse one. Four conditions decide when the bundle
is made -- the watchdog takes an existing bundle without asking (Naht S4/S6).
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from scriptor.document import SIDECAR_VERSION

from src.archilles.scriptor_build import (
    MAX_INHERITED,
    MIN_ATTESTED,
    MIN_COVERAGE,
    audit_certain_notes,
    check_bundle,
    definition_count,
    open_decisions,
    text_coverage,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import scriptor_prepare as sp  # noqa: E402

# Enough words that every page clears the 40-word floor of the measure.
PAGE_A = ("Die Aneignung von Bildern ist eine urheberrechtliche Frage, die sich "
          "seit der Appropriation Art immer wieder neu stellt und bis zu den Memes "
          "der Gegenwart reicht, wo sie eine ganz eigene Gestalt annimmt, weil das "
          "Zitat dort zur Münze des Gesprächs geworden ist und niemand mehr fragt, "
          "wem das Bild gehörte, bevor es geteilt wurde.")
PAGE_B = ("Der zweite Abschnitt handelt von der Schranke des Zitatrechts, von ihrer "
          "Geschichte und von den Gründen, aus denen sie in der digitalen Öffentlichkeit "
          "anders wirkt als im gedruckten Buch, wo jeder Nachdruck ein Verlagsvertrag "
          "war und die Schranke deshalb selten gebraucht wurde, während sie heute in "
          "jedem geteilten Bild mitschwingt und kaum je ausdrücklich benannt wird.")


def _pdf(path: Path, pages: list[str]) -> Path:
    """A small real PDF with a running head, a page number and body text."""
    import pymupdf

    doc = pymupdf.open()
    for number, body in enumerate(pages, 1):
        page = doc.new_page()
        page.insert_textbox((40, 30, 550, 50), "DIE ANEIGNUNG VON BILDERN", fontsize=9)
        page.insert_textbox((40, 60, 550, 700), body, fontsize=11)
        page.insert_textbox((40, 720, 550, 740), str(86 + number), fontsize=9)
    doc.save(path)
    doc.close()
    return path


def _master(text: str, *, version: str = "0.3.0") -> str:
    return f"---\nformat_version: {version}\nchunking_strategy: scientific\n---\n\n{text}\n"


def _bundle(folder: Path, body: str, *, attested=0.9, inherited=0.01,
            certain=0, definitions="", sidecar=True, decisions=0) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    master = folder / "book.md"
    master.write_text(_master(body + definitions), encoding="utf-8")
    (folder / "book.md.audit.txt").write_text(
        f"# Footnote confidence audit\n# 2 pages, {certain} certain footnotes, 0 uncertain, "
        "0 with several candidates.\n", encoding="utf-8")
    if sidecar:
        profile = {"edge": "bottom", "attested": attested, "description": "bottom"}
        if inherited is not None:
            profile["inherited"] = inherited
        (folder / "book.md.pagination.json").write_text(
            json.dumps({"version": SIDECAR_VERSION, "profile": profile, "pages": []}),
            encoding="utf-8")
    if decisions:
        (folder / "book.md.decisions.txt").write_text(
            "# header\n" + "\n".join(f"p.{i} unklar" for i in range(decisions)), encoding="utf-8")
    return master


# ── the measures ─────────────────────────────────────────────────────────────

def test_a_master_holding_both_pages_covers_them(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    share, lost = text_coverage(pdf, _master(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))
    assert (share, lost) == (1.0, [])


def test_a_page_missing_from_the_master_is_named(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    share, lost = text_coverage(pdf, _master(f"[p. 87] {PAGE_A}"))
    assert (share, lost) == (0.5, [2])


def test_the_running_head_and_the_page_number_do_not_count_as_loss(tmp_path):
    """Scriptor removes both on purpose. Counting windows instead of pages put
    flawless volumes at 92-96 %."""
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    share, _lost = text_coverage(pdf, _master(f"{PAGE_A}\n\n{PAGE_B}"))
    assert share == 1.0


def test_a_footnote_anchor_does_not_break_the_page_it_sits_on(tmp_path):
    """In the PDF the superscript sits against the word ('Frage,1 die'); in the
    master it is '[^1]'. Reduced to its number, the text matches again."""
    body = PAGE_A.replace("Frage,", "Frage,1")
    pdf = _pdf(tmp_path / "book.pdf", [body])
    share, _lost = text_coverage(
        pdf, _master(PAGE_A.replace("Frage,", "Frage,[^1]") + "\n\n[^1]: Dazu unten."))
    assert share == 1.0


def test_the_audit_header_gives_the_certain_footnotes():
    assert audit_certain_notes("# 5 pages, 212 certain footnotes, 3 uncertain, 0 with") == 212
    assert audit_certain_notes("# nothing of the sort") is None


def test_definitions_are_counted_at_the_line_start():
    text = "Ein Satz mit [^1] Anker.\n\n[^1]: Die Note.\n[^2]: Und noch eine.\n"
    assert definition_count(text) == 2


def test_open_decisions_skip_the_header(tmp_path):
    master = tmp_path / "book.md"
    master.write_text("x", encoding="utf-8")
    (tmp_path / "book.md.decisions.txt").write_text(
        "# Kommentar\n\np.12 unklar\np.13 unklar\n", encoding="utf-8")
    assert open_decisions(master) == 2


def test_a_volume_without_a_decisions_sidecar_has_none(tmp_path):
    master = tmp_path / "book.md"
    master.write_text("x", encoding="utf-8")
    assert open_decisions(master) == 0


# ── the four conditions together ─────────────────────────────────────────────

def test_a_sound_bundle_is_admitted(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    master = _bundle(tmp_path / "b", f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}")
    check = check_bundle(master, pdf)
    assert check.admitted, check.reasons
    assert check.coverage == 1.0 and check.attested == 0.9 and check.inherited == 0.01


def test_lost_text_refuses_the_bundle(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    check = check_bundle(_bundle(tmp_path / "b", f"[p. 87] {PAGE_A}"), pdf)
    assert not check.admitted
    assert any("coverage" in r for r in check.reasons)
    assert check.lost_pages == [2]


def test_fewer_definitions_than_certain_footnotes_refuses_the_bundle(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    master = _bundle(tmp_path / "b", f"{PAGE_A}\n\n{PAGE_B}", certain=3,
                     definitions="\n\n[^1]: Eine Note.")
    check = check_bundle(master, pdf)
    assert not check.admitted
    assert any("1 definitions < 3 certain" in r for r in check.reasons)


def test_a_bundle_without_citation_addresses_does_not_replace_the_pdf(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    master = _bundle(tmp_path / "b", f"{PAGE_A}\n\n{PAGE_B}", attested=MIN_ATTESTED - 0.01)
    check = check_bundle(master, pdf)
    assert not check.admitted
    assert any("attested" in r for r in check.reasons)


def test_text_standing_under_another_pages_number_refuses_the_bundle(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    master = _bundle(tmp_path / "b", f"{PAGE_A}\n\n{PAGE_B}", inherited=MAX_INHERITED)
    check = check_bundle(master, pdf)
    assert not check.admitted
    assert any("cites as another page" in r for r in check.reasons)


def test_a_sidecar_without_the_inherited_share_is_unknown_and_unknown_is_refused(tmp_path):
    """Only the producer can count it; a bundle from an older Scriptor cannot
    be judged, and what cannot be judged does not replace the PDF."""
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    master = _bundle(tmp_path / "b", f"{PAGE_A}\n\n{PAGE_B}", inherited=None)
    check = check_bundle(master, pdf)
    assert not check.admitted
    assert any("inherited" in r for r in check.reasons)


def test_an_unreadable_pdf_is_a_refusal_not_a_crash(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    check = check_bundle(_bundle(tmp_path / "b", PAGE_A), broken)
    assert not check.admitted and check.error


# ── where a volume stands ────────────────────────────────────────────────────

def test_a_fresh_bundle_is_prepared(tmp_path):
    from src.archilles.hashing import file_sha256

    master = _bundle(tmp_path / "b", PAGE_A)
    entry = {"built_hash": file_sha256(master), "indexed_hash": None}
    assert sp.volume_state(entry, master) == sp.STATE_PREPARED


def test_a_bundle_whose_text_reached_the_index_is_released(tmp_path):
    from src.archilles.hashing import file_sha256

    master = _bundle(tmp_path / "b", PAGE_A)
    digest = file_sha256(master)
    assert sp.volume_state({"built_hash": digest, "indexed_hash": digest}, master) \
        == sp.STATE_RELEASED


def test_handwork_the_index_has_not_seen_shows_as_edited(tmp_path):
    from src.archilles.hashing import file_sha256

    master = _bundle(tmp_path / "b", PAGE_A)
    digest = file_sha256(master)
    master.write_text(master.read_text(encoding="utf-8") + "\n\nEin Zusatz.\n", encoding="utf-8")
    assert sp.volume_state({"built_hash": digest, "indexed_hash": digest}, master) \
        == sp.STATE_EDITED


# ── one volume through the script ────────────────────────────────────────────

def _fake_run_all(body: str, *, attested=0.9, inherited=0.01, decisions=0):
    """Stand in for scriptor.pipeline.run_all: write the bundle it would write."""
    def run_all(pdf, out_path, pages_dir=None, chunking_strategy="basic"):
        out = Path(out_path)
        _bundle(out.parent, body, attested=attested, inherited=inherited, decisions=decisions)
        Path(pages_dir).mkdir(parents=True, exist_ok=True)
        (Path(pages_dir) / "0001.json").write_text("{}", encoding="utf-8")
    return run_all


def _install(monkeypatch, run_all):
    import scriptor.pipeline

    monkeypatch.setattr(scriptor.pipeline, "run_all", run_all)


def test_a_passing_run_lands_where_indexing_looks(tmp_path, monkeypatch):
    from src.archilles.book_files import bundle_master

    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    scriptor_dir = tmp_path / ".archilles" / "scriptor"
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}", decisions=3))

    master, check, timings = sp.prepare_volume(
        {"id": "10593"}, pdf, scriptor_dir, chunking="scientific", keep_pages=False)

    assert check.admitted and master is not None
    assert master == bundle_master(tmp_path / ".archilles", "10593")
    assert timings["decisions"] == 3
    assert not (scriptor_dir / sp.WORK_FOLDER / "10593").exists()
    assert not (master.parent / "pages").exists()          # page models are not kept


def test_a_refused_run_leaves_no_bundle_and_keeps_its_evidence(tmp_path, monkeypatch):
    from src.archilles.book_files import bundle_master

    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    scriptor_dir = tmp_path / ".archilles" / "scriptor"
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}", attested=0.9))

    master, check, _t = sp.prepare_volume(
        {"id": "10593"}, pdf, scriptor_dir, chunking="scientific", keep_pages=False)

    assert master is None and not check.admitted
    assert bundle_master(tmp_path / ".archilles", "10593") is None
    assert (scriptor_dir / sp.REJECTED_FOLDER / "10593" / "book.md.audit.txt").exists()


def test_a_failed_scriptor_run_does_not_touch_the_bundle_in_place(tmp_path, monkeypatch):
    """Nothing is deleted before its replacement is there; the book keeps the
    text it has."""
    from src.archilles.book_files import bundle_master

    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    scriptor_dir = tmp_path / ".archilles" / "scriptor"
    good = _bundle(scriptor_dir / "10593", f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}")

    def boom(*a, **kw):
        raise RuntimeError("Scriptor exploded")

    _install(monkeypatch, boom)
    master, check, _t = sp.prepare_volume(
        {"id": "10593"}, pdf, scriptor_dir, chunking="scientific", keep_pages=False)

    assert master is None and "Scriptor exploded" in check.error
    assert bundle_master(tmp_path / ".archilles", "10593") == good
    assert (scriptor_dir / sp.REJECTED_FOLDER / "10593" / "traceback.txt").exists()


def test_force_replaces_the_bundle_that_is_there(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    scriptor_dir = tmp_path / ".archilles" / "scriptor"
    _bundle(scriptor_dir / "10593", "Ein alter Text.")
    (scriptor_dir / "10593" / "stale.txt").write_text("alt", encoding="utf-8")
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))

    master, check, _t = sp.prepare_volume(
        {"id": "10593"}, pdf, scriptor_dir, chunking="scientific", keep_pages=False)

    assert check.admitted
    assert PAGE_A[:30] in master.read_text(encoding="utf-8")
    assert not (master.parent / "stale.txt").exists()      # the old bundle is gone, not merged


def test_keep_pages_keeps_the_page_models(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    scriptor_dir = tmp_path / ".archilles" / "scriptor"
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))

    master, _c, _t = sp.prepare_volume(
        {"id": "10593"}, pdf, scriptor_dir, chunking="scientific", keep_pages=True)
    assert (master.parent / "pages" / "0001.json").exists()


# ── the report ───────────────────────────────────────────────────────────────

def test_the_report_names_every_volume_with_its_four_values(tmp_path):
    state = {
        "10593": {"title": "Die Aneignung von Bildern", "admitted": True, "decisions": 3,
                  "master": str(tmp_path / "nope.md"), "built_hash": "a", "indexed_hash": "a",
                  "checks": {"coverage": 1.0, "definitions": 12, "certain_notes": 12,
                             "attested": 0.91, "inherited": 0.004}},
        "8081": {"title": "Josephus", "admitted": False, "decisions": 0,
                 "checks": {"reasons": ["text coverage 84.1% < 98% (132 pages lost)"]}},
    }
    report = sp.write_report(tmp_path, state, {"total": 2, "built": 1, "rejected": 1})
    text = report.read_text(encoding="utf-8")

    assert "Die Aneignung von Bildern" in text
    assert "100%" in text and "12/12" in text and "91%" in text
    assert "Josephus" in text and "132 pages lost" in text
    assert "Offene Entscheidungen insgesamt: **3**" in text


def test_the_report_says_which_document_elsewhere_is_now_redundant(tmp_path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / sp.LAB_DUPLICATES_FILE).write_text(
        json.dumps({"7246": ["D:/Archilles-Lab/zuckerman_scriptor.md"]}), encoding="utf-8")
    state = {"7246": {"title": "A Jewish Princedom", "admitted": True, "checks": {}}}

    text = sp.write_report(tmp_path, state, {"total": 1}).read_text(encoding="utf-8")
    assert "zuckerman_scriptor.md" in text


# ── selection ────────────────────────────────────────────────────────────────

def test_only_pdfs_are_offered_to_scriptor():
    assert sp._pdf_of({"formats": [{"format": "EPUB", "path": "a.epub"},
                                   {"format": "PDF", "path": "b.pdf"}]}) == "b.pdf"
    assert sp._pdf_of({"formats": [{"format": "EPUB", "path": "a.epub"}]}) is None
    assert sp._pdf_of({"formats": []}) is None


def test_a_zotero_item_is_selected_through_its_adapter(tmp_path, monkeypatch):
    pdf = tmp_path / "Aufsatz.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    doc = SimpleNamespace(doc_id="ABCD1234", title="Aufsatz", authors=["Bauer"],
                          file_path=pdf, file_format="pdf", tags=[])
    adapter = SimpleNamespace(adapter_type="zotero", library_path=tmp_path,
                              list_documents=lambda **kw: [doc])
    args = SimpleNamespace(tag=None, author=None, ids=None, collection=None, item_type=None,
                           rating=None, filter_authors=None, exclude_tags=None,
                           include_excluded=True)

    books = sp._select_books(args, tmp_path, adapter)
    assert [b["id"] for b in books] == ["ABCD1234"]
    assert sp._pdf_of(books[0]) == str(pdf)


def test_the_queue_the_ids_go_to_belongs_to_the_source(tmp_path):
    calibre = SimpleNamespace(adapter_type="calibre")
    zotero = SimpleNamespace(adapter_type="zotero")
    assert sp._queue_file(tmp_path, calibre).name == "index_queue.json"
    assert sp._queue_file(tmp_path, zotero).name == "zotero_index_queue.json"


def test_queued_ids_are_merged_not_replaced(tmp_path):
    path = tmp_path / "index_queue.json"
    path.write_text(json.dumps(["10593"]), encoding="utf-8")
    sp._queue(path, ["8081", "10593"])
    assert json.loads(path.read_text(encoding="utf-8")) == ["10593", "8081"]


def test_the_library_chunking_setting_is_read(tmp_path):
    from src.archilles.config import get_scriptor_config

    assert get_scriptor_config(tmp_path)["chunking"] == "scientific"
    (tmp_path / ".archilles").mkdir()
    (tmp_path / ".archilles" / "config.json").write_text(
        json.dumps({"scriptor": {"chunking": "basic"}}), encoding="utf-8")
    assert get_scriptor_config(tmp_path)["chunking"] == "basic"


def test_a_scriptor_block_is_a_known_config_key():
    """Otherwise the config reader warns about it on every run."""
    from src.archilles.config import _KNOWN_LIBRARY_CONFIG_KEYS

    assert "scriptor" in _KNOWN_LIBRARY_CONFIG_KEYS


@pytest.mark.parametrize("threshold", [MIN_COVERAGE, MIN_ATTESTED, MAX_INHERITED])
def test_the_thresholds_are_the_ones_the_user_released(threshold):
    assert threshold in (0.98, 0.20, 0.05)


# ── the run as a whole ───────────────────────────────────────────────────────

def _args(**over):
    base = dict(all=True, tag=None, author=None, ids=None, collection=None, item_type=None,
                rating=None, filter_authors=None, exclude_tags=None, include_excluded=True,
                limit=None, dry_run=False, force=False, index=True, chunking="scientific",
                keep_pages=False, report_only=False, no_lock=True)
    base.update(over)
    return SimpleNamespace(**base)


def _wire(tmp_path, monkeypatch, books, indexed):
    monkeypatch.setattr(sp, "get_library_path", lambda: tmp_path)
    monkeypatch.setattr(sp, "_select_books", lambda args, lib, adapter: books)
    monkeypatch.setattr(
        sp, "_load_rag",
        lambda lib, adapter: SimpleNamespace(
            index_book=lambda path, book_id, force: indexed.append((path, book_id, force))
            or {"chunks_indexed": 7}))


def _book(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    return {"id": "10593", "title": "Die Aneignung von Bildern", "author": "Bauer",
            "formats": [{"format": "PDF", "path": str(pdf)}]}, pdf


def test_indexing_is_handed_the_book_file_never_the_master(tmp_path, monkeypatch):
    """The book file is the book's identity -- Calibre metadata, viewer
    annotations and links are looked up with it (Naht S4)."""
    book, pdf = _book(tmp_path)
    indexed = []
    _wire(tmp_path, monkeypatch, [book], indexed)
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))

    assert sp.run(_args()) == 0
    assert indexed == [(str(pdf), "10593", True)]


def test_a_second_run_skips_the_volume_it_already_bundled(tmp_path, monkeypatch, capsys):
    book, _pdf_path = _book(tmp_path)
    indexed = []
    _wire(tmp_path, monkeypatch, [book], indexed)
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))

    sp.run(_args())
    capsys.readouterr()
    sp.run(_args())

    assert len(indexed) == 1
    assert "Bundle exists" in capsys.readouterr().out


def test_force_prepares_it_again(tmp_path, monkeypatch):
    book, _pdf_path = _book(tmp_path)
    indexed = []
    _wire(tmp_path, monkeypatch, [book], indexed)
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))

    sp.run(_args())
    sp.run(_args(force=True))
    assert len(indexed) == 2


def test_without_indexing_the_id_goes_to_the_queue(tmp_path, monkeypatch):
    book, _pdf_path = _book(tmp_path)
    indexed = []
    _wire(tmp_path, monkeypatch, [book], indexed)
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))

    assert sp.run(_args(index=False)) == 0
    assert indexed == []
    queue = json.loads((tmp_path / ".archilles" / "index_queue.json").read_text(encoding="utf-8"))
    assert queue == ["10593"]


def test_a_dry_run_runs_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    book, _pdf_path = _book(tmp_path)
    indexed = []
    _wire(tmp_path, monkeypatch, [book], indexed)

    def never(*a, **kw):
        raise AssertionError("Scriptor must not run in a dry run")

    _install(monkeypatch, never)
    assert sp.run(_args(dry_run=True)) == 0
    assert "Would prepare" in capsys.readouterr().out
    assert not (tmp_path / ".archilles" / "scriptor").exists()


def test_a_book_without_a_pdf_is_named_and_left_alone(tmp_path, monkeypatch, capsys):
    epub = {"id": "9999", "title": "Nur als EPUB", "author": "X",
            "formats": [{"format": "EPUB", "path": str(tmp_path / "x.epub")}]}
    indexed = []
    _wire(tmp_path, monkeypatch, [epub], indexed)
    assert sp.run(_args()) == 0
    assert "No PDF" in capsys.readouterr().out
    assert indexed == []


def test_the_run_writes_the_report(tmp_path, monkeypatch):
    book, _pdf_path = _book(tmp_path)
    _wire(tmp_path, monkeypatch, [book], [])
    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}", decisions=2))

    sp.run(_args())
    report = (tmp_path / ".archilles" / "scriptor" / sp.REPORT_FILE).read_text(encoding="utf-8")
    assert "Die Aneignung von Bildern" in report
    assert sp.STATE_RELEASED in report


# ── what the coverage question does not apply to ─────────────────────────────

PAGE_C = ("Der dritte Abschnitt fragt nach dem Freiraum, den ein Zitatrecht lassen "
          "muss, damit die Kunst der Aneignung überhaupt möglich bleibt, und nach der "
          "Frage, wer diesen Freiraum eigentlich verteidigt, wenn die Verwerter ihn "
          "nicht brauchen und die Urheber ihn fürchten, obwohl sie selbst von ihm "
          "leben, sooft sie ein fremdes Bild in die eigene Arbeit holen.")

TOC_PAGE = ("Inhaltsverzeichnis Einleitung 19 Aneignung als Rechtsbegriff 24 "
            "Aneignung als kultureller Begriff 27 Begriffsbestimmung für die Zwecke "
            "dieser Arbeit 32 Begriff des Bildlichen 42 Fazit 58 Zweites Kapitel 61 "
            "Vervielfältigungsrecht 142 Zwischenfazit 178 Strategien 222 Register 340")

CONTENTS_HEAD = ("[region: front-matter]\n\n[region: contents]\n\n## Contents\n\n"
                 "- [Einleitung](#p-19) — p. 19\n- [Fazit](#p-58) — p. 58\n\n"
                 "[region: main]\n\n")


def _paged_bundle(folder: Path, body: str, positions: list[int]) -> Path:
    """A bundle whose sidecar knows ``positions`` as labelled pages."""
    folder.mkdir(parents=True, exist_ok=True)
    master = folder / "book.md"
    master.write_text(_master(body), encoding="utf-8")
    (folder / "book.md.audit.txt").write_text(
        "# 4 pages, 0 certain footnotes, 0 uncertain, 0 with several candidates.\n",
        encoding="utf-8")
    (folder / "book.md.pagination.json").write_text(json.dumps({
        "version": SIDECAR_VERSION,
        "profile": {"edge": "bottom", "attested": 0.9, "inherited": 0.0,
                    "description": "bottom"},
        "pages": [{"pos": p, "label": str(p), "source": "printed", "confidence": 1.0}
                  for p in positions],
    }), encoding="utf-8")
    return master


def test_a_rebuilt_table_of_contents_is_not_counted_as_lost_text(tmp_path):
    """Scriptor replaces the printed contents with a link list on purpose, so
    that page leaves no marker and its text is nowhere in the master."""
    from src.archilles.scriptor_build import front_matter_pages

    pdf = _pdf(tmp_path / "book.pdf", [TOC_PAGE, PAGE_A, PAGE_B, PAGE_C])
    master = _paged_bundle(
        tmp_path / "b",
        CONTENTS_HEAD + f"[p. 2] {PAGE_A}\n\n[p. 3] {PAGE_B}\n\n[p. 4] {PAGE_C}",
        [1, 2, 3, 4])

    assert front_matter_pages(master) == {1}
    check = check_bundle(master, pdf)
    assert check.admitted, check.reasons
    assert check.coverage == 1.0


def test_a_body_page_that_left_no_marker_is_still_lost(tmp_path):
    """The same shape as a rebuilt contents -- no marker, no text -- but the
    stretch it falls in opens no front-matter region."""
    from src.archilles.scriptor_build import front_matter_pages

    pdf = _pdf(tmp_path / "book.pdf", [TOC_PAGE, PAGE_A, PAGE_B, PAGE_C])
    master = _paged_bundle(
        tmp_path / "b",
        CONTENTS_HEAD + f"[p. 2] {PAGE_A}\n\n[p. 4] {PAGE_C}",
        [1, 2, 3, 4])

    assert front_matter_pages(master) == {1}
    check = check_bundle(master, pdf)
    assert not check.admitted
    assert check.lost_pages == [3]


def test_a_footnote_renumbered_document_wide_does_not_break_its_page(tmp_path):
    """The printed superscript is page-local (182), the master's anchor is
    document-wide ([^185]); digits are dropped on both sides so the sentence
    still matches."""
    printed = PAGE_A.replace("Frage,", "Frage,182")
    pdf = _pdf(tmp_path / "book.pdf", [printed])
    share, lost = text_coverage(
        pdf, _master(PAGE_A.replace("Frage,", "Frage, [^185]") + "\n\n[^185]: Dazu unten."))
    assert (share, lost) == (1.0, [])


def test_a_volume_with_nothing_left_to_check_is_unknown_not_empty(tmp_path):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    share, _lost = text_coverage(pdf, _master(PAGE_A), skip_pages={1, 2})
    assert share is None

    master = _bundle(tmp_path / "b", PAGE_A)
    check = check_bundle(master, pdf)
    check.coverage = None
    assert "no page could be checked" not in "".join(check.reasons)   # measured above


def test_a_volume_that_passes_leaves_no_refused_copy_behind(tmp_path, monkeypatch):
    pdf = _pdf(tmp_path / "book.pdf", [PAGE_A, PAGE_B])
    scriptor_dir = tmp_path / ".archilles" / "scriptor"

    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}"))            # refused
    sp.prepare_volume({"id": "10593"}, pdf, scriptor_dir,
                      chunking="scientific", keep_pages=False)
    assert (scriptor_dir / sp.REJECTED_FOLDER / "10593").exists()

    _install(monkeypatch, _fake_run_all(f"[p. 87] {PAGE_A}\n\n[p. 88] {PAGE_B}"))
    master, check, _t = sp.prepare_volume({"id": "10593"}, pdf, scriptor_dir,
                                          chunking="scientific", keep_pages=False)
    assert check.admitted and master is not None
    assert not (scriptor_dir / sp.REJECTED_FOLDER).exists()
    assert not (scriptor_dir / sp.WORK_FOLDER).exists()


def test_the_report_gives_the_spread_of_attested_pages(tmp_path):
    state = {
        str(i): {"title": f"Band {i}", "admitted": True, "decisions": 0,
                 "checks": {"coverage": 1.0, "attested": a, "inherited": 0.0,
                            "definitions": 0, "certain_notes": 0}}
        for i, a in enumerate([0.31, 0.62, 0.99])
    }
    text = sp.write_report(tmp_path, state, {"total": 3}).read_text(encoding="utf-8")
    assert "Median 62%, von 31% bis 99%" in text
