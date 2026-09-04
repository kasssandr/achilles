"""Bounds on the external catch-up nomination (follow-up to review 1.2).

Finding 1.2 made ``discover_pending_external_books`` derive candidates from the
index — books with ``content`` chunks and no PARENT/CHILD — instead of trusting
the ``pending_external`` marker alone, which is written only under
``mode: full-external`` and was empty on this machine.

The briefing paired that with a guard: "a purely flat library must not suddenly
nominate its entire corpus", implemented as a ``has_parent_chunks()`` gate. A
read-only measurement against the real Calibre index showed the gate cannot
carry that weight. Of 8 985 indexed books exactly **three** are hierarchical —
test runs from 7./8. February 2026 — and those three set the flag True, opening
the gate for 4 273 books and ~1.5 M content chunks. Tightening the gate is the
wrong repair: with three hierarchical books any threshold nominates nothing at
all, which is the bug 1.2 just fixed.

So the size may be large; it may not be *silent*. The call site runs
``batch_prepare`` without a prompt, and the nominated set feeds a metered
external embedding run afterwards.

Two design differences from :mod:`archilles.orphan_guard`, which this otherwise
mirrors:

* **Absolute, not proportional.** The orphan guard asks whether a deletion is
  plausible *relative* to the library, so it pairs a share with a count. Here
  the risk is cost, and cost is absolute: preparing 100 books is the same work
  and the same rented GPU time whether they are 10 % or 100 % of the corpus.
* **Only the derived half counts.** ``pending_external`` is written by an
  explicit ``mode: full-external`` decision, so a large marked set is the
  operator's own doing and never a surprise. Only the index-derived half can
  grow behind their back, so only it is bounded.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Refuse above this many index-derived books unless the flag is given.
#: A trickle (the lifecycle this path was built for) is a handful of titles per
#: run; three digits is already a bulk operation.
DERIVED_COUNT_LIMIT = 100

#: Name of the CLI flag that authorises a deliberate bulk catch-up.
ALLOW_LARGE_CATCHUP_FLAG = "--allow-large-external-catchup"


@dataclass(frozen=True)
class CatchupBound:
    """Verdict on one proposed external catch-up nomination."""

    allowed: bool
    marked_count: int
    derived_count: int
    reason: str

    @property
    def nominated_count(self) -> int:
        return self.marked_count + self.derived_count

    def as_dict(self) -> dict:
        """Shape for ``results`` / JSON output, so a refusal is reportable."""
        return {
            "allowed": self.allowed,
            "nominated_count": self.nominated_count,
            "marked_count": self.marked_count,
            "derived_count": self.derived_count,
            "reason": self.reason,
        }


def check_external_catchup_bound(
    marked_count: int,
    derived_count: int,
    *,
    allow_large: bool = False,
) -> CatchupBound:
    """Decide whether this catch-up set may be prepared without confirmation.

    Args:
        marked_count: books carrying the ``pending_external`` marker.
        derived_count: books derived from the index shape (finding 1.2).
        allow_large: the operator states the size is intended (the CLI flag).

    Returns a :class:`CatchupBound`; callers must not prepare when ``allowed``
    is False, and should report ``reason`` either way.
    """
    if derived_count <= DERIVED_COUNT_LIMIT:
        return CatchupBound(
            allowed=True,
            marked_count=marked_count,
            derived_count=derived_count,
            reason=(
                f"{marked_count + derived_count} book(s) nominated "
                f"({marked_count} marked, {derived_count} derived) — within "
                f"the bound of {DERIVED_COUNT_LIMIT} derived."
            ),
        )

    if allow_large:
        return CatchupBound(
            allowed=True,
            marked_count=marked_count,
            derived_count=derived_count,
            reason=(
                f"Preparing {marked_count + derived_count} book(s) "
                f"({marked_count} marked, {derived_count} derived from the "
                f"index) — above the bound of {DERIVED_COUNT_LIMIT}, allowed "
                f"explicitly via {ALLOW_LARGE_CATCHUP_FLAG}."
            ),
        )

    return CatchupBound(
        allowed=False,
        marked_count=marked_count,
        derived_count=derived_count,
        reason=(
            f"Refusing to prepare {marked_count + derived_count} book(s): "
            f"{derived_count} of them were derived from the index shape, "
            f"above the bound of {DERIVED_COUNT_LIMIT} ({marked_count} carry "
            f"the pending_external marker and are not bounded). A set this "
            f"size is a bulk catch-up, not the trickle this path was built "
            f"for, and it feeds a metered external embedding run. Re-run with "
            f"{ALLOW_LARGE_CATCHUP_FLAG} if that is intended, or with "
            f"--dry-run to see the list first."
        ),
    )
