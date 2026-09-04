"""Chunk text has exactly one implementation (finding 1.8).

``scripts/patch_comments.py`` carried a hand-copied duplicate of
``Indexer._build_comment_chunks`` — same constants, same wording, no coupling.
That is the mechanism behind the ``Kernaussagen:`` / ``Key points:`` split in
the corpus, and the rule the review draws from it is worth stating plainly:

    **any function that composes chunk *text* has exactly one implementation,
    because its output is frozen into vectors.**

Comparing the two before merging them found a divergence the review does not
mention: the duplicate joined tags with ``" / "`` while the indexer uses
``", "``.

Measuring it properly mattered more than finding it. A first count said "11
rows carry the slash form" — but almost all of those are tag *names* that
contain a slash (BISAC categories like ``HISTORY / Military / Aviation``),
where ``", "`` is already the separator. Exactly **one book** used ``" / "`` as
a separator: its two ``calibre_comment`` rows disagreed with the other rows of
the same book and with Calibre. The mechanical replacement that the first count
suggested would have shredded 305 correct rows.
"""

import pytest

from src.archilles.comment_chunks import build_comment_chunks, format_tags


META = {
    "title": "Ein Buch",
    "author": "A. Autor",
    "publisher": "Verlag",
    "calibre_id": 42,
    "tags": ["Geschichte", "Antike"],
    "year": 1999,
    "comments_html": (
        "<h2>Erste Sektion</h2>"
        "<p>Text mit <strong>wichtiger Stelle</strong> darin.</p>"
    ),
}


class TestOneComposition:
    def test_the_indexer_uses_the_shared_function(self):
        """Not "produces the same output" — literally the same code."""
        import inspect

        from src.archilles.engine.indexing import Indexer

        source = inspect.getsource(Indexer._build_comment_chunks)
        assert "build_comment_chunks(" in source

    def test_patch_comments_uses_the_shared_function(self):
        import inspect

        from scripts import patch_comments

        source = inspect.getsource(patch_comments)
        assert "from src.archilles.comment_chunks import" in source
        assert "MAX_COMMENT_WORDS = 400" not in source, (
            "a second copy of the constant is a second implementation waiting"
        )


class TestTagsHaveOneFormat:
    """The divergence found while merging: ', ' against ' / '."""

    def test_the_canonical_separator_is_the_comma(self):
        assert format_tags(["a", "b"]) == "a, b"

    def test_a_tag_containing_a_slash_survives(self):
        """BISAC categories are one tag, not three. The live index holds 305
        such rows, and a naive ' / ' -> ', ' repair would have split them."""
        assert format_tags(["HISTORY / Military / Aviation", "Aviation"]) ==             "HISTORY / Military / Aviation, Aviation"

    def test_a_string_passes_through(self):
        assert format_tags("already, formatted") == "already, formatted"

    def test_none_and_empty(self):
        assert format_tags(None) == ""
        assert format_tags([]) == ""

    def test_chunks_carry_the_canonical_form(self):
        chunks = build_comment_chunks(META, "42", "epub", "hash")
        assert chunks[0]["tags"] == "Geschichte, Antike"


class TestCompositionIsUnchanged:
    """The merge must not move a single character of chunk text: every one of
    these strings is already frozen into vectors in the live index."""

    def test_key_points_wording(self):
        chunks = build_comment_chunks(META, "42", "epub", "hash")
        assert "Key points: wichtiger Stelle" in chunks[0]["text"]

    def test_headline_wrapping(self):
        chunks = build_comment_chunks(META, "42", "epub", "hash")
        assert "## Erste Sektion ##" in chunks[0]["text"]

    def test_prefix_and_id_scheme(self):
        chunks = build_comment_chunks(META, "42", "epub", "hash")
        assert chunks[0]["text"].startswith("[CALIBRE_COMMENT] ")
        assert chunks[0]["id"] == "42_comment_0"
        assert chunks[0]["chunk_index"] == -1

    def test_plain_comments_without_html(self):
        chunks = build_comment_chunks(
            {"title": "T", "comments": "Nur Text"}, "7", "pdf", "h",
        )
        assert len(chunks) == 1
        assert chunks[0]["text"] == "[CALIBRE_COMMENT] Nur Text"

    def test_no_comments_yields_nothing(self):
        assert build_comment_chunks({"title": "T"}, "7", "pdf", "h") == []

    def test_long_sections_split_at_sentence_boundaries(self):
        long_text = " ".join(f"Satz {i} ist hier." for i in range(200))
        chunks = build_comment_chunks(
            {"title": "T", "comments": long_text}, "7", "pdf", "h",
        )
        assert len(chunks) > 1
        for c in chunks:
            assert c["text"].rstrip().endswith("."), "split on sentences, not words"

    def test_key_passages_only_on_the_first_split_part(self):
        """A hoisted key passage must not be repeated into every sub-chunk."""
        long_text = " ".join(f"Satz {i} ist hier." for i in range(200))
        meta = {
            "title": "T",
            "comments_html": f"<p><strong>Wichtig</strong> {long_text}</p>",
        }
        chunks = build_comment_chunks(meta, "7", "pdf", "h")

        assert sum("Key points:" in c["text"] for c in chunks) == 1

    def test_section_title_is_set_only_with_a_headline(self):
        with_headline = build_comment_chunks(META, "42", "epub", "hash")
        without = build_comment_chunks(
            {"title": "T", "comments": "x"}, "7", "pdf", "h",
        )
        assert with_headline[0]["section_title"] == "Erste Sektion"
        assert "section_title" not in without[0]


class TestMetadataFields:
    def test_book_metadata_is_applied(self):
        chunks = build_comment_chunks(META, "42", "epub", "hash")
        c = chunks[0]

        assert c["book_title"] == "Ein Buch"
        assert c["author"] == "A. Autor"
        assert c["publisher"] == "Verlag"
        assert c["calibre_id"] == 42
        assert c["metadata_hash"] == "hash"
        assert c["format"] == "epub"

    def test_missing_fields_are_simply_absent(self):
        chunks = build_comment_chunks(
            {"title": "T", "comments": "x"}, "7", "pdf", "h",
        )
        assert "author" not in chunks[0]
        assert "publisher" not in chunks[0]
