#!/usr/bin/env python3
"""ARCHILLES — prepare library volumes with Scriptor and put the bundle in place.

This is the script that makes bundles (Naht S6). For each selected book it runs
``scriptor.pipeline.run_all`` over the book's PDF, measures the four admission
conditions of ``src.archilles.scriptor_build``, and only then moves the bundle
into ``<library>/.archilles/scriptor/<key>/``, where indexing finds it. A volume
that fails a condition gets no bundle; the report names it with its reason.

It is deliberately **not** part of the watchdog: the watchdog takes a bundle
that lies there without comparing anything, so the comparison happens here, once,
when the bundle is made.

Indexing afterwards is handed the **book file**, never the master: the book file
is the book's identity — Calibre metadata, viewer annotations and links are
looked up with it, and ``Indexer._text_source`` switches only the text source.

Usage:
    # what would happen, without running Scriptor
    python scripts/scriptor_prepare.py --tag "prio" --dry-run

    # one volume (the golden-chain test)
    python scripts/scriptor_prepare.py --ids 10593

    # a first batch of born-digital monographs
    python scripts/scriptor_prepare.py --tag "prio" --limit 20

    # prepare now, index later (ids go to the library's index queue)
    python scripts/scriptor_prepare.py --all --limit 50 --no-index

    # rewrite REPORT.md from the recorded state, without preparing anything
    python scripts/scriptor_prepare.py --report-only

    # another library (Zotero, a folder): the source decides, not this script
    ARCHILLES_LIBRARY_PATH=D:\\Zotero python scripts/scriptor_prepare.py --all --limit 5
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.archilles.book_files import BUNDLE_FOLDER, bundle_dir, bundle_master, prepared_jsonl_name
from src.archilles.config import (
    get_excluded_tags,
    get_languages,
    get_library_path,
    get_mode,
    get_scriptor_config,
)
from src.archilles.engine import ArchillesRAG
from src.archilles.indexer.checkpoint import IndexingCheckpoint
from src.archilles.runtime_lock import routine_lock
from src.archilles.scriptor_build import check_bundle, open_decisions
from src.archilles.hashing import file_sha256

# The three states a volume can be in, as the user named them. They are derived,
# not stored: the master's hash against the hash that reached the index.
STATE_PREPARED = "vorbereitet"
STATE_EDITED = "bearbeitet"
STATE_RELEASED = "freigegeben zur Indexierung"

WORK_FOLDER = "_work"
REJECTED_FOLDER = "_rejected"
STATE_FILE = "state.json"
REPORT_FILE = "REPORT.md"
# Written by the user (or a measurement), never by this script: which prepared
# document in another source a bundled volume makes redundant.
LAB_DUPLICATES_FILE = "lab_duplicates.json"

LOCK_WAIT_S = 7200


# ── selection ────────────────────────────────────────────────────────────────

def _select_books(args, library_path: Path, adapter) -> list[dict[str, Any]]:
    """The books to prepare, via the adapter where there is one that is not Calibre."""
    from scripts.batch_index import (
        _adapter_list_books,
        get_all_books,
        get_books_by_author,
        get_books_by_ids,
        get_books_by_tag,
    )

    excludes = [] if args.include_excluded else get_excluded_tags(library_path)
    excludes += args.exclude_tags or []

    if adapter is not None and getattr(adapter, "adapter_type", "calibre") != "calibre":
        books = _adapter_list_books(
            adapter,
            tag_filter=args.tag,
            exclude_tags=excludes,
            author_filter=args.filter_authors or ([args.author] if args.author else None),
            collection_filter=args.collection,
            item_type_filter=args.item_type,
        )
        if args.ids:
            wanted = {i.strip() for i in args.ids.split(",") if i.strip()}
            books = [b for b in books if str(b["id"]) in wanted]
        return books

    if args.ids:
        return get_books_by_ids(library_path, [int(i) for i in args.ids.split(",") if i.strip()])
    if args.tag:
        return get_books_by_tag(library_path, args.tag, exclude_tags=excludes,
                                rating=args.rating, author_filter=args.filter_authors)
    if args.author:
        return get_books_by_author(library_path, args.author, exclude_tags=excludes,
                                   rating=args.rating)
    return get_all_books(library_path, author_filter=args.filter_authors, exclude_tags=excludes)


def _pdf_of(book: dict[str, Any]) -> str | None:
    """The book's PDF, or None. Scriptor reads PDFs; EPUB is not its subject."""
    for fmt in book.get("formats") or []:
        if str(fmt.get("format", "")).upper() == "PDF":
            return fmt["path"]
    return None


