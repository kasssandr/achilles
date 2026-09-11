"""A priority tag that reaches nothing must say so.

``first_tags`` matches a tag by its exact name, so renaming a tag in Calibre
silently stops it from ordering anything -- the run goes on, in the wrong order,
without a word. A routine kept prioritising "Judenkoenige" months after the tag
had been renamed to "BBB"; one stray book still carried the old name, so even a
zero-match guard would have missed it. The count is what has to be visible.
"""

from src.archilles.watchdog import report_priority_tags

LIBRARY = {
    1: {"tags": ["BBB", "Geschichte"]},
    2: {"tags": ["BBB"]},
    3: {"tags": ["prio", "BBB"]},
    4: {"tags": ["Judenkoenige"]},      # the one book left on the old name
    5: {"tags": []},
}


def test_each_tag_is_reported_with_the_number_of_items_it_reaches(capsys):
    counts = report_priority_tags(["prio", "BBB"], LIBRARY)

    assert counts == [("prio", 1), ("BBB", 3)]
    assert "Priority tags: prio (1), BBB (3)" in capsys.readouterr().out


def test_a_renamed_tag_is_visible_by_its_count_not_by_an_error(capsys):
    """The failure that happened: one match instead of 3.014, no warning
    anywhere -- only the number tells the reader."""
    counts = report_priority_tags(["prio", "Judenkoenige"], LIBRARY)

    assert counts == [("prio", 1), ("Judenkoenige", 1)]
    assert "Judenkoenige (1)" in capsys.readouterr().out


def test_a_tag_no_item_carries_is_warned_about(capsys):
    results: dict = {}
    report_priority_tags(["BBB", "Tippfehler"], LIBRARY, results)

    out = capsys.readouterr().out
    assert "'Tippfehler'" in out and "match no item" in out
    assert results["warnings"] and "Tippfehler" in results["warnings"][0]
    assert results["priority_tag_counts"] == {"BBB": 3, "Tippfehler": 0}


def test_matching_ignores_case_but_not_the_rest_of_the_name():
    assert report_priority_tags(["bbb"], LIBRARY) == [("bbb", 3)]
    assert report_priority_tags(["BB"], LIBRARY) == [("BB", 0)]


def test_without_priority_tags_nothing_is_said(capsys):
    assert report_priority_tags([], LIBRARY) == []
    assert report_priority_tags(None, LIBRARY) == []
    assert capsys.readouterr().out == ""


def test_the_scan_result_carries_the_counts_for_the_log():
    results: dict = {}
    report_priority_tags(["BBB"], LIBRARY, results)
    assert results["priority_tag_counts"] == {"BBB": 3}
    assert "warnings" not in results


def test_the_log_records_what_each_tag_reached(tmp_path):
    """The run that ordered by a renamed tag looked normal afterwards; the log
    has to say what the tags actually matched."""
    from src.archilles.watchdog import WatchdogScanner

    scanner = object.__new__(WatchdogScanner)
    scanner.archilles_dir = tmp_path
    scanner.log_file = tmp_path / "watchdog.log"
    scanner._write_log({
        'total_time': 1.0, 'new_books': [], 'metadata_changed': [],
        'annotations_changed': [], 'unchanged': [], 'errors': [], 'delta_updates': 0,
        'priority_tag_counts': {'prio': 16, 'Judenkoenige': 1},
    })

    assert "priority_tags: prio (16), Judenkoenige (1)" in \
        scanner.log_file.read_text(encoding="utf-8")
