#!/usr/bin/env python3
"""
ARCHILLES Routine Runner — generic wrapper for scheduled watchdog/batch_index
runs across all configured sources.

Per-source tool selection
-------------------------
Calibre adapter        → ``scripts/watchdog.py --json`` (real watchdog with
                         hash-based metadata/annotation diff)
Other adapters         → ``scripts/batch_index.py --all --skip-existing``
                         (finds new documents only — no hash diff yet)

Marker logic
------------
A marker file ``<library>/.archilles/last_routine_run.txt`` is written on
successful completion.  On the next invocation the runner skips when:

* ``--frequency daily``  and the marker is dated today, or
* ``--frequency weekly`` and the marker falls in the same ISO calendar week.

Use ``--force`` to ignore the marker and ``--dry-run`` to print the planned
command without executing.

Usage
-----
    python scripts/run_routine.py --source archilles-lab --frequency daily
    python scripts/run_routine.py --source archilles-zotero --frequency weekly
    python scripts/run_routine.py --source archilles --frequency daily --force
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.archilles import runtime_lock
from src.archilles.config import load_master_config


def _should_skip(marker: Path, frequency: str, now: datetime) -> tuple[bool, str | None]:
    if not marker.exists():
        return False, None
    try:
        last_iso = marker.read_text(encoding="utf-8").strip()
        last = datetime.fromisoformat(last_iso)
    except Exception:
        return False, None
    if frequency == "daily" and last.date() == now.date():
        return True, last_iso
    if frequency == "weekly" and last.isocalendar()[:2] == now.isocalendar()[:2]:
        return True, last_iso
    return False, None


def _append_history(history_file: Path, record: dict) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _build_command(
    adapter: str,
    phase: str = "A",
    max_new: int | None = None,
    priority_tags: list[str] | None = None,
    rating: int | None = None,
    priority_collections: list[str] | None = None,
) -> list[str]:
    if adapter in ("calibre", "zotero"):
        # watchdog.py auto-detects library type via zotero.sqlite vs metadata.db
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "watchdog.py"), "--json"]
        # Priority tags (group 0 in _index_priority_key) — books carrying one of
        # these are indexed before everything else, regardless of rating/recency.
        for tag in priority_tags or []:
            cmd += ["--first-tag", tag]
        # Zotero collections as a priority axis (Calibre has none; the flag is
        # accepted but only the Zotero scanner honours it).
        for coll in priority_collections or []:
            cmd += ["--first-collection", coll]
        if adapter == "calibre":
            if phase == "A":
                # Phase A: fast daily scan — create metadata stubs for new books,
                # apply delta updates (metadata/annotations) for existing books.
                cmd += ["--index-metadata-only"]
            else:
                # Phase B: drain the fulltext backlog (phase1-stub → full content).
                # With --max-new the run is capped so it re-scans and re-sorts the
                # pending pool every day: freshly added (and 5★/4★) titles bubble
                # to the front instead of waiting behind a multi-day marathon scan.
                cmd += ["--index-fulltext-pending"]
                if max_new is not None:
                    cmd += ["--max-new", str(max_new)]
                if rating is not None:
                    cmd += ["--rating", str(rating)]
        elif adapter == "zotero":
            # Zotero has no A/B stub phase — a new item is either fully indexed
            # or "new". Index new items immediately; --max-new bounds a run so a
            # large first backlog (e.g. 500+ items) drains over several runs,
            # newest-first, instead of one marathon.
            cmd += ["--index-new"]
            if max_new is not None:
                cmd += ["--max-new", str(max_new)]
        return cmd
    # No --profile: it bypasses resolve_indexing_plan's mode/config
    # resolution entirely, pinning these libraries to flat/minimal forever
    # regardless of the configured mode. Let mode/config decide instead —
    # weak hardware still resolves to light automatically via plan().
    # --cleanup-orphans: path-keyed sources (Obsidian/Folder) turn every
    # delete or rename into a stale index entry; without a hash diff the
    # routine at least keeps deletions from accumulating. (Calibre/Zotero
    # get the same via the watchdog's built-in orphan cleanup.)
    return [
        sys.executable,
        str(REPO_ROOT / "scripts" / "batch_index.py"),
        "--all",
        "--skip-existing",
        "--cleanup-orphans",
        "--non-interactive",
    ]


def _parse_stats(stdout: str, adapter: str) -> dict:
    """Best-effort extraction of counters from tool stdout."""
    if adapter in ("calibre", "zotero"):
        # watchdog --json prints progress lines BEFORE the final JSON dump;
        # extract the last top-level JSON object by scanning from the end.
        s = stdout.rstrip()
        if not s.endswith("}"):
            return {}
        depth = 0
        start = -1
        for i in range(len(s) - 1, -1, -1):
            ch = s[i]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
        if start < 0:
            return {}
        try:
            data = json.loads(s[start:])
            return {
                "scanned": data.get("scanned"),
                "new_books": len(data.get("new_books", [])),
                "fulltext_pending": len(data.get("fulltext_pending", [])),
                "metadata_changed": len(data.get("metadata_changed", [])),
                "annotations_changed": len(data.get("annotations_changed", [])),
                "delta_updates": data.get("delta_updates", 0),
                "new_indexed": data.get("new_indexed", 0),
                "fulltext_indexed": data.get("fulltext_indexed", 0),
                "orphans_removed": data.get("orphans_removed", 0),
                "skipped_no_file": len(data.get("skipped_no_file", [])),
                "errors": len(data.get("errors", [])),
            }
        except Exception:
            return {}
    stats: dict = {}
    for key, pat in (
        ("indexed",  r"Successfully indexed:\s+(\d+)"),
        ("failed",   r"Failed:\s+(\d+)"),
        ("skipped",  r"Skipped:\s+(\d+)"),
        ("orphans_removed", r"Cleanup complete: (\d+)"),
    ):
        m = re.search(pat, stdout)
        if m:
            stats[key] = int(m.group(1))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ARCHILLES routine runner (scheduler-friendly wrapper).",
    )
    parser.add_argument("--source", required=True,
                        help="Source name from master config (e.g. 'archilles-lab')")
    parser.add_argument("--frequency", required=True, choices=["daily", "weekly"])
    parser.add_argument("--force", action="store_true",
                        help="Ignore marker and run unconditionally")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan and exit (no subprocess, no marker update)")
    parser.add_argument("--wait-for-lock", type=int, default=0, metavar="SECONDS",
                        help="Poll for the global lock up to SECONDS before giving up "
                             "(default: 0 = skip immediately if locked). "
                             "Recommended value for OnLogon tasks: 7200")
    parser.add_argument("--phase", choices=["A", "B"], default=None,
                        help="Calibre indexing phase: A = metadata stubs for new books "
                             "(fast, daily), B = fulltext for stub-only books (slow, "
                             "runs until done). Default: A (safer default — Phase B "
                             "must be requested explicitly). Ignored for non-Calibre adapters.")
    parser.add_argument("--max-new", type=int, default=None, metavar="N",
                        help="Phase B only: cap the fulltext backlog to N books per run. "
                             "Keeps daily runs short and forces a fresh scan+sort each day "
                             "so newly added / highly rated titles index first. "
                             "Recommended for slow machines, e.g. --max-new 12.")
    parser.add_argument("--rating", type=int, choices=[0, 1, 2, 3, 4, 5], default=None,
                        metavar="STARS",
                        help="Phase B only: restrict the fulltext backlog to books with "
                             "exactly this star rating (0 = unrated, 1-5 = N stars), newest "
                             "first. E.g. --phase B --rating 3 --max-new 50.")
    args = parser.parse_args()
    phase_explicit = args.phase is not None
    phase = args.phase or "A"

    master = load_master_config()
    if master is None:
        print("No master config found. Expected at ~/.archilles/config.json.",
              file=sys.stderr)
        return 2

    src = next((s for s in master.sources if s.name == args.source), None)
    if src is None:
        names = [s.name for s in master.sources]
        print(f"Unknown source '{args.source}'. Available: {names}", file=sys.stderr)
        return 2

    if phase_explicit and (src.adapter or "calibre") != "calibre":
        print(
            f"WARNING: --phase {phase} is ignored for adapter '{src.adapter}' "
            f"(only Calibre has phases A/B).",
            file=sys.stderr,
        )

    library_path = Path(src.library_path)
    archilles_dir = library_path / ".archilles"
    archilles_dir.mkdir(parents=True, exist_ok=True)
    # Phase B gets its own marker so A and B run independently.
    phase_suffix = f"_phase{phase}" if (src.adapter or "calibre") == "calibre" else ""
    marker = archilles_dir / f"last_routine_run{phase_suffix}.txt"
    log_file = archilles_dir / "routine.log"
    history_file = archilles_dir / "routine_history.jsonl"

    now = datetime.now().astimezone()
    skip, last_iso = (False, None) if args.force else _should_skip(marker, args.frequency, now)
    if skip:
        msg = (f"[{now.isoformat()}] {args.source}: SKIP "
               f"(last run {last_iso}, frequency={args.frequency})")
        print(msg)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        return 0

    adapter = src.adapter or "calibre"
    cmd = _build_command(adapter, phase=phase, max_new=args.max_new,
                         priority_tags=src.priority_tags, rating=args.rating,
                         priority_collections=src.priority_collections)
    env = os.environ.copy()
    env["ARCHILLES_LIBRARY_PATH"] = str(library_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    if args.dry_run:
        print(f"DRY-RUN — would run: {' '.join(cmd)}\n"
              f"  ARCHILLES_LIBRARY_PATH={library_path}")
        return 0

    if not runtime_lock.acquire(f"run_routine({args.source})", wait_s=args.wait_for_lock):
        # Log it like the frequency SKIP above. This abort used to leave no
        # trace at all: acquire() reports it on stderr, which lives only in
        # the console window, so a routine that waited out its timeout looked
        # exactly like one that never started. Observed 2026-09-04, when a
        # reboot left a stale lock and two routines dropped their day
        # silently.
        msg = (f"[{datetime.now().astimezone().isoformat()}] {args.source}: SKIP "
               f"(routine lock still held after {args.wait_for_lock}s wait)")
        print(msg)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        return 1
    heartbeat_stop = threading.Event()
    runtime_lock.start_heartbeat(heartbeat_stop)
    try:
        start = datetime.now().astimezone()
        print(f"[{start.isoformat()}] {args.source}: {' '.join(cmd)}")

        log_handle = log_file.open("a", encoding="utf-8")
        log_handle.write(f"\n=== {start.isoformat()} {args.source} START ===\n")
        log_handle.write(f"cmd: {' '.join(cmd)}\n")
        log_handle.flush()

        stdout_buf: list[str] = []
        log_lock = threading.Lock()

        # Terminal-Transform: Die einzige Aufgabe ist, den abschliessenden
        # JSON-Blob von ``watchdog --json`` vom Bildschirm zu nehmen (er dient
        # nur _parse_stats() und wird aus stdout_buf gelesen). Alle uebrigen
        # Zeilen aus rag.index_book (File:, Extract:, Embed:, Index:, der
        # Buch-Header [N/M] Autor: Titel …) gehen unveraendert durch — so sieht
        # der Watchdog im Terminal genauso aus wie ein direkter batch_index-Lauf.
        #
        # stderr wird NICHT umgeleitet, sondern erbt das echte Terminal des
        # Runners (siehe Popen unten). Dadurch rendert die tqdm-"Embedding"-
        # Leiste als eine sich in-place aktualisierende Zeile statt als hunderte
        # gestapelter Zeilen (Text-Pipes uebersetzen tqdms \r sonst zu \n).
        # Trade-off: stderr (tqdm + Fehler) landet nicht in routine.log; die
        # vollstaendige Zusammenfassung steht weiterhin in watchdog.log und
        # routine_history.jsonl.
        def _make_book_transform():
            in_json = False

            def transform(line: str):
                nonlocal in_json
                # Final JSON dump from watchdog --json: a single "{" on its own
                # line that runs to EOF.
                if line.rstrip() == "{":
                    in_json = True
                    return None
                if in_json:
                    return None
                return line

            return transform

        def _pump(stream, terminal, buf=None, transform=None):
            try:
                for line in iter(stream.readline, ""):
                    display = transform(line) if transform else line
                    if display is not None:
                        terminal.write(display)
                        terminal.flush()
                    if buf is not None:
                        buf.append(line)
                    with log_lock:
                        log_handle.write(line)
                        log_handle.flush()
            finally:
                stream.close()

        try:
            proc = subprocess.Popen(
                cmd, env=env, cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE, stderr=None,
                text=True, encoding="utf-8", bufsize=1,
            )
        except OSError as e:
            log_handle.write(f"FEHLER beim Start: {e}\n")
            log_handle.close()
            raise

        t_out = threading.Thread(
            target=_pump,
            args=(proc.stdout, sys.stdout, stdout_buf, _make_book_transform()),
            daemon=True)
        t_out.start()

        # Phase B drains the fulltext backlog and may run for days; the
        # Scheduler-Task uses ExecutionTimeLimit=Zero. Phase A is the
        # fast daily run with an 8h ceiling. For other adapters we keep
        # the 8h ceiling as a safety net.
        is_phase_b = adapter == "calibre" and phase == "B"
        wait_timeout = None if is_phase_b else 8 * 3600

        timed_out = False
        try:
            returncode = proc.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                returncode = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                returncode = -1
            timed_out = True

        t_out.join(timeout=5)

        end = datetime.now().astimezone()
        duration = (end - start).total_seconds()

        if timed_out:
            with log_lock:
                log_handle.write(f"\n=== {end.isoformat()} TIMEOUT ===\n")
            log_handle.close()
            # Marker schreiben, damit Phase A am selben Tag nicht erneut startet
            # (verhindert Endlos-Retry bei wiederholten Logons).
            marker.write_text(end.isoformat(), encoding="utf-8")
            record = {
                "timestamp": start.isoformat(), "source": args.source,
                "adapter": adapter, "frequency": args.frequency,
                "exit_code": -1, "duration_s": duration,
                "error": "timeout (>8h)", "stats": {},
            }
            _append_history(history_file, record)
            return 124

        with log_lock:
            log_handle.write(f"\n=== {end.isoformat()} EXIT={returncode} dur={duration:.1f}s ===\n")
        log_handle.close()

        stdout_text = "".join(stdout_buf)
        stats = _parse_stats(stdout_text, adapter)

        # For Calibre/Zotero the JSON blob is suppressed at the terminal,
        # so print a readable summary here.
        if adapter in ("calibre", "zotero") and stats:
            parts = [
                f"Scanned: {stats.get('scanned', '?')}",
                f"New: {stats.get('new_books', 0)}",
                f"Fulltext pending: {stats.get('fulltext_pending', 0)}",
                f"Metadata: {stats.get('metadata_changed', 0)}",
                f"Annotations: {stats.get('annotations_changed', 0)}",
                f"Updated: {stats.get('delta_updates', 0)}",
            ]
            if stats.get('new_indexed'):
                parts.append(f"Newly indexed: {stats['new_indexed']}")
            if stats.get('fulltext_indexed'):
                parts.append(f"Fulltext indexed: {stats['fulltext_indexed']}")
            if stats.get('orphans_removed'):
                parts.append(f"Orphans removed: {stats['orphans_removed']}")
            if stats.get('skipped_no_file'):
                parts.append(f"Not indexable: {stats['skipped_no_file']}")
            parts.append(f"Errors: {stats.get('errors', 0)}")
            print(f"\n  Watchdog finished in {duration:.0f}s — " + " | ".join(parts))

        record = {
            "timestamp": start.isoformat(), "source": args.source,
            "adapter": adapter, "frequency": args.frequency,
            "exit_code": returncode, "duration_s": round(duration, 1),
            "stats": stats,
        }
        _append_history(history_file, record)

        if returncode == 0:
            marker.write_text(end.isoformat(), encoding="utf-8")

        return returncode
    finally:
        heartbeat_stop.set()
        runtime_lock.release()


if __name__ == "__main__":
    sys.exit(main())
