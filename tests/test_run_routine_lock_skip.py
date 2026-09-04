"""A lock timeout must leave a trace in routine.log.

Observed 2026-09-04: a hard reboot left a stale routine lock, three scheduled
routines queued behind it, and two of them waited out their two hours and
gave up. The frequency SKIP is logged, so Phase A's skip was reconstructable;
the lock timeout went only to stderr and died with the console window. From
the log alone a routine that dropped its day looked identical to one that
never started.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.run_routine as rr


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A master config whose only source points at a tmp library.

    Patching ``load_master_config`` is what isolates this test: without it the
    run resolves the real ``~/.archilles/config.json`` and appends to the
    live Calibre library's ``.archilles/routine.log``.
    """
    lib = tmp_path / "lib"
    (lib / ".archilles").mkdir(parents=True)
    source = SimpleNamespace(
        name="archilles", library_path=str(lib), adapter="calibre",
        priority_tags=None, priority_collections=None,
    )
    monkeypatch.setattr(
        rr, "load_master_config",
        lambda: SimpleNamespace(sources=[source]),
    )
    return lib


def _run(monkeypatch, lib, acquired: bool, wait_s=7200):
    monkeypatch.setattr(rr.runtime_lock, "acquire", lambda name, wait_s=0: acquired)
    monkeypatch.setattr(rr.runtime_lock, "start_heartbeat", lambda ev: None)
    monkeypatch.setattr(rr.runtime_lock, "release", lambda: None)
    monkeypatch.setattr(
        sys, "argv",
        ["run_routine.py", "--source", "archilles", "--frequency", "daily",
         "--wait-for-lock", str(wait_s)],
    )
    return rr.main()


class TestLockTimeoutIsLogged:
    def test_timeout_writes_to_routine_log(self, library, monkeypatch, capsys):
        rc = _run(monkeypatch, library, acquired=False)

        assert rc == 1
        log = (library / ".archilles" / "routine.log").read_text(encoding="utf-8")
        assert "SKIP" in log
        assert "routine lock still held" in log
        assert "7200" in log

    def test_timeout_is_distinguishable_from_a_frequency_skip(
        self, library, monkeypatch,
    ):
        """Both are SKIPs; the log must say which, or the entry is useless."""
        _run(monkeypatch, library, acquired=False)

        log = (library / ".archilles" / "routine.log").read_text(encoding="utf-8")
        assert "frequency=" not in log
        assert "lock" in log

    def test_log_line_carries_source_and_timestamp(self, library, monkeypatch):
        _run(monkeypatch, library, acquired=False)

        line = (library / ".archilles" / "routine.log").read_text(
            encoding="utf-8"
        ).strip()
        assert line.startswith("[")
        assert "archilles" in line
