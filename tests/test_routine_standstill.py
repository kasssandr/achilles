"""Tests for review finding 1.10(a+b) — telling an intended standstill from an
unintended one.

``routine_history.jsonl`` recorded what a run *did* and never what it was asked
to do, so "0 indexed" could not be classified: a routine that saw 523 new
titles and took in none read exactly like one that had nothing to do. Two
pieces close that: ``_derive_intent`` writes the asked-for work into the record
(a), and the weekly mail prints what was taken in and flags the mismatch (b).

The Zotero stat-shape fix rides along: the mail branched on ``adapter ==
"calibre"``, so Zotero — whose watchdog writes the very same keys — fell into
the batch_index branch and printed three permanent zeros.
"""

from pathlib import Path

from scripts.run_routine import _build_command, _derive_intent
from scripts.weekly_status_mail import _format_source_block


def _row(intent=None, **stats):
    row = {
        "timestamp": "2026-09-04T09:00:00+02:00",
        "exit_code": 0,
        "duration_s": 12,
        "stats": stats,
    }
    if intent is not None:
        row["intent"] = intent
    return row


class TestDeriveIntent:
    """Intent is read back from the finished command, so it cannot drift away
    from what was actually executed."""

    def test_calibre_phase_a_is_metadata_only(self):
        intent = _derive_intent(_build_command("calibre", phase="A"))

        assert intent["index_metadata_only"] is True
        assert intent["index_fulltext_pending"] is False

    def test_calibre_phase_b_is_fulltext_pending(self):
        intent = _derive_intent(_build_command("calibre", phase="B"))

        assert intent["index_fulltext_pending"] is True
        assert intent["index_metadata_only"] is False

    def test_the_two_calibre_phases_are_distinguishable(self):
        """The history record carried no phase at all: 85 runs, one of which
        has ``fulltext_indexed > 0``, and nothing said which were phase B."""
        assert _derive_intent(_build_command("calibre", phase="A")) != \
               _derive_intent(_build_command("calibre", phase="B"))

    def test_zotero_is_index_new(self):
        intent = _derive_intent(_build_command("zotero"))

        assert intent["index_new"] is True

    def test_batch_index_skip_existing_counts_as_index_new(self):
        """``--all --skip-existing`` expresses the same intent as
        ``--index-new``: take in everything not yet in the index."""
        assert _derive_intent(_build_command("obsidian"))["index_new"] is True

    def test_every_key_is_always_present(self):
        for adapter in ("calibre", "zotero", "obsidian", "folder"):
            intent = _derive_intent(_build_command(adapter))
            assert set(intent) == {
                "index_new", "index_metadata_only", "index_fulltext_pending"
            }


class TestWeeklyMailReportsWhatWasTakenIn:
    def test_new_indexed_and_fulltext_indexed_appear(self):
        block = _format_source_block(
            "archilles", "calibre", Path("D:/lib"),
            [_row(new_books=9, new_indexed=9, fulltext_indexed=2, errors=0)],
        )

        assert "Aufgenommen: neu 9" in block
        assert "Volltext 2" in block

    def test_errors_appear(self):
        block = _format_source_block(
            "archilles", "calibre", Path("D:/lib"),
            [_row(new_books=1, new_indexed=1, errors=4)],
        )

        assert "Fehler: 4" in block

    def test_zotero_gets_the_watchdog_stat_shape(self):
        """Zotero records carry new_books/new_indexed, not indexed/skipped/
        failed — the old branch printed zeros for keys Zotero never writes."""
        block = _format_source_block(
            "archilles-zotero", "zotero", Path("D:/Zotero"),
            [_row(new_books=520, new_indexed=17, delta_updates=21)],
        )

        assert "Neue Bücher: 520" in block
        assert "Aufgenommen: neu 17" in block
        assert "übersprungen" not in block

    def test_batch_index_sources_keep_their_own_shape(self):
        block = _format_source_block(
            "archilles-lab", "obsidian", Path("D:/Archilles-Lab"),
            [_row(indexed=12, skipped=3, failed=1)],
        )

        assert "Indexiert: 12" in block
        assert "übersprungen: 3" in block


