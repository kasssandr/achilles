#!/usr/bin/env python3
"""
ARCHILLES Weekly Status Mail — sends a one-page summary of the past week's
routine runs across all configured sources via Gmail SMTP.

Reads each source's ``<library>/.archilles/routine_history.jsonl``, filters
to the last 7 days, and assembles a plaintext email.

Auth
----
Reads ``~/.archilles/secrets.env`` (or ``secrets.env.txt`` — Notepad's
silent ``.txt`` suffix is tolerated):

    GMAIL_APP_PASSWORD   required — Gmail app password
    GMAIL_USER           required — sending Gmail address (SMTP login)
    GMAIL_RECIPIENT      optional — defaults to GMAIL_USER

Marker
------
A marker file ``~/.archilles/last_weekly_mail.txt`` holds the ISO timestamp
of the last successful send.  The script skips when the marker falls in the
current ISO calendar week — so if you log in twice on Sunday it sends once.
Use ``--force`` to override.

Usage
-----
    python scripts/weekly_status_mail.py
    python scripts/weekly_status_mail.py --dry-run     # build mail, print, no send
    python scripts/weekly_status_mail.py --force       # ignore weekly marker
"""

import argparse
import json
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.archilles import runtime_lock
from src.archilles.config import load_master_config
from src.archilles.orphan_guard import ORPHAN_COUNT_LIMIT


SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SMTPS


def _load_secrets() -> dict[str, str]:
    """Read all KEY=VALUE pairs from ~/.archilles/secrets.env (or .env.txt)."""
    secrets: dict[str, str] = {}
    home_archilles = Path.home() / ".archilles"
    for name in ("secrets.env", "secrets.env.txt"):
        f = home_archilles / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            secrets.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return secrets


def _read_history(history_file: Path, since: datetime) -> list[dict]:
    if not history_file.exists():
        return []
    rows = []
    for line in history_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["timestamp"])
            if ts >= since:
                rows.append(rec)
        except Exception:
            continue
    return rows


def _format_linker_block(library: Path, rows: list[dict]) -> str:
    lines = [f"  vault-linker  (maintenance, lib: {library})"]
    if not rows:
        lines.append("    Keine Läufe in den letzten 7 Tagen.")
        return "\n".join(lines) + "\n"

    runs = [r for r in rows if not r.get("skipped")]
    skips = [r for r in rows if r.get("skipped")]
    successes = [r for r in runs if r.get("exit_code") == 0]
    failures  = [r for r in runs if r.get("exit_code") != 0]

    lines.append(
        f"    Einträge: {len(rows)}  "
        f"(läufe: {len(runs)} / erfolg: {len(successes)} / fehler: {len(failures)} / "
        f"skips: {len(skips)})"
    )

    if runs:
        last = max(runs, key=lambda r: r.get("timestamp", ""))
        st = last.get("stats", {}) or {}
        if st:
            lines.append(
                f"    Letzter Lauf: notes={st.get('notes_found','?')}  "
                f"MOCs={st.get('mocs_created','?')}  "
                f"updated={st.get('notes_updated','?')}  "
                f"semantic={st.get('semantic_links','?')}"
            )
        lines.append(
            f"    Letzter Lauf: {last.get('timestamp')} "
            f"(exit={last.get('exit_code')}, {last.get('duration_s')}s)"
        )

    if skips:
        last_skip = max(skips, key=lambda r: r.get("timestamp", ""))
        reason = last_skip.get("reason", "?")
        lines.append(f"    Letzter Skip: {last_skip.get('timestamp')} (Grund: {reason})")

    return "\n".join(lines) + "\n"


# Sources whose runs go through scripts/watchdog.py and therefore share one
# stat shape (new_books/new_indexed/…), as opposed to batch_index.py's
# indexed/skipped/failed.
WATCHDOG_ADAPTERS = ("calibre", "zotero")