# ── the recorded state ───────────────────────────────────────────────────────

def _load_state(scriptor_dir: Path) -> dict[str, dict]:
    path = scriptor_dir / STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"  ⚠️  {path.name} unreadable — starting a fresh record")
        return {}


def _save_state(scriptor_dir: Path, state: dict[str, dict]) -> None:
    scriptor_dir.mkdir(parents=True, exist_ok=True)
    (scriptor_dir / STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def volume_state(entry: dict, master: Path | None) -> str:
    """Where a volume stands: prepared, edited by hand, or in the index as it is.

    The master's current hash decides. Equal to what was indexed, nothing is
    outstanding; equal to what Scriptor wrote but not indexed, it waits;
    anything else is handwork the index has not seen.
    """
    if master is None or not master.exists():
        return STATE_PREPARED
    current = file_sha256(master)
    if current and current == entry.get("indexed_hash"):
        return STATE_RELEASED
    if current == entry.get("built_hash"):
        return STATE_PREPARED
    return STATE_EDITED


# ── one volume ───────────────────────────────────────────────────────────────

def _clear_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _prune(*folders: Path) -> None:
    """Remove the scratch folders once they hold nothing."""
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            pass


def _move_bundle(work: Path, target: Path, keep_pages: bool) -> None:
    """Put a passed bundle where indexing looks for it.

    The old bundle is replaced only now, once the new one exists and passed --
    nothing is deleted before its replacement is there (the rule of the July
    review).
    """
    if not keep_pages:
        _clear_dir(work / "pages")
    _clear_dir(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(work), str(target))


def prepare_volume(
    book: dict[str, Any],
    pdf: Path,
    scriptor_dir: Path,
    *,
    chunking: str,
    keep_pages: bool,
) -> tuple[Path | None, Any, dict]:
    """Run Scriptor over one PDF and judge the result.

    Returns ``(master_or_None, check, timings)``. The master is None when the
    run failed or a condition refused it; the rejected bundle is kept under
    ``_rejected/<key>/`` so its audit and decisions can be read.
    """
    from src.archilles.scriptor_build import BundleCheck

    key = prepared_jsonl_name(str(book["id"]))[: -len(".jsonl")]
    work = scriptor_dir / WORK_FOLDER / key
    target = scriptor_dir / key
    rejected = scriptor_dir / REJECTED_FOLDER / key

    _clear_dir(work)
    work.mkdir(parents=True, exist_ok=True)
    master = work / "book.md"

    started = time.perf_counter()
    try:
        from scriptor import pipeline

        pipeline.run_all(pdf, master, pages_dir=work / "pages", chunking_strategy=chunking)
    except BaseException as exc:                 # SystemExit included: the CLI may raise it
        check = BundleCheck(error=f"{type(exc).__name__}: {exc}")
        print(f"         Scriptor failed: {check.error}")
        (work / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        _clear_dir(work / "pages")
        _clear_dir(rejected)
        rejected.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(rejected))
        _prune(scriptor_dir / WORK_FOLDER)
        return None, check, {"seconds": round(time.perf_counter() - started, 1)}

    run_seconds = round(time.perf_counter() - started, 1)
    check = check_bundle(master, pdf)
    decisions = open_decisions(master)
    timings = {"seconds": run_seconds, "decisions": decisions}

    if not check.admitted:
        print(f"         Not admitted: {'; '.join(check.reasons or [check.error or '?'])}")
        _clear_dir(work / "pages")
        _clear_dir(rejected)
        rejected.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(work), str(rejected))
        _prune(scriptor_dir / WORK_FOLDER)
        return None, check, timings

    _move_bundle(work, target, keep_pages)
    # A volume that passes this time leaves no refused copy of itself behind.
    _clear_dir(rejected)
    _prune(scriptor_dir / WORK_FOLDER, scriptor_dir / REJECTED_FOLDER)
    return target / master.name, check, timings


# ── the report ───────────────────────────────────────────────────────────────

