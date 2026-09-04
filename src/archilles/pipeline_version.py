"""The generation marker every chunk carries — review finding 1.7(a).

Until now the only provenance on a chunk was ``indexed_at``: a timestamp, and
nothing about *how* the row was produced. After a year of pipeline changes
there was no query that answered "which generation is this row from", so drift
could only be found by noticing its symptoms in the text — which is how the
``Kernaussagen:`` / ``Key points:`` split was found, months after the fact and
by accident.

``PIPELINE_VERSION`` closes that. ``add_chunks`` writes it into every row; the
column is migratable with default ``''``, so the 1.5 M rows written before it
existed keep the empty string. That empty string is not a gap — it *is* the
signal: ``pipeline_version = ''`` plus ``indexed_at`` locates the pre-marker
generations precisely, which is all that was ever missing.

When to bump
------------
Bump when a change alters the **text or metadata that goes into an embedding**
for rows that already exist — a new separator, a changed prefix, a different
paratext filter, a chunker boundary change. Those are the changes that split a
corpus into two populations that only a re-embed reunites; see the rule at the
top of ``comment_chunks``.

Do **not** bump for changes that leave written rows comparable: a new search
mode, a faster writer, a fix in code that has never run against this index.
A version that changes for reasons invisible in the data is a version nobody
trusts, and the point of the marker is that ``''`` versus ``'1'`` means
something specific.

Bumping is cheap and never rewrites rows: old rows keep their old value, which
is the whole purpose. Add a line to the history below when you bump, because
the number alone says "different", not "different how".

History
-------
``''``   Everything written before 2026-09-04. Spans every generation up to
         and including the ``Kernaussagen:`` / ``Key points:`` split and the
         EPUB paratext fix (``85f2f69``, 2026-08-10) — the marker cannot
         separate those from each other, only from what follows.
``1``    First marked generation. Comment-chunk composition unified in
         ``comment_chunks`` (finding 1.8); ``strip_html`` no longer splits
         words at inline markup (finding 1.12).
"""

from __future__ import annotations

PIPELINE_VERSION = "1"