def _standstills(rows: list[dict], agg, peak) -> list[str]:
    """Weeks in which the routine was asked to index and took in nothing.

    Reads the ``intent`` record written by run_routine (review 1.10a). Runs
    from before that record existed carry no intent; those are left unflagged
    rather than guessed at — an unclassifiable run must not raise an alarm.

    The two axes are checked separately on purpose. One combined sum would
    hide the case that is actually live here: phase A takes in its handful of
    new stubs every day, so a phase B that has drained nothing since May would
    never reach zero and never be flagged.

    The counts name the *queue* ("0 of 520 waiting"), not the week's sum of
    sightings printed above: those two are the same backlog seen seven times,
    and putting "520" next to a summed "1040" in one block would read as a
    contradiction.

    ``delta_updates`` deliberately does not count as progress on the new-title
    axis, against the letter of the review's fix shape. Replayed against the
    real Zotero records of 2026-06-30 and 2026-07-01 — the very runs the review
    names as its motivating case — the combined condition stays silent: those
    runs saw 520 new items, took in none, and carried 21 delta updates, which
    would have excused the standstill. A metadata change on a book already in
    the index says the run was not dead; it says nothing about the queue.
    """
    intents = [r.get("intent") or {} for r in rows]
    wanted = lambda k: any(i.get(k) for i in intents)

    out: list[str] = []
    if (wanted("index_new") or wanted("index_metadata_only"))             and agg("new_indexed") == 0             and peak("new_books") > 0:
        out.append(
            f"Stillstand: 0 von zuletzt {peak('new_books')} wartenden neuen "
            f"Titeln aufgenommen — der Lauf sollte indexieren"
        )
    if wanted("index_fulltext_pending")             and agg("fulltext_indexed") == 0             and peak("fulltext_pending") > 0:
        out.append(
            f"Stillstand: 0 von zuletzt {peak('fulltext_pending')} wartenden "
            f"Volltexten aufgenommen — der Lauf sollte indexieren"
        )
    return out


