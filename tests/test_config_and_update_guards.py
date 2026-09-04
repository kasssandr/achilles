"""Two guards against silently ignored input (findings 1.7b, 1.14).

**1.7(b)** ``LanceDBStore.update_metadata_fields`` filters the caller's dict
against ``self.table.schema.names`` and writes whatever survives. ``text`` and
``vector`` are in ``schema.names``. Nothing but a docstring stands between the
codebase and a text update without a matching embedding — a row whose vector
describes text it no longer contains, which no query can detect and no scan
repairs.

**1.14** ``get_rag_db_path`` reads ``rag_db_path``; the live Calibre library
config sets ``db_path``. The key is silently ignored, and it works today only
because the value happens to equal the fallback. Point that setting at another
drive and nothing moves — the routine keeps writing to the old location, with
no warning anywhere. A typo in any other key behaves the same way, so the fix
is to notice unknown keys rather than to add one alias.
"""

import json

import numpy as np
import pytest

from src.storage.lancedb_store import LanceDBStore


def _chunks(n, book_id="b"):
    return [
        {
            "id": f"{book_id}_chunk_{i}",
            "text": f"chunk {i}",
            "book_id": book_id,
            "calibre_id": 0,
            "chunk_index": i,
            "chunk_type": "content",
        }
        for i in range(n)
    ]


def _emb(n, dim=1024):
    v = np.random.randn(n, dim).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


class TestUpdateRefusesTextAndVector:
    """A metadata update must stay a metadata update."""

    @pytest.fixture
    def store(self, tmp_path):
        s = LanceDBStore(db_path=str(tmp_path / "db"))
        s.add_chunks(_chunks(3), _emb(3))
        return s

    def test_text_is_refused(self, store):
        with pytest.raises(ValueError) as exc:
            store.update_metadata_fields("b", {"text": "rewritten"})

        assert "text" in str(exc.value)
        assert "embed" in str(exc.value).lower(), "the message must say why"

    def test_vector_is_refused(self, store):
        with pytest.raises(ValueError):
            store.update_metadata_fields("b", {"vector": [0.0] * 1024})

    def test_refusal_happens_before_anything_is_written(self, store):
        with pytest.raises(ValueError):
            store.update_metadata_fields("b", {"tags": "new", "text": "rewritten"})

        rows = store.get_by_book_id("b", limit=10)
        assert all(r.get("tags") != "new" for r in rows), (
            "a mixed update must not apply its legitimate half"
        )
        assert all(r["text"].startswith("chunk") for r in rows)

    def test_ordinary_metadata_updates_still_work(self, store):
        updated = store.update_metadata_fields("b", {"author": "New Author"})

        assert updated == 3
        assert all(r["author"] == "New Author"
                   for r in store.get_by_book_id("b", limit=10))

    def test_an_empty_update_is_still_a_no_op(self, store):
        assert store.update_metadata_fields("b", {}) == 0


class TestUnknownConfigKeysAreReported:
    """The live Calibre config has carried an ignored `db_path` for months."""

    def _write(self, tmp_path, config):
        d = tmp_path / ".archilles"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return tmp_path

    def test_the_real_world_typo_is_named(self, tmp_path, caplog):
        from src.archilles.config import get_rag_db_path

        lib = self._write(tmp_path, {"db_path": "D:/elsewhere/rag_db"})

        with caplog.at_level("WARNING"):
            path = get_rag_db_path(lib)

        assert "db_path" in caplog.text
        assert "rag_db_path" in caplog.text, "name the key that was meant"
        assert path == str(lib / ".archilles" / "rag_db"), (
            "behaviour is unchanged — the key is still not honoured, only visible"
        )

    def test_known_keys_are_silent(self, tmp_path, caplog):
        from src.archilles.config import get_rag_db_path

        lib = self._write(tmp_path, {
            "rag_db_path": "rag_db",
            "excluded_tags": ["exclude"],
            "languages": ["de"],
            "enable_reranking": True,
            "reranker_device": "cpu",
            "mode": "auto",
            "embedder": {},
        })

        with caplog.at_level("WARNING"):
            get_rag_db_path(lib)

        assert "unknown" not in caplog.text.lower()

    def test_an_arbitrary_typo_is_reported_too(self, tmp_path, caplog):
        """The point of the general check: it catches the next one as well."""
        from src.archilles.config import get_rag_db_path

        lib = self._write(tmp_path, {"exlcuded_tags": ["x"]})

        with caplog.at_level("WARNING"):
            get_rag_db_path(lib)

        assert "exlcuded_tags" in caplog.text

    def test_a_missing_config_is_not_a_warning(self, tmp_path, caplog):
        from src.archilles.config import get_rag_db_path

        with caplog.at_level("WARNING"):
            get_rag_db_path(tmp_path)

        assert caplog.text == ""

    def test_each_file_warns_once_not_once_per_getter(self, tmp_path, caplog):
        """get_rag_db_path, get_excluded_tags and friends all read the same
        file; warning per call would print the same line a dozen times a run."""
        from src.archilles.config import get_excluded_tags, get_rag_db_path

        lib = self._write(tmp_path, {"db_path": "x"})

        with caplog.at_level("WARNING"):
            get_rag_db_path(lib)
            get_excluded_tags(lib)
            get_rag_db_path(lib)

        # Count records, not substrings: the one message mentions both
        # 'db_path' and 'rag_db_path'.
        unknown_key_warnings = [
            r for r in caplog.records if "Unknown key" in r.getMessage()
        ]
        assert len(unknown_key_warnings) == 1


class TestTheKnownKeyListStaysComplete:
    """A whitelist is only as good as its completeness, and mine was not:
    the first version warned about ``exclude_patterns``, which FolderAdapter
    reads. A false alarm is worse than no alarm — it trains the reader to
    ignore the line. So the list is checked against the code that reads it."""

    def _keys_read_in_source(self):
        """Every literal key fetched from a parsed library config.json."""
        import re
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        pattern = re.compile(
            r"(?:config|cfg|conf)\.get\(\s*['\"]([a-zA-Z_]+)['\"]"
        )
        found = set()
        for path in list((repo / "src").rglob("*.py")) + list((repo / "scripts").rglob("*.py")):
            found.update(pattern.findall(path.read_text(encoding="utf-8")))
        return found

    def test_every_key_the_code_reads_is_known(self):
        from src.archilles.config import _KNOWN_LIBRARY_CONFIG_KEYS

        read_in_code = self._keys_read_in_source()
        missing = read_in_code - set(_KNOWN_LIBRARY_CONFIG_KEYS)

        assert not missing, (
            f"these keys are read from a config but would be reported as "
            f"unknown: {sorted(missing)}"
        )

    def test_exclude_patterns_specifically(self):
        """The one that actually slipped through — pinned by name."""
        from src.archilles.config import _KNOWN_LIBRARY_CONFIG_KEYS

        assert "exclude_patterns" in _KNOWN_LIBRARY_CONFIG_KEYS

    def test_master_config_keys_are_explained_not_just_flagged(self, tmp_path, caplog):
        """`instance_name` sits in a live config and never had an effect; the
        warning should say where the setting really lives."""
        import json

        from src.archilles.config import get_rag_db_path

        d = tmp_path / ".archilles"
        d.mkdir(parents=True)
        (d / "config.json").write_text(
            json.dumps({"instance_name": "archilles-zotero"}), encoding="utf-8"
        )

        with caplog.at_level("WARNING"):
            get_rag_db_path(tmp_path)

        assert "instance_name" in caplog.text
        assert "master config" in caplog.text
