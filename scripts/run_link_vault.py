#!/usr/bin/env python3
"""
ARCHILLES Vault Linker Runner — wrapper around link_vault.py with hard
gating against the Lab indexing routine and monthly throttling.

Logic
-----
1. Load master config, find the ``archilles-lab`` source. The path to the
   external ``link_vault.py`` comes from ``--script`` or the source's
   ``link_vault_script`` key in ``~/.archilles/config.json``.
2. **Hard gate:** read ``<lab>/.archilles/last_routine_run.txt``.  If absent
   or not dated today, SKIP — the linker requires a fresh LanceDB from a
   completed Lab indexing run.  The next logon trigger will retry.
3. **Monthly marker:** if ``<lab>/.archilles/last_link_vault_run.txt`` is
   in the current calendar month, SKIP.
4. Otherwise run ``link_vault.py <lab_library_path> --semantic --apply``.
   Capture output, parse counters best-effort, write history JSONL.
5. On exit code 0, set the monthly marker.

Files written
-------------
``<lab>/.archilles/vault_linker.log``           — full subprocess output
``<lab>/.archilles/vault_linker_history.jsonl`` — one JSON per run
``<lab>/.archilles/last_link_vault_run.txt``    — ISO timestamp of last success

Usage
-----
    python scripts/run_link_vault.py
    python scripts/run_link_vault.py --force      # ignore monthly marker
    python scripts/run_link_vault.py --dry-run    # plan only, no subprocess
    python scripts/run_link_vault.py --bypass-gate  # ignore Lab-routine gate
                                                    # (use only when you know
                                                    # the LanceDB is current)
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.archilles import runtime_lock
from src.archilles.config import load_master_config


def _is_today(marker: Path, now: datetime) -> bool:
    if not marker.exists():
        return False
    try:
        last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return last.date() == now.date()


def _is_same_month(marker: Path, now: datetime) -> bool:
    if not marker.exists():
        return False
    try:
        last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return (last.year, last.month) == (now.year, now.month)


def _append_history(history_file: Path, record: dict) -> None:
    history_file.parent.mkdir(parents=True, exist_ok=True)
    with history_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _parse_stats(stdout: str) -> dict:
    """Best-effort counter extraction from link_vault output."""
    stats: dict = {}
    for key, pat in (
        ("notes_found",       r"Found\s+(\d+)\s+notes"),
        ("unmatched",         r"Unmatched notes:\s+(\d+)"),
        ("mocs_created",      r"Created\s+(\d+)\s+MOC"),
        ("notes_updated",     r"Updated\s+(\d+)\s+note\(s\)"),
        ("semantic_backed",   r"davon\s+(\d+)\s+semantisch gestützt"),
        ("embeddings_loaded", r"(\d+)\s+Dokument-Embeddings geladen"),
        ("inline_links",      r"Linked\s+(\d+)\s+note\(s\)"),
    ):
        m = re.search(pat, stdout)
        if m:
            stats[key] = int(m.group(1))
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ARCHILLES Vault Linker — gated, monthly throttled wrapper.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Ignore monthly marker (still respects Lab-routine gate)")
    parser.add_argument("--bypass-gate", action="store_true",
                        help="Ignore the Lab-routine gate — use only if you know "
                             "the LanceDB is current")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan and exit (no subprocess, no marker update)")
    parser.add_argument("--script", type=Path, default=None,
                        help="Path to the external link_vault.py (overrides the "
                             "source's link_vault_script config key)")
    args = parser.parse_args()

    master = load_master_config()
    if master is None:
        print("No master config found.", file=sys.stderr)
        return 2

    lab = next((s for s in master.sources if s.name == "archilles-lab"), None)
    if lab is None:
        print("Source 'archilles-lab' not in master config.", file=sys.stderr)
        return 2

    link_vault_script = args.script or lab.link_vault_script
    if link_vault_script is None:
        print("No link_vault script configured — pass --script or set "
              "'link_vault_script' on the 'archilles-lab' source in "
              "~/.archilles/config.json.", file=sys.stderr)
        return 2
    if not link_vault_script.exists():
        print(f"link_vault.py not found: {link_vault_script}", file=sys.stderr)
        return 2

    library_path = Path(lab.library_path)
    archilles_dir = library_path / ".archilles"
    archilles_dir.mkdir(parents=True, exist_ok=True)

    routine_marker = archilles_dir / "last_routine_run.txt"
    monthly_marker = archilles_dir / "last_link_vault_run.txt"
    log_file       = archilles_dir / "vault_linker.log"
    history_file   = archilles_dir / "vault_linker_history.jsonl"

    now = datetime.now().astimezone()

    # Gate 1: Lab-Routine muss heute durchgelaufen sein
    if not args.bypass_gate and not _is_today(routine_marker, now):
        msg = (f"[{now.isoformat()}] vault-linker: SKIP "
               f"(Lab-Routine wartet — last_routine_run.txt nicht von heute)")
        print(msg)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        _append_history(history_file, {
            "timestamp": now.isoformat(),
            "skipped": True,
            "reason": "lab_routine_not_today",
        })
        return 0

    # Gate 2: Monatsmarker
    if not args.force and _is_same_month(monthly_marker, now):
        last = monthly_marker.read_text(encoding="utf-8").strip()
        msg = f"[{now.isoformat()}] vault-linker: SKIP (diesen Monat schon gelaufen: {last})"
        print(msg)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        _append_history(history_file, {
            "timestamp": now.isoformat(),
            "skipped": True,
            "reason": "monthly_marker",
            "previous_run": last,
        })
        return 0

    # Semantic linking is link_vault's default since the 2026-08 rework;
    # inline linking is opt-in there and stays off for the routine.
    cmd = [
        sys.executable, str(link_vault_script),
        str(library_path),
        "--apply",
    ]

    if args.dry_run:
        print(f"DRY-RUN — würde ausführen: {' '.join(cmd)}")
        return 0

    # Vault-Linker kann bis zu 4 h laufen — wir brauchen den Heartbeat aus
    # runtime_lock, damit der globale 1-h-Stale-Threshold die laufende
    # Session nicht fälschlich für „gecrasht" hält.
    with runtime_lock.routine_lock("run_link_vault") as acquired:
        if not acquired:
            return 1
        start = datetime.now().astimezone()
        print(f"[{start.isoformat()}] vault-linker: starte {cmd}")
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {start.isoformat()} vault-linker START ===\n")
            f.write(f"cmd: {' '.join(cmd)}\n")

        try:
            proc = subprocess.run(
                cmd, cwd=str(REPO_ROOT),
                capture_output=True, text=True, encoding="utf-8",
                timeout=4 * 3600,
            )
        except subprocess.TimeoutExpired:
            end = datetime.now().astimezone()
            _append_history(history_file, {
                "timestamp": start.isoformat(), "skipped": False,
                "exit_code": -1, "duration_s": (end - start).total_seconds(),
                "error": "timeout (>4h)", "stats": {},
            })
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"=== {end.isoformat()} TIMEOUT ===\n")
            return 124

        end = datetime.now().astimezone()
        duration = (end - start).total_seconds()

        with log_file.open("a", encoding="utf-8") as f:
            f.write(proc.stdout or "")
            if proc.stderr:
                f.write("\n--- stderr ---\n" + proc.stderr)
            f.write(f"\n=== {end.isoformat()} EXIT={proc.returncode} dur={duration:.1f}s ===\n")

        sys.stdout.write(proc.stdout or "")
        if proc.stderr:
            sys.stderr.write(proc.stderr)

        _append_history(history_file, {
            "timestamp": start.isoformat(), "skipped": False,
            "exit_code": proc.returncode, "duration_s": round(duration, 1),
            "stats": _parse_stats(proc.stdout or ""),
        })

        if proc.returncode == 0:
            monthly_marker.write_text(end.isoformat(), encoding="utf-8")

        return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