def _format_source_block(name: str, adapter: str, library: Path, rows: list[dict]) -> str:
    lines = [f"  {name}  (adapter: {adapter}, lib: {library})"]
    if not rows:
        lines.append("    Keine Läufe in den letzten 7 Tagen.")
        return "\n".join(lines) + "\n"

    successes = [r for r in rows if r.get("exit_code") == 0]
    failures  = [r for r in rows if r.get("exit_code") != 0]
    lines.append(f"    Läufe: {len(rows)}  (erfolg: {len(successes)}, fehler: {len(failures)})")

    agg = lambda k: sum((r.get("stats", {}) or {}).get(k, 0) or 0 for r in rows)
    # Queue sizes are per-run snapshots of the *same* backlog, so summing them
    # multiplies one backlog by the number of runs. Take the peak instead.
    peak = lambda k: max(
        [(r.get("stats", {}) or {}).get(k, 0) or 0 for r in rows] or [0]
    )

    # Branch on the stat shape, not on "calibre": the Zotero watchdog writes
    # the same keys, so the old else-branch looked up indexed/skipped/failed
    # on a Zotero record and printed three permanent zeros.
    if adapter in WATCHDOG_ADAPTERS:
        lines.append(
            f"    Neue Bücher: {agg('new_books')}  |  "
            f"Metadaten: {agg('metadata_changed')}  |  "
            f"Annotationen: {agg('annotations_changed')}  |  "
            f"Delta-Updates: {agg('delta_updates')}"
        )
        # What the week actually took in. Without these the July pattern is
        # invisible: a routine that saw 523 new titles and indexed none reads
        # exactly like one that had nothing to do (review 1.10b).
        lines.append(
            f"    Aufgenommen: neu {agg('new_indexed')}  |  "
            f"Volltext {agg('fulltext_indexed')}  |  "
            f"Fehler: {agg('errors')}"
        )
        for stall in _standstills(rows, agg, peak):
            lines.append(f"    ⚠️  {stall}")
    else:
        lines.append(
            f"    Indexiert: {agg('indexed')}  |  "
            f"übersprungen: {agg('skipped')}  |  "
            f"fehlgeschlagen: {agg('failed')}"
        )

    # Deletions, always — at any count, including zero (review 1.10c). This is
    # the only report in which a wrong orphan cleanup would surface, and it
    # runs weekly; a deletion nobody mentions is a deletion nobody notices.
    # Queued but not indexable (review 1.15). Shown only when non-zero: unlike a
    # deletion, "nothing was skipped" is the normal case and needs no line.
    skipped = sum((r.get("stats", {}) or {}).get("skipped_no_file", 0) or 0 for r in rows)
    if skipped:
        lines.append(
            f"    Unindexierbar (in der Warteschlange, ohne Datei): {skipped}"
        )
        lines.append(
            "      Grund steht im watchdog.log der Quelle (skipped_no_file)"
        )

    orphans = sum((r.get("stats", {}) or {}).get("orphans_removed", 0) or 0 for r in rows)
    mark = "  ⚠️  ungewöhnlich viele — bitte prüfen" if orphans > ORPHAN_COUNT_LIMIT else ""
    lines.append(f"    Aus dem Index entfernt (Waisen): {orphans}{mark}")
    if orphans:
        lines.append(
            "      Rollback: <library>/.archilles/backups/orphans_*.parquet"
        )

    last = max(rows, key=lambda r: r.get("timestamp", ""))
    lines.append(
        f"    Letzter Lauf: {last.get('timestamp')} "
        f"(exit={last.get('exit_code')}, {last.get('duration_s')}s)"
    )

    if failures:
        lines.append("    Fehler-Läufe (max. letzte 3):")
        for r in failures[-3:]:
            err = r.get("error") or f"exit_code={r.get('exit_code')}"
            lines.append(f"      - {r.get('timestamp')}: {err}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send weekly ARCHILLES routine status email.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Ignore weekly marker and send anyway")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build mail body and print to stdout, do not send")
    args = parser.parse_args()

    home_archilles = Path.home() / ".archilles"
    home_archilles.mkdir(parents=True, exist_ok=True)
    marker = home_archilles / "last_weekly_mail.txt"

    now = datetime.now().astimezone()
    if marker.exists() and not args.force:
        try:
            last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
            if last.isocalendar()[:2] == now.isocalendar()[:2]:
                print(f"SKIP — Mail wurde diese Woche bereits gesendet ({last.isoformat()})")
                return 0
        except Exception:
            pass

    master = load_master_config()
    if master is None:
        print("Keine Master-Config gefunden.", file=sys.stderr)
        return 2

    since = now - timedelta(days=7)
    blocks = []
    for src in master.sources:
        library = Path(src.library_path)
        history_file = library / ".archilles" / "routine_history.jsonl"
        rows = _read_history(history_file, since)
        blocks.append(_format_source_block(src.name, src.adapter or "?", library, rows))

    # Maintenance: Vault-Linker (separate History-Datei in der Lab-Library)
    lab = next((s for s in master.sources if s.name == "archilles-lab"), None)
    if lab is not None:
        linker_history = Path(lab.library_path) / ".archilles" / "vault_linker_history.jsonl"
        linker_rows = _read_history(linker_history, since)
        blocks.append(_format_linker_block(Path(lab.library_path), linker_rows))

    body = (
        "ARCHILLES Wochen-Status\n"
        f"Stichtag: {now.strftime('%Y-%m-%d %H:%M %Z')}\n"
        "Zeitraum: letzte 7 Tage\n\n"
        + "\n".join(blocks)
        + "\n--\nGeneriert von scripts/weekly_status_mail.py\n"
    )

    if args.dry_run:
        print(body)
        return 0

    with runtime_lock.routine_lock("weekly_status_mail") as acquired:
        if not acquired:
            return 1
        secrets = _load_secrets()
        password = secrets.get("GMAIL_APP_PASSWORD")
        gmail_user = secrets.get("GMAIL_USER")
        recipient = secrets.get("GMAIL_RECIPIENT") or gmail_user
        missing = [k for k, v in (("GMAIL_APP_PASSWORD", password),
                                  ("GMAIL_USER", gmail_user)) if not v]
        if missing:
            print(f"{', '.join(missing)} not found in ~/.archilles/secrets.env(.txt).",
                  file=sys.stderr)
            return 3

        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = f"[Archilles] Wochen-Status {now.strftime('%Y-%m-%d')}"
        msg["From"] = gmail_user
        msg["To"] = recipient

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(gmail_user, password)
                smtp.send_message(msg)
        except Exception as exc:
            print(f"SMTP error: {exc}", file=sys.stderr)
            return 4

        marker.write_text(now.isoformat(), encoding="utf-8")
        print(f"OK — status mail sent to {recipient} ({now.isoformat()})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
