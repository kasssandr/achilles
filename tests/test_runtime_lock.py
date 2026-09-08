"""Tests for ``src.archilles.runtime_lock``.

We monkey-patch ``LOCK_FILE`` to a tmp path in each test so the real
``~/.archilles/routine.lock`` is never touched.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from src.archilles import runtime_lock


@pytest.fixture(autouse=True)
def _isolated_lockfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module-level LOCK_FILE to a fresh tmp path per test."""
    lock = tmp_path / "routine.lock"
    monkeypatch.setattr(runtime_lock, "LOCK_FILE", lock)
    return lock


# ── Low-level acquire / release ─────────────────────────────────────────


class TestAcquireRelease:
    def test_acquire_on_empty_slot(self, _isolated_lockfile: Path):
        assert runtime_lock.acquire("test") is True
        assert _isolated_lockfile.exists()
        content = _isolated_lockfile.read_text(encoding="utf-8")
        assert "test" in content
        assert f"PID={os.getpid()}" in content

    def test_release_removes_lockfile(self, _isolated_lockfile: Path):
        runtime_lock.acquire("test")
        runtime_lock.release()
        assert not _isolated_lockfile.exists()

    def test_release_idempotent(self, _isolated_lockfile: Path):
        runtime_lock.release()  # no lock held
        runtime_lock.release()  # still no-op
        assert not _isolated_lockfile.exists()

    def test_acquire_fails_when_lock_fresh(self, _isolated_lockfile: Path):
        assert runtime_lock.acquire("first") is True
        # Second acquire with wait_s=0 must fail immediately
        assert runtime_lock.acquire("second", wait_s=0) is False
        # First holder's content must still be intact
        assert "first" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_acquire_reclaims_stale_lock(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """If the lockfile mtime is older than STALE_AFTER_S, the next
        caller may reclaim it."""
        _isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
        _isolated_lockfile.write_text("crashed-holder", encoding="utf-8")
        # Backdate the lockfile mtime past the stale threshold
        ancient = time.time() - runtime_lock.STALE_AFTER_S - 60
        os.utime(_isolated_lockfile, (ancient, ancient))

        assert runtime_lock.acquire("new-owner", wait_s=0) is True
        assert "new-owner" in _isolated_lockfile.read_text(encoding="utf-8")


# ── Heartbeat ───────────────────────────────────────────────────────────


class TestHeartbeat:
    def test_heartbeat_refreshes_mtime(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A running heartbeat must refresh the lockfile's mtime when the
        configured interval elapses."""
        # Drive heartbeat from a very short interval so the test stays fast
        monkeypatch.setattr(runtime_lock, "HEARTBEAT_INTERVAL_S", 0.05)

        runtime_lock.acquire("hb-test")
        # Backdate mtime so we can detect the refresh
        old_mtime = time.time() - 1000
        os.utime(_isolated_lockfile, (old_mtime, old_mtime))

        stop = threading.Event()
        runtime_lock.start_heartbeat(stop)
        time.sleep(0.3)  # several intervals
        stop.set()

        new_mtime = _isolated_lockfile.stat().st_mtime
        assert new_mtime > old_mtime + 100, (
            f"Heartbeat did not refresh mtime: old={old_mtime}, new={new_mtime}"
        )

    def test_heartbeat_after_release_does_not_resurrect(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """If release() runs while the heartbeat is mid-tick, the next tick
        must NOT recreate the lockfile (regression guard against using
        ``Path.touch`` with ``exist_ok=True``)."""
        monkeypatch.setattr(runtime_lock, "HEARTBEAT_INTERVAL_S", 0.05)

        runtime_lock.acquire("race-test")
        stop = threading.Event()
        runtime_lock.start_heartbeat(stop)
        time.sleep(0.1)
        runtime_lock.release()
        time.sleep(0.2)  # let several heartbeat ticks fire after release
        stop.set()

        assert not _isolated_lockfile.exists(), (
            "Heartbeat resurrected the lockfile after release()"
        )

    def test_heartbeat_thread_is_daemon(self, _isolated_lockfile: Path):
        """The heartbeat must not block process shutdown."""
        runtime_lock.acquire("daemon-test")
        stop = threading.Event()
        t = runtime_lock.start_heartbeat(stop)
        try:
            assert t.daemon is True
        finally:
            stop.set()
            runtime_lock.release()


# ── Context manager ─────────────────────────────────────────────────────


class TestRoutineLockContext:
    def test_acquired_yields_true_and_holds_lock(self, _isolated_lockfile: Path):
        with runtime_lock.routine_lock("ctx-test") as acquired:
            assert acquired is True
            assert _isolated_lockfile.exists()
            content = _isolated_lockfile.read_text(encoding="utf-8")
            assert "ctx-test" in content
        # Exit must release
        assert not _isolated_lockfile.exists()

    def test_busy_yields_false_and_leaves_other_lock_intact(
        self, _isolated_lockfile: Path,
    ):
        """Acquire externally first, then try the context manager — it
        must yield False AND must not destroy the existing lock."""
        assert runtime_lock.acquire("first") is True
        try:
            with runtime_lock.routine_lock("second", wait_s=0) as acquired:
                assert acquired is False
                # First holder's lockfile must still be intact
                assert "first" in _isolated_lockfile.read_text(encoding="utf-8")
            # On exit of a non-acquired context, the file must STILL be
            # the first holder's — we mustn't release someone else's lock.
            assert _isolated_lockfile.exists()
            assert "first" in _isolated_lockfile.read_text(encoding="utf-8")
        finally:
            runtime_lock.release()

    def test_release_on_exception(self, _isolated_lockfile: Path):
        """The lock must be released even if the body raises."""
        with pytest.raises(RuntimeError, match="boom"):
            with runtime_lock.routine_lock("crash-test") as acquired:
                assert acquired is True
                raise RuntimeError("boom")
        assert not _isolated_lockfile.exists()

    def test_heartbeat_runs_inside_context(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Inside the context, the heartbeat thread refreshes mtime."""
        monkeypatch.setattr(runtime_lock, "HEARTBEAT_INTERVAL_S", 0.05)

        with runtime_lock.routine_lock("hb-ctx"):
            old_mtime = time.time() - 1000
            os.utime(_isolated_lockfile, (old_mtime, old_mtime))
            time.sleep(0.3)
            assert _isolated_lockfile.stat().st_mtime > old_mtime + 100


# ── Crash & reboot recovery ─────────────────────────────────────────────


class TestCrashRecovery:
    """A lock whose holder cannot possibly be alive must be reclaimed
    immediately, without waiting out ``STALE_AFTER_S``."""

    def test_reclaims_lock_written_before_last_boot(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A hard reboot kills the holder but leaves the lockfile behind.
        Its mtime then predates the boot — no process survives that."""
        runtime_lock.acquire("pre-reboot-holder")
        # Pretend the machine booted after the last heartbeat.
        mtime = _isolated_lockfile.stat().st_mtime
        monkeypatch.setattr(
            runtime_lock, "_boot_time", lambda: mtime + 600,
        )

        assert runtime_lock.acquire("post-reboot", wait_s=0) is True
        assert "post-reboot" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_keeps_lock_written_after_last_boot(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The mirror case: a lock newer than the boot belongs to a holder
        that may well be alive — it must not be stolen."""
        runtime_lock.acquire("live-holder")
        mtime = _isolated_lockfile.stat().st_mtime
        monkeypatch.setattr(
            runtime_lock, "_boot_time", lambda: mtime - 600,
        )

        assert runtime_lock.acquire("intruder", wait_s=0) is False
        assert "live-holder" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_reclaims_lock_of_dead_pid_without_reboot(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A crash without a reboot leaves a fresh mtime but a dead PID."""
        _isolated_lockfile.write_text(
            "crashed(routine)  PID=424242  since=2026-09-04T11:38:09",
            encoding="utf-8",
        )
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: False)

        assert runtime_lock.acquire("new-owner", wait_s=0) is True
        assert "new-owner" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_stale_mtime_still_wins_over_a_live_pid(
        self, _isolated_lockfile: Path,
    ):
        """The liveness check may only free a lock *earlier*, never hold one
        longer.  Once the mtime aged past STALE_AFTER_S the lock is up for
        grabs even if the recorded PID is still alive — otherwise a hung
        holder would block every routine forever."""
        _isolated_lockfile.write_text(
            f"hung(script)  PID={os.getpid()}  since=2026-09-04T11:38:09",
            encoding="utf-8",
        )
        ancient = time.time() - runtime_lock.STALE_AFTER_S - 60
        os.utime(_isolated_lockfile, (ancient, ancient))

        assert runtime_lock.acquire("new-owner", wait_s=0) is True
        assert "new-owner" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_live_pid_keeps_a_fresh_lock(self, _isolated_lockfile: Path):
        """Within STALE_AFTER_S a live holder is untouchable.  ``since``
        must be *now*: our own PID cannot have written a lock that predates
        the running interpreter, and the recycling check would say so."""
        _isolated_lockfile.write_text(
            f"running(script)  PID={os.getpid()}  "
            f"since={datetime.now().isoformat()}",
            encoding="utf-8",
        )

        assert runtime_lock.acquire("intruder", wait_s=0) is False
        assert "running(script)" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_reclaims_when_pid_was_recycled(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A recycled PID points at a process that started *after* the lock
        was written — it cannot be the holder."""
        _isolated_lockfile.write_text(
            "old(routine)  PID=4242  since=2026-09-04T11:38:09",
            encoding="utf-8",
        )
        lock_ts = datetime.fromisoformat("2026-09-04T11:38:09").timestamp()
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(
            runtime_lock, "_pid_start_time", lambda pid: lock_ts + 3600,
        )

        assert runtime_lock.acquire("new-owner", wait_s=0) is True
        assert "new-owner" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_reclaims_when_pid_was_recycled_within_the_clock_margin(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: after a reboot the OS reuses a dead holder's PID
        within seconds, so a start time that merely lies "well after" the
        lock proves nothing.  The recorded ``start`` field settles it —
        without it, four routines once waited out a full STALE_AFTER_S
        hour behind a holder that had been dead all along."""
        runtime_lock.acquire("holder")
        info = _isolated_lockfile.read_text(encoding="utf-8")
        holder_start = runtime_lock._parse_start(info)
        assert holder_start is not None, "acquire() must record start="

        # Same PID, but a process that came up 58s later — as the svchost
        # that inherited PID 21888 on 2026-09-05 did.
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(
            runtime_lock, "_pid_start_time", lambda pid: holder_start + 58,
        )

        assert runtime_lock.acquire("new-owner", wait_s=0) is True
        assert "new-owner" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_matching_start_time_keeps_the_lock(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The mirror case: same PID, same creation time — that really is
        the holder, and it keeps its lock."""
        runtime_lock.acquire("holder")
        holder_start = runtime_lock._parse_start(
            _isolated_lockfile.read_text(encoding="utf-8")
        )
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(
            runtime_lock, "_pid_start_time", lambda pid: holder_start,
        )

        assert runtime_lock.acquire("intruder", wait_s=0) is False
        assert "holder" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_unreadable_start_time_keeps_the_lock(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A recorded start we cannot compare against is not evidence of
        death — the mtime rule stays the backstop."""
        runtime_lock.acquire("holder")
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(runtime_lock, "_pid_start_time", lambda pid: None)

        assert runtime_lock.acquire("intruder", wait_s=0) is False

    def test_legacy_lock_reclaimed_when_image_is_not_python(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Lockfiles written before the ``start`` field existed still get
        the weaker check: every tenant is a Python script, so a PID now
        running something else has been recycled."""
        _isolated_lockfile.write_text(
            f"old(routine)  PID=4242  since={datetime.now().isoformat()}",
            encoding="utf-8",
        )
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(
            runtime_lock, "_pid_image_is_python", lambda pid: False,
        )

        assert runtime_lock.acquire("new-owner", wait_s=0) is True
        assert "new-owner" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_legacy_lock_kept_when_image_is_unreadable(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """An image name we cannot read proves nothing — keep the lock."""
        _isolated_lockfile.write_text(
            f"old(routine)  PID=4242  since={datetime.now().isoformat()}",
            encoding="utf-8",
        )
        monkeypatch.setattr(runtime_lock, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(
            runtime_lock, "_pid_image_is_python", lambda pid: None,
        )
        monkeypatch.setattr(runtime_lock, "_pid_start_time", lambda pid: None)

        assert runtime_lock.acquire("intruder", wait_s=0) is False

    def test_lock_line_records_our_own_start_time(
        self, _isolated_lockfile: Path,
    ):
        """The recorded start must be this interpreter's real creation
        time, otherwise the identity check compares noise."""
        psutil = pytest.importorskip("psutil")
        runtime_lock.acquire("self-test")
        recorded = runtime_lock._parse_start(
            _isolated_lockfile.read_text(encoding="utf-8")
        )
        assert recorded is not None
        actual = psutil.Process(os.getpid()).create_time()
        assert abs(recorded - actual) <= runtime_lock._START_TIME_EPSILON_S

    def test_lock_line_without_psutil_omits_start(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Without psutil the field is simply absent and the legacy path
        applies — writing a bogus value would be worse than none."""
        monkeypatch.setattr(runtime_lock, "psutil", None)
        runtime_lock.acquire("no-psutil")
        info = _isolated_lockfile.read_text(encoding="utf-8")
        assert "start=" not in info
        assert runtime_lock._parse_start(info) is None

    def test_unparseable_lockfile_is_left_to_the_mtime_rule(
        self, _isolated_lockfile: Path,
    ):
        """Without a PID we cannot prove the holder is gone, so a fresh
        lockfile stays busy — the STALE_AFTER_S rule remains the backstop."""
        _isolated_lockfile.write_text("garbage without markers", encoding="utf-8")

        assert runtime_lock.acquire("intruder", wait_s=0) is False


class TestBootTimeHelper:
    def test_boot_time_is_in_the_past_and_plausible(self):
        boot = runtime_lock._boot_time()
        now = time.time()
        assert boot < now, "boot time must lie in the past"
        # A machine that booted more than 10 years ago is not plausible.
        assert now - boot < 10 * 365 * 24 * 3600


# ── release() ownership ─────────────────────────────────────────────────


class TestReleaseOnlyOwnLock:
    """``release()`` must never remove a lock that belongs to someone else.

    A runner that finishes while a *later* tenant already holds the lock
    used to delete that tenant's lockfile, letting a third routine start
    alongside it — two indexing runs on one GPU.  Every uncertain case
    here keeps the lockfile: ``STALE_AFTER_S`` and the liveness checks in
    :func:`acquire` remain the backstop for a genuinely abandoned lock.
    """

    def test_keeps_lock_held_by_another_pid(self, _isolated_lockfile: Path):
        _isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
        _isolated_lockfile.write_text(
            f"other-runner  PID={os.getpid() + 1}  "
            f"since={datetime.now().isoformat()}  start=1.0",
            encoding="utf-8",
        )

        runtime_lock.release()

        assert _isolated_lockfile.exists()
        assert "other-runner" in _isolated_lockfile.read_text(encoding="utf-8")

    def test_keeps_lock_whose_start_time_is_not_ours(
        self, _isolated_lockfile: Path,
    ):
        """Same PID, different creation time — a recycled PID, not us."""
        _isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
        _isolated_lockfile.write_text(
            f"recycled  PID={os.getpid()}  "
            f"since={datetime.now().isoformat()}  start=1.0",
            encoding="utf-8",
        )

        runtime_lock.release()

        assert _isolated_lockfile.exists()

    def test_keeps_lock_without_pid_field(self, _isolated_lockfile: Path):
        """An unparseable holder is not provably us, so it stays."""
        _isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
        _isolated_lockfile.write_text("legacy-holder", encoding="utf-8")

        runtime_lock.release()

        assert _isolated_lockfile.exists()

    def test_releases_legacy_lock_with_our_pid_and_no_start(
        self, _isolated_lockfile: Path,
    ):
        """A lockfile predating the ``start`` field still releases on PID."""
        _isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
        _isolated_lockfile.write_text(
            f"legacy  PID={os.getpid()}  since={datetime.now().isoformat()}",
            encoding="utf-8",
        )

        runtime_lock.release()

        assert not _isolated_lockfile.exists()

    def test_still_releases_our_own_lock(self, _isolated_lockfile: Path):
        """The regression guard: the normal path must keep working."""
        assert runtime_lock.acquire("mine") is True
        runtime_lock.release()
        assert not _isolated_lockfile.exists()

    def test_context_manager_keeps_a_stolen_lock(
        self, _isolated_lockfile: Path,
    ):
        """The high-level API inherits the ownership guard."""
        with runtime_lock.routine_lock("mine") as got_it:
            assert got_it
            # A later tenant reclaims the slot while we are still running.
            _isolated_lockfile.write_text(
                f"later-tenant  PID={os.getpid() + 1}  "
                f"since={datetime.now().isoformat()}  start=1.0",
                encoding="utf-8",
            )

        assert _isolated_lockfile.exists()
        assert "later-tenant" in _isolated_lockfile.read_text(encoding="utf-8")


# ── Concurrent acquire ──────────────────────────────────────────────────


class TestConcurrentAcquire:
    """Two routines starting in the same instant must not both win.

    This is the failure that let a Calibre and a Zotero watchdog index
    side by side on one 4 GB GPU: both OnLogon tasks fired in the same
    second, both found the slot empty, and both filled it.
    """

    def test_only_one_of_many_racers_acquires(
        self, _isolated_lockfile: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        # Widen the gap between deciding the slot is free and filling it.
        # An atomic create has no such gap; a check-then-write has one,
        # and this makes it big enough to hit reliably.
        real_lock_line = runtime_lock._lock_line

        def _slow_lock_line(script_name: str) -> str:
            time.sleep(0.05)
            return real_lock_line(script_name)

        monkeypatch.setattr(runtime_lock, "_lock_line", _slow_lock_line)

        racers = 6
        start = threading.Barrier(racers)
        results: list[bool] = []
        guard = threading.Lock()

        def _race(n: int) -> None:
            start.wait()
            got = runtime_lock.acquire(f"racer-{n}", wait_s=0)
            with guard:
                results.append(got)

        threads = [
            threading.Thread(target=_race, args=(n,)) for n in range(racers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sum(results) == 1, f"expected exactly one winner, got {results}"
        # The lockfile must name the winner and nobody else.
        content = _isolated_lockfile.read_text(encoding="utf-8")
        assert content.count("racer-") == 1, content

    def test_second_acquire_leaves_the_first_lockfile_untouched(
        self, _isolated_lockfile: Path,
    ):
        """A loser must not overwrite the winner's line."""
        assert runtime_lock.acquire("winner") is True
        before = _isolated_lockfile.read_text(encoding="utf-8")

        assert runtime_lock.acquire("loser", wait_s=0) is False

        assert _isolated_lockfile.read_text(encoding="utf-8") == before


class TestDiscardLockfile:
    def test_removes_the_lockfile_it_was_shown(self, _isolated_lockfile: Path):
        _isolated_lockfile.write_text("dead-holder", encoding="utf-8")
        runtime_lock._discard_lockfile("dead-holder")
        assert not _isolated_lockfile.exists()

    def test_keeps_a_lockfile_that_changed_meanwhile(
        self, _isolated_lockfile: Path,
    ):
        """Another acquirer may have cleared the dead lock and taken the
        slot between our judgement and our unlink — that fresh lock must
        survive, or we are back to two holders."""
        _isolated_lockfile.write_text("fresh-holder", encoding="utf-8")
        runtime_lock._discard_lockfile("dead-holder")
        assert _isolated_lockfile.exists()
        assert (
            _isolated_lockfile.read_text(encoding="utf-8") == "fresh-holder"
        )

    def test_no_lockfile_is_a_no_op(self, _isolated_lockfile: Path):
        runtime_lock._discard_lockfile("anything")
        assert not _isolated_lockfile.exists()
