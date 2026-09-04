"""The three remaining LOW findings (1.11, 1.12, 1.13).

Small, unrelated in location, identical in shape: each is a place where one
concept has two implementations, or where a guard has a door left open next to
it.

**1.11** ``dedupe_chunks --type all`` sets ``chunk_type = None``, and
``_batch_filter`` then omits the ``AND chunk_type = …`` clause — reproducing
exactly the bare ``id IN (...)`` predicate that ``6cc57d7`` was written to
remove, via the flag that most invites a corpus-wide clean-up.

**1.12** ``strip_html`` joins its parts with a space, so inline markup splits
words: ``<i>Hamlet</i>'s`` becomes ``Hamlet 's``. Zotero abstracts and notes
routinely carry ``<i>``, ``<sup>`` and ``<b>`` inside words. The text goes into
an embedding, so the damage is a quietly worse vector that no later scan
corrects.

**1.13** ``get_book_state`` counts ``HIERARCHICAL_TYPES`` while
``get_hashes_for_indexed_books`` uses ``CONTENT_TYPES``, and the two differ on
``EXCHANGE``. A dialogue-chunked document therefore has fulltext according to
the watchdog and no content according to ``index_book``.
"""

import numpy as np
import pytest

from src.archilles.constants import ChunkType
from src.archilles.html_text import strip_html
from src.storage.lancedb_store import LanceDBStore


class TestStripHtmlKeepsWordsIntact:
    """1.12: the join must not manufacture whitespace."""

    def test_inline_markup_inside_a_word(self):
        assert strip_html("<i>pre</i>fix") == "prefix"

    def test_possessive_after_a_tag(self):
        """The live shape: italic titles in Zotero abstracts."""
        assert strip_html("<i>Hamlet</i>'s ghost") == "Hamlet's ghost"

    def test_superscript_footnote_marker(self):
        assert strip_html("Word<sup>1</sup> follows") == "Word1 follows"

    def test_block_level_tags_still_separate(self):
        """<p> and <br> are word boundaries and must stay boundaries."""
        assert strip_html("<p>Hello</p><p>world</p>") == "Hello world"
        assert strip_html("one<br>two") == "one two"
        assert strip_html("<div>a</div><div>b</div>") == "a b"

    def test_list_items_separate(self):
        assert strip_html("<ul><li>one</li><li>two</li></ul>") == "one two"

    def test_existing_whitespace_is_preserved_and_collapsed(self):
        assert strip_html("<p>Hello <b>world</b></p> trailing") == "Hello world trailing"

    def test_entities_still_decode(self):
        assert strip_html("caf&eacute; &amp; bar") == "café & bar"

    def test_empty_and_plain_input(self):
        assert strip_html("") == ""
        assert strip_html("no markup at all") == "no markup at all"


class TestOneDefinitionOfHasContent:
    """1.13: the watchdog and the indexer must agree on what fulltext is."""

    def _rows(self, book_id, chunk_type, n=2):
        # calibre_id must track book_id: get_hashes_for_indexed_books keys on
        # it, and a shared 0 would collapse every book into one entry.
        return [
            {
                "id": f"{book_id}_{chunk_type}_{i}",
                "text": f"{chunk_type} {i}",
                "book_id": book_id,
                "calibre_id": int(book_id) if book_id.isdigit() else 0,
                "chunk_index": i,
                "chunk_type": chunk_type,
            }
            for i in range(n)
        ]

    def _emb(self, n, dim=1024):
        v = np.random.randn(n, dim).astype(np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    @pytest.fixture
    def store(self, tmp_path):
        return LanceDBStore(db_path=str(tmp_path / "db"))

    def test_exchange_chunks_count_as_content(self, store):
        """A dialogue-chunked document has fulltext. Before the fix
        get_book_state said otherwise and index_book would have replaced it
        with generic extraction."""
        store.add_chunks(self._rows("dlg", ChunkType.EXCHANGE), self._emb(2))

        state = store.get_book_state("dlg")
        assert state["has_content"] is True
        assert state["content_count"] == 2

    def test_the_two_queries_agree(self, store):
        """The actual invariant: whatever get_book_state calls content, the
        watchdog's hash query must call content too. These are the two
        implementations that disagreed."""
        store.add_chunks(self._rows("11", ChunkType.EXCHANGE), self._emb(2))
        store.add_chunks(self._rows("12", ChunkType.CONTENT), self._emb(2))
        store.add_chunks(self._rows("13", ChunkType.PHASE1_METADATA, 1), self._emb(1))

        hashes = store.get_hashes_for_indexed_books()
        for book_id in ("11", "12", "13"):
            assert store.get_book_state(book_id)["has_content"] == \
                hashes[int(book_id)]["has_content"], book_id

    def test_a_stub_is_still_not_content(self, store):
        store.add_chunks(self._rows("stub", ChunkType.PHASE1_METADATA, 1), self._emb(1))

        assert store.get_book_state("stub")["has_content"] is False

    def test_annotations_alone_are_not_content(self, store):
        store.add_chunks(self._rows("ann", ChunkType.ANNOTATION), self._emb(2))

        assert store.get_book_state("ann")["has_content"] is False


class TestDedupeNeverEmitsATypeBlindPredicate:
    """1.11: the guard from 6cc57d7 must not have a door next to it."""

    def test_a_concrete_type_is_always_in_the_filter(self):
        from scripts.dedupe_chunks import _batch_filter

        predicate = _batch_filter(["a", "b"], ChunkType.CONTENT)
        assert "chunk_type" in predicate

    def test_no_type_is_refused_rather_than_silently_unqualified(self):
        """The old behaviour returned a bare `id IN (...)`; that is the exact
        predicate the fix removed."""
        from scripts.dedupe_chunks import _batch_filter

        with pytest.raises(ValueError) as exc:
            _batch_filter(["a"], None)

        assert "chunk_type" in str(exc.value)

    def test_all_expands_to_the_concrete_types(self):
        """--type all must run the per-type path once each, not once untyped."""
        from scripts.dedupe_chunks import resolve_chunk_types

        types = resolve_chunk_types("all")

        assert None not in types
        assert len(types) > 1
        assert ChunkType.CONTENT in types
        assert ChunkType.ANNOTATION in types

    def test_a_single_type_resolves_to_itself(self):
        from scripts.dedupe_chunks import resolve_chunk_types

        assert resolve_chunk_types(ChunkType.CONTENT) == [ChunkType.CONTENT]
