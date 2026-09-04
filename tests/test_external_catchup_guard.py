"""Bounds on the external catch-up nomination (follow-up to finding 1.2).

1.2 made discovery derive candidates from the index instead of the marker.
The guard the briefing relied on — "a purely flat library must not nominate
its whole corpus" — is ``has_parent_chunks()``, and a measurement against the
real index showed it is too weak to carry that weight: three test books from
February 2026 set it True, which opened the gate for 4 273 books and 1.5 M
chunks.

Tightening that gate is the wrong repair (with three hierarchical books any
threshold returns to nominating nothing, i.e. back to the bug). So the size is
allowed to be large — it just may not be *silent*.
"""

import pytest

from src.archilles.external_catchup_guard import (
    ALLOW_LARGE_CATCHUP_FLAG,
    DERIVED_COUNT_LIMIT,
    check_external_catchup_bound,
)


class TestSmallNominations:
    def test_empty_nomination_is_allowed(self):
        b = check_external_catchup_bound(marked_count=0, derived_count=0)
        assert b.allowed is True

    def test_trickle_below_the_limit_passes(self):
        b = check_external_catchup_bound(marked_count=2, derived_count=5)
        assert b.allowed is True
        assert b.nominated_count == 7


class TestMarkerIsAlwaysTrusted:
    def test_huge_marked_set_is_never_blocked(self):
        """``pending_external`` is only written by an explicit
        ``mode: full-external`` decision — that is the operator's own doing
        and never a surprise, however large."""
        b = check_external_catchup_bound(
            marked_count=DERIVED_COUNT_LIMIT * 50, derived_count=0
        )
        assert b.allowed is True

    def test_only_the_derived_half_counts_towards_the_bound(self):
        b = check_external_catchup_bound(
            marked_count=10_000, derived_count=DERIVED_COUNT_LIMIT
        )
        assert b.allowed is True


class TestLargeDerivedNominations:
    def test_refuses_above_the_limit(self):
        b = check_external_catchup_bound(
            marked_count=0, derived_count=DERIVED_COUNT_LIMIT + 1
        )
        assert b.allowed is False
        assert ALLOW_LARGE_CATCHUP_FLAG in b.reason

    def test_flag_authorises_it(self):
        b = check_external_catchup_bound(
            marked_count=0, derived_count=DERIVED_COUNT_LIMIT + 1, allow_large=True
        )
        assert b.allowed is True
        assert "allowed explicitly" in b.reason

    def test_the_real_measured_case_is_refused_by_default(self):
        """The numbers this guard exists for: 4 273 derived, marker empty."""
        b = check_external_catchup_bound(marked_count=0, derived_count=4273)
        assert b.allowed is False
        assert "4273" in b.reason.replace(" ", "").replace("\u202f", "")

    def test_reason_names_both_halves_so_the_split_is_visible(self):
        b = check_external_catchup_bound(marked_count=7, derived_count=4273)
        assert "7" in b.reason and "4273" in b.reason.replace(" ", "")


class TestReporting:
    def test_as_dict_is_json_shaped(self):
        d = check_external_catchup_bound(marked_count=1, derived_count=2).as_dict()
        assert set(d) == {
            "allowed", "nominated_count", "marked_count", "derived_count", "reason",
        }
        assert d["nominated_count"] == 3


class TestCliWiring:
    def test_flag_exists_and_defaults_false(self):
        from scripts.batch_index import build_parser
        args = build_parser().parse_args(["--prepare-pending-external"])
        assert args.allow_large_external_catchup is False

    def test_flag_can_be_set(self):
        from scripts.batch_index import build_parser
        args = build_parser().parse_args(
            ["--prepare-pending-external", "--allow-large-external-catchup"]
        )
        assert args.allow_large_external_catchup is True


class TestNomination:
    """``nominate_external_catchup`` must return the two halves disjoint, so
    the guard can count them separately."""

    def test_halves_are_disjoint(self):
        from types import SimpleNamespace
        from scripts.batch_index import nominate_external_catchup

        store = SimpleNamespace(
            get_pending_external_book_ids=lambda: {"a", "b"},
            has_parent_chunks=lambda: True,
            get_book_ids_without_parent_chunks=lambda: {"b", "c"},
        )
        marked, derived = nominate_external_catchup(store)

        assert marked == {"a", "b"}
        assert derived == {"c"}, "the marked half must be subtracted"
        assert not (marked & derived)

    def test_flat_index_yields_no_derived_half(self):
        from types import SimpleNamespace
        from scripts.batch_index import nominate_external_catchup

        store = SimpleNamespace(
            get_pending_external_book_ids=lambda: {"a"},
            has_parent_chunks=lambda: False,
            get_book_ids_without_parent_chunks=lambda: {"x", "y"},
        )
        assert nominate_external_catchup(store) == ({"a"}, set())
