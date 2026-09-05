"""Tests for ``src.archilles.process_lifetime``.

The behaviour under test is the one that let an orphaned indexing run
keep a GPU busy: closing a console window kills ``run_routine`` outright,
without running its ``finally``, while the ``watchdog.py`` child it
spawned carried on holding VRAM.  Tying the child to the parent makes the
kernel clean it up.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest

from src.archilles import process_lifetime

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="job objects are a Windows mechanism"
)


def _pid_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except psutil.Error:  # pragma: no cover - race with process teardown
        return False


class TestTieChildToParent:
    def test_child_dies_when_parent_is_killed_hard(self):
        """A hard-killed parent must not leave its child running."""
        parent_src = textwrap.dedent(
            """
            import subprocess, sys, time
            sys.path.insert(0, %r)
            from src.archilles import process_lifetime

            child = subprocess.Popen([sys.executable, "-c",
                                      "import time; time.sleep(120)"])
            process_lifetime.tie_child_to_parent(child.pid)
            print(child.pid, flush=True)
            time.sleep(120)
            """
        ) % str(process_lifetime.__file__).rsplit("src", 1)[0]

        parent = subprocess.Popen(
            [sys.executable, "-c", parent_src],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            child_pid = int(parent.stdout.readline().strip())
            assert _pid_alive(child_pid)

            parent.kill()  # TerminateProcess — no finally, no cleanup
            parent.wait(timeout=10)

            deadline = time.time() + 10
            while time.time() < deadline and _pid_alive(child_pid):
                time.sleep(0.2)
            assert not _pid_alive(child_pid), (
                f"child {child_pid} survived its killed parent"
            )
        finally:
            if parent.poll() is None:  # pragma: no cover - cleanup path
                parent.kill()

    def test_reports_failure_for_a_dead_pid(self):
        """An unopenable PID reports False rather than raising."""
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert process_lifetime.tie_child_to_parent(proc.pid) is False

    def test_survives_being_called_twice(self):
        """The job is created once; a second child joins the same job."""
        procs = [
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            for _ in range(2)
        ]
        try:
            assert all(process_lifetime.tie_child_to_parent(p.pid) for p in procs)
        finally:
            for p in procs:
                p.kill()
                p.wait(timeout=10)