def _fmt_share(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def write_report(scriptor_dir: Path, state: dict[str, dict], run: dict[str, Any]) -> Path:
    """``REPORT.md`` — what every volume measured, and where it stands.

    Written after every run, from the recorded state, so it describes the whole
    library and not only this batch.
    """
    lab = {}
    lab_file = scriptor_dir / LAB_DUPLICATES_FILE
    if lab_file.exists():
        try:
            lab = json.loads(lab_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lab = {}

    lines = [
        "# Scriptor-Bündel in dieser Bibliothek",
        "",
        f"*Stand: {datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"geschrieben von `scripts/scriptor_prepare.py`.*",
        "",
        "Ein Bündel ersetzt die Textquelle des Buchs; das Buch selbst bleibt seine",
        "Calibre- bzw. Zotero-Datei. Vier Zulassungsbedingungen entscheiden beim",
        "Erzeugen: Textabdeckung der Seiten, Noten gegen Scriptors Audit, Anteil",
        "bezeugter Seiten, und der Anteil Text, der unter der Nummer einer anderen",
        "Seite steht. Wer eine verfehlt, bekommt kein Bündel.",
        "",
        "## Dieser Lauf",
        "",
        f"- Bände betrachtet: {run.get('total', 0)}",
        f"- Bündel gebaut: {run.get('built', 0)}",
        f"- übersprungen (Bündel vorhanden): {run.get('skipped', 0)}",
        f"- ohne PDF (Scriptor liest keine EPUBs): {run.get('no_pdf', 0)}",
        f"- nicht zugelassen: {run.get('rejected', 0)}",
        f"- gescheitert: {run.get('failed', 0)}",
        f"- indexiert: {run.get('indexed', 0)}",
        "",
    ]

    built = [(bid, e) for bid, e in sorted(state.items()) if e.get("admitted")]
    if built:
        total_decisions = sum(e.get("decisions") or 0 for _bid, e in built)
        lines += [
            "## Bündel im Bestand",
            "",
            f"Offene Entscheidungen insgesamt: **{total_decisions}** "
            f"in {sum(1 for _b, e in built if e.get('decisions'))} von {len(built)} Bänden.",
            "",
            "| Band | Titel | Deckung | Noten | bezeugt | geerbt | Entsch. | Stand |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
        for book_id, entry in built:
            checks = entry.get("checks", {})
            master = Path(entry["master"]) if entry.get("master") else None
            notes = f"{checks.get('definitions')}/{checks.get('certain_notes')}"
            lines.append(
                f"| {book_id} | {entry.get('title', '')[:60]} "
                f"| {_fmt_share(checks.get('coverage'))} | {notes} "
                f"| {_fmt_share(checks.get('attested'))} | {_fmt_share(checks.get('inherited'))} "
                f"| {entry.get('decisions', 0)} | {volume_state(entry, master)} |"
            )
        lines.append("")

    refused = [(bid, e) for bid, e in sorted(state.items()) if not e.get("admitted")]
    if refused:
        lines += [
            "## Nicht zugelassen",
            "",
            "Diese Bände behalten ihren PDF-Pfad. Das verworfene Bündel liegt unter",
            f"`{REJECTED_FOLDER}/<key>/` — Audit und Entscheidungen sind dort lesbar.",
            "",
            "| Band | Titel | Grund |",
            "|---|---|---|",
        ]
        for book_id, entry in refused:
            checks = entry.get("checks", {})
            reason = "; ".join(checks.get("reasons") or []) or checks.get("error") or "?"
            lines.append(f"| {book_id} | {entry.get('title', '')[:60]} | {reason} |")
        lines.append("")

    hints = [(bid, lab[bid]) for bid, entry in built if bid in lab]
    if hints:
        lines += [
            "## Jetzt entbehrlich",
            "",
            "Diese Bände lagen als aufbereitetes Dokument in einer anderen Quelle.",
            "Der Band ist jetzt am Buch selbst indexiert; das Dokument dort kann über",
            "`exclude_patterns` aus dem Index genommen werden.",
            "",
        ]
        for book_id, paths in hints:
            for path in paths if isinstance(paths, list) else [paths]:
                lines.append(f"- [{book_id}] `{path}`")
        lines.append("")

    path = scriptor_dir / REPORT_FILE
    scriptor_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── the run ──────────────────────────────────────────────────────────────────

def _queue_file(archilles_dir: Path, adapter) -> Path:
    zotero = adapter is not None and getattr(adapter, "adapter_type", "") == "zotero"
    return archilles_dir / ("zotero_index_queue.json" if zotero else "index_queue.json")


def _queue(path: Path, book_ids: list[str]) -> None:
    existing: list = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    merged = sorted({str(b) for b in existing} | set(book_ids))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def run(args) -> int:
    library_path = get_library_path()
    archilles_dir = library_path / ".archilles"
    scriptor_dir = archilles_dir / BUNDLE_FOLDER

    adapter = None
    try:
        from src.adapters import create_adapter

        adapter = create_adapter(library_path)
    except Exception as exc:
        print(f"⚠️  No adapter for {library_path} ({exc}) — Calibre paths only")

    state = _load_state(scriptor_dir)

    if args.report_only:
        report = write_report(scriptor_dir, state, {"total": 0})
        print(f"📝 {report}")
        return 0

    chunking = args.chunking or get_scriptor_config(library_path)["chunking"]

    print(f"\n{'=' * 64}")
    print("  ARCHILLES — SCRIPTOR PREPARE")
    print(f"{'=' * 64}")
    print(f"  Library:  {library_path} ({getattr(adapter, 'adapter_type', 'calibre')})")
    print(f"  Bundles:  {scriptor_dir}")
    print(f"  Chunking: {chunking}")
    print(f"  Mode:     {'DRY RUN' if args.dry_run else 'PREPARING'}"
          f"{'' if args.index else ' (no indexing)'}")
    print(f"{'=' * 64}\n")

    books = _select_books(args, library_path, adapter)
    if args.limit:
        books = books[: args.limit]
    if not books:
        print("No books selected.")
        return 0

    stats: dict[str, Any] = {
        "total": len(books), "built": 0, "skipped": 0, "no_pdf": 0,
        "rejected": 0, "failed": 0, "indexed": 0,
    }
    queued: list[str] = []

    checkpoint_file = archilles_dir / "scriptor_prepare_checkpoint.json"
    checkpoint = None
    if not args.dry_run:
        checkpoint = IndexingCheckpoint.load(checkpoint_file)
        done = set(checkpoint.completed_books) if checkpoint else set()
        if done:
            print(f"  (Resuming: {len(done)} volumes already done)\n")
            books = [b for b in books if str(b["id"]) not in done]
        if checkpoint is None:
            checkpoint = IndexingCheckpoint.create_new(
                checkpoint_file, profile="scriptor",
                book_ids=[str(b["id"]) for b in books], phase="prepare",
            )

    rag = None
    for i, book in enumerate(books, 1):
        book_id = str(book["id"])
        print(f"\n[{i}/{len(books)}] {book.get('author', '')}: {book.get('title', '')}")

        pdf = _pdf_of(book)
        if not pdf:
            formats = ", ".join(f.get("format", "?") for f in book.get("formats") or [])
            print(f"         No PDF ({formats or 'no formats'}) — Scriptor reads PDFs. Skipping.")
            stats["no_pdf"] += 1
            continue

        existing = bundle_master(archilles_dir, book_id)
        if existing and not args.force:
            entry = state.get(book_id, {})
            print(f"         Bundle exists ({volume_state(entry, existing)}). "
                  f"Skipping — use --force to rebuild.")
            stats["skipped"] += 1
            continue

        if args.dry_run:
            print(f"         Would prepare: {pdf}")
            print(f"         Bundle would go to: {bundle_dir(archilles_dir, book_id)}")
            continue

        master, check, timings = prepare_volume(
            book, Path(pdf), scriptor_dir,
            chunking=chunking, keep_pages=args.keep_pages,
        )

        entry = {
            "title": book.get("title", ""),
            "author": book.get("author", ""),
            "source_pdf": str(pdf),
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "seconds": timings.get("seconds"),
            "decisions": timings.get("decisions", 0),
            "admitted": master is not None,
            "checks": check.as_dict(),
            "master": str(master) if master else None,
            "built_hash": file_sha256(master) if master else None,
            "indexed_hash": None,
            "indexed_at": None,
        }

        if master is None:
            stats["failed" if check.error else "rejected"] += 1
            state[book_id] = entry
            _save_state(scriptor_dir, state)
            if checkpoint:
                checkpoint.fail_book(book_id, "; ".join(check.reasons or [check.error or "?"]))
            continue

        stats["built"] += 1
        checks = check.as_dict()
        print(f"         Bundle OK: coverage {_fmt_share(checks['coverage'])}, "
              f"attested {_fmt_share(checks['attested'])}, "
              f"inherited {_fmt_share(checks['inherited'])}, "
              f"{checks['definitions']} notes, {entry['decisions']} open decisions "
              f"({timings.get('seconds')}s)")

        if args.index:
            if rag is None:
                rag = _load_rag(library_path, adapter)
            try:
                # The book file, never the master: identity stays with the book,
                # only the text comes from the bundle (Naht S4).
                result = rag.index_book(pdf, book_id, force=True)
                stats["indexed"] += 1
                entry["indexed_hash"] = entry["built_hash"]
                entry["indexed_at"] = datetime.now().isoformat(timespec="seconds")
                print(f"         Indexed: {result.get('chunks_indexed', '?')} chunks")
            except Exception as exc:
                print(f"         Indexing FAILED: {exc}")
                stats["failed"] += 1
        else:
            queued.append(book_id)

        state[book_id] = entry
        _save_state(scriptor_dir, state)
        if checkpoint:
            checkpoint.complete_book(book_id)

    if queued:
        path = _queue_file(archilles_dir, adapter)
        _queue(path, queued)
        print(f"\n📋 {len(queued)} volumes written to {path.name} for later indexing")

    if checkpoint and not args.dry_run:
        checkpoint.delete()

    print(f"\n{'=' * 64}")
    print(f"  Built: {stats['built']}  Skipped: {stats['skipped']}  "
          f"No PDF: {stats['no_pdf']}  Not admitted: {stats['rejected']}  "
          f"Failed: {stats['failed']}  Indexed: {stats['indexed']}")
    print(f"{'=' * 64}")

    if not args.dry_run:
        report = write_report(scriptor_dir, state, stats)
        print(f"\n📝 Report: {report}")
    return 0


def _load_rag(library_path: Path, adapter) -> ArchillesRAG:
    """The indexing engine, wired like batch_index's."""
    from src.archilles.config import get_rag_db_path
    from src.archilles.hardware import detect_hardware
    from src.archilles.recipe import default_recipe
    from scripts.batch_index import resolve_indexing_plan

    resolution = resolve_indexing_plan(
        mode_cli=None,
        mode_config=get_mode(library_path),
        profile_override=None,
        hierarchical_flag=False,
        prepare_only_flag=False,
        hw=detect_hardware(),
        recipe=default_recipe(),
    )
    plan = resolution.execution_plan
    print(f"         (engine: {plan.embedding_device}, batch {plan.batch_size}, "
          f"hierarchical {resolution.hierarchical})")
    return ArchillesRAG(
        db_path=get_rag_db_path(library_path),
        languages=get_languages(library_path),
        execution_plan=plan,
        hierarchical=resolution.hierarchical,
        adapter=adapter,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare library volumes with Scriptor and place the bundle.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="every book in the library")
    group.add_argument("--tag", help="books carrying this tag")
    group.add_argument("--author", help="books by this author (partial match)")
    group.add_argument("--ids", metavar="ID[,ID,...]", help="these book ids")
    group.add_argument("--collection", metavar="NAME", help="this collection (Zotero)")

    parser.add_argument("--type", dest="item_type", metavar="TYPE",
                        help="item type filter (Zotero)")
    parser.add_argument("--rating", type=int, choices=[0, 1, 2, 3, 4, 5], default=None,
                        help="exactly this rating")
    parser.add_argument("--filter-author", action="append", dest="filter_authors",
                        metavar="AUTHOR", help="restrict to these authors (repeatable)")
    parser.add_argument("--exclude-tag", action="append", dest="exclude_tags", metavar="TAG",
                        help="skip books with this tag (repeatable)")
    parser.add_argument("--include-excluded", action="store_true",
                        help="ignore the configured excluded_tags")
    parser.add_argument("--limit", type=int, help="at most this many volumes")

    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be prepared, run nothing")
    parser.add_argument("--force", action="store_true",
                        help="rebuild a bundle that already exists")
    parser.add_argument("--no-index", dest="index", action="store_false",
                        help="prepare only; write the ids to the library's index queue")
    parser.add_argument("--chunking", choices=["basic", "scientific"],
                        help="override the library's scriptor.chunking setting")
    parser.add_argument("--keep-pages", action="store_true",
                        help="keep Scriptor's page models beside the master (~50 KB/page)")
    parser.add_argument("--report-only", action="store_true",
                        help="rewrite REPORT.md from the recorded state and exit")
    parser.add_argument("--no-lock", action="store_true",
                        help="run without the routine lock (for a foreground one-off)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not any([args.all, args.tag, args.author, args.ids, args.collection, args.report_only]):
        build_parser().error(
            "one of --all --tag --author --ids --collection --report-only is required")

    if args.dry_run or args.report_only or args.no_lock:
        return run(args)

    # Scriptor is CPU-only, but the re-index embeds on the GPU -- the VRAM
    # budget is what the routine lock protects.
    with routine_lock("scriptor-prepare", wait_s=LOCK_WAIT_S) as acquired:
        if not acquired:
            print("⏳ Another routine holds the lock — try again later.")
            return 1
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