class TestStandstillFlag:
    def test_seen_but_not_taken_in_is_flagged(self):
        """The July pattern: 520 new items, none indexed, exit 0."""
        block = _format_source_block(
            "archilles-zotero", "zotero", Path("D:/Zotero"),
            [_row(intent={"index_new": True}, new_books=520, new_indexed=0,
                  delta_updates=0)],
        )

        assert "Stillstand" in block
        assert "0 von zuletzt 520 wartenden neuen Titeln" in block

    def test_a_working_run_is_not_flagged(self):
        block = _format_source_block(
            "archilles", "calibre", Path("D:/lib"),
            [_row(intent={"index_metadata_only": True}, new_books=9,
                  new_indexed=9, delta_updates=3)],
        )

        assert "Stillstand" not in block

    def test_nothing_to_do_is_not_a_standstill(self):
        block = _format_source_block(
            "archilles", "calibre", Path("D:/lib"),
            [_row(intent={"index_metadata_only": True}, new_books=0,
                  new_indexed=0, delta_updates=0)],
        )

        assert "Stillstand" not in block

    def test_delta_updates_do_not_excuse_a_stuck_queue(self):
        """The review's fix shape counts delta_updates as progress. Replayed
        against the real Zotero runs of 2026-06-30/07-01 — the review's own
        motivating case — that condition stays silent: 520 seen, 0 taken in,
        21 delta updates. A metadata change on a book already in the index
        says the run was not dead, not that the queue moved."""
        block = _format_source_block(
            "archilles-zotero", "zotero", Path("D:/Zotero"),
            [_row(intent={"index_new": True}, new_books=520, new_indexed=0,
                  delta_updates=21)],
        )

        assert "0 von zuletzt 520 wartenden neuen Titeln" in block

    def test_records_without_intent_are_never_flagged(self):
        """Runs from before 1.10(a) cannot be classified. Guessing would put
        a warning on the whole existing history."""
        block = _format_source_block(
            "archilles-zotero", "zotero", Path("D:/Zotero"),
            [_row(new_books=520, new_indexed=0, delta_updates=0)],
        )

        assert "Stillstand" not in block

    def test_fulltext_axis_is_checked_separately(self):
        """The live case: phase A takes in its handful of new stubs every day,
        so a combined sum never reaches zero and a phase B that has drained
        nothing since May would never be flagged."""
        rows = [
            _row(intent={"index_metadata_only": True}, new_books=1,
                 new_indexed=1, fulltext_pending=4638, fulltext_indexed=0),
            _row(intent={"index_fulltext_pending": True}, new_books=0,
                 new_indexed=0, fulltext_pending=4638, fulltext_indexed=0),
        ]

        block = _format_source_block("archilles", "calibre", Path("D:/lib"), rows)

        assert "0 von zuletzt 4638 wartenden Volltexten" in block
        assert "wartenden neuen Titeln" not in block

    def test_phase_a_alone_does_not_raise_the_fulltext_flag(self):
        """A pending backlog is not a standstill for a run that was never
        asked to drain it."""
        block = _format_source_block(
            "archilles", "calibre", Path("D:/lib"),
            [_row(intent={"index_metadata_only": True}, new_books=1,
                  new_indexed=1, fulltext_pending=4638, fulltext_indexed=0)],
        )

        assert "Stillstand" not in block

    def test_queue_size_is_the_peak_not_the_sum(self):
        """Each run snapshots the same backlog; summing seven runs would
        report 32466 waiting titles where 4638 wait."""
        rows = [
            _row(intent={"index_fulltext_pending": True}, fulltext_pending=4638,
                 fulltext_indexed=0)
            for _ in range(7)
        ]

        block = _format_source_block("archilles", "calibre", Path("D:/lib"), rows)

        assert "zuletzt 4638 wartenden Volltexten" in block

    def test_a_drained_backlog_is_not_flagged(self):
        block = _format_source_block(
            "archilles", "calibre", Path("D:/lib"),
            [_row(intent={"index_fulltext_pending": True}, fulltext_pending=4638,
                  fulltext_indexed=12)],
        )

        assert "Stillstand" not in block
