"""
Global routine-lock for Archilles automations.

A single file-based mutex at ``~/.archilles/routine.lock`` serialises any
process that needs exclusive access to the shared GPU / LanceDB / Calibre
SQLite resources — scheduled watchdogs, vault-linker, status-mail, and
future tenants like the news agent.

Design
------
* **Lockfile content:** a short human-readable line ``"{script_name}  PID=…
  since=ISO"``.  Reading it shows you which automation currently holds the
  lock and since when.
* **Heartbeat:** a daemon thread refreshes the lockfile's mtime every
  :data:`HEARTBEAT_INTERVAL_S` seconds via ``os.utime`` (atomic — never
  recreates a stray file if the lock was just released).
* **Stale recovery:** if the mtime has not been refreshed within
  :data:`STALE_AFTER_S` (1 h), the next acquirer reclaims the lock.  This
  covers the case where a previous holder crashed without releasing.
* **Liveness recovery:** waiting out a full hour is wasteful when the
  holder is *provably* gone, so :func:`acquire` additionally reclaims a
  lock whose recorded holder cannot exist any more — its mtime predates
  the last boot (hard reboot), or its PID is dead, or the PID was
  recycled by a process that started after the lock was written.  This
  check may only release a lock *earlier* than the mtime rule would, and
  never holds one longer: a hung-but-alive holder still loses its lock
  after :data:`STALE_AFTER_S`.
* **Wait-and-poll:** acquirers pass ``wait_s`` to wait for a busy lock to
  free up.  Scheduled tasks pass 2 h so OnLogon triggers serialise rather
  than skip the day.

Usage
-----
High-level (recommended): the context manager handles heartbeat lifecycle.

.. code-block:: python

    from archilles.runtime_lock import routine_lock

    with routine_lock("news-agent(heavy)", wait_s=1800) as acquired:
        if not acquired:
            sys.exit(1)
        do_gpu_work()

Low-level (for legacy try/finally patterns):

.. code-block:: python

    import threading
    from archilles import runtime_lock

    if not runtime_lock.acquire("script-name", wait_s=7200):
        return 1
    stop = threading.Event()
    runtime_lock.start_heartbeat(stop)
    try:
        do_work()
    finally:
        stop.set()
        runtime_lock.release()
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

try:  # optional — the liveness check degrades gracefully without it
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ── Public constants ────────────────────────────────────────────────────

#: Path to the shared lockfile.  Override at import time if you need a
#: different location for testing or per-user isolation.
LOCK_FILE: Path = Path.home() / ".archilles" / "routine.lock"

#: How often a live holder refreshes the lockfile mtime.
HEARTBEAT_INTERVAL_S: int = 300

#: How long the lockfile mtime may stay unrefreshed before the next
#: acquirer treats it as stale and reclaims it.  Must be larger than
#: :data:`HEARTBEAT_INTERVAL_S` by a comfortable margin so a delayed
#: heartbeat doesn't trigger false stealing.  1 h is also large enough to
#: tolerate occasional non-heartbeating tenants (legacy short-lived
#: scripts that haven't been migrated yet).
STALE_AFTER_S: int = 3600

#: How often :func:`acquire` re-checks a busy lock while waiting.
_POLL_INTERVAL_S: int = 300

#: Slack allowed when comparing timestamps against the boot time or a
#: process start time.  Absorbs clock jitter and the coarse resolution of
#: the stdlib boot-time fallback, so we never declare a live holder dead.
_CLOCK_MARGIN_S: int = 120

_PID_RE = re.compile(r"PID=(\d+)")
_SINCE_RE = re.compile(r"since=(\S+)")


# ── Holder liveness ─────────────────────────────────────────────────────


def _boot_time() -> float:
    """Wall-clock timestamp of the last system boot.

    Uses ``psutil`` when available and otherwise falls back to wall clock
    minus monotonic uptime, which is coarser but good enough given
    :data:`_CLOCK_MARGIN_S`.
    """
    if psutil is not None:
        try:
            return float(psutil.boot_time())
        except Exception:  # pragma: no cover - platform quirks
            pass
    return time.time() - time.monotonic()


def _pid_is_alive(pid: int) -> bool:
    """Whether a process with ``pid`` currently exists.

    Returns ``True`` when we cannot tell — an unknown holder must never be
    treated as dead.
    """
    if psutil is None:
        return True
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # pragma: no cover - platform quirks
        return True


def _pid_start_time(pid: int) -> float | None:
    """Creation timestamp of ``pid``, or ``None`` if it cannot be read."""
    if psutil is None:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _parse_since(info: str) -> float | None:
    """Timestamp from the lockfile's ``since=`` field, if parseable."""
    m = _SINCE_RE.search(info)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1)).timestamp()
    except ValueError:
        return None


def _holder_is_gone(info: str, mtime: float) -> bool:
    """Whether the lockfile's recorded holder provably no longer exists.

    Deliberately conservative: every uncertain case answers ``False`` so
    that a live holder is never robbed of its lock.  A ``True`` here only
    ever *shortens* the wait that :data:`STALE_AFTER_S` would impose.
    """
    # A hard reboot kills every holder.  The heartbeat only ticks while the
    # holder runs, so an mtime older than the boot proves the holder died
    # with the machine.
    if mtime < _boot_time() - _CLOCK_MARGIN_S:
        return True

    m = _PID_RE.search(info)
    if not m:
        return False  # unknown holder — assume it is alive
    pid = int(m.group(1))

    if not _pid_is_alive(pid):
        return True  # crashed without releasing, no reboot involved

    # The PID exists, but PIDs get recycled: a process that started after
    # the lock was written cannot be the holder we are looking at.
    since = _parse_since(info)
    started = _pid_start_time(pid)
    if since is not None and started is not None:
        if started > since + _CLOCK_MARGIN_S:
            return True

    return False


# ── Low-level API ───────────────────────────────────────────────────────


def acquire(script_name: str, wait_s: int = 0) -> bool:
    """Try to acquire the global routine lock.

    Parameters
    ----------
    script_name
        Short identifier written into the lockfile so other processes can
        see who holds it (e.g. ``"run_routine(archilles)"``).
    wait_s
        How long to wait for a busy lock to free up.  ``0`` (default)
        means "fail fast"; positive values poll every
        :data:`_POLL_INTERVAL_S` seconds until the timeout elapses.

    Returns
    -------
    bool
        ``True`` if the lock was acquired; ``False`` if ``wait_s`` ran
        out while the lock stayed busy.

    Notes
    -----
    A returned ``True`` does *not* start the heartbeat — callers must
    either use :func:`start_heartbeat` directly (low-level) or, better,
    wrap the work in :func:`routine_lock` (high-level).  A long-running
    holder without heartbeat risks having its lock reclaimed after
    :data:`STALE_AFTER_S`.
    """
    deadline = time.time() + wait_s
    info = ""
    while True:
        busy = False
        if LOCK_FILE.exists():
            try:
                mtime = LOCK_FILE.stat().st_mtime
                content = LOCK_FILE.read_text(encoding="utf-8").strip()
            except OSError:
                # Vanished between exists() and the read — treat as free.
                pass
            else:
                if time.time() - mtime < STALE_AFTER_S:
                    if _holder_is_gone(content, mtime):
                        logger.warning(
                            "Reclaiming routine lock — holder is gone: %s",
                            content,
                        )
                        print(
                            f"  Reclaiming stale lock (holder gone): {content}",
                            file=sys.stderr,
                        )
                    else:
                        busy = True
                        info = content

        if not busy:
            LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
            LOCK_FILE.write_text(
                f"{script_name}  PID={os.getpid()}  since={datetime.now().isoformat()}",
                encoding="utf-8",
            )
            return True

        if time.time() >= deadline:
            logger.warning(
                "Routine lock still held after %ss wait: %s", wait_s, info
            )
            print(
                f"SKIP — routine lock still held after {wait_s}s wait: {info}",
                file=sys.stderr,
            )
            return False

        remaining = int(deadline - time.time())
        logger.info("Waiting for routine lock (%ss left): %s", remaining, info)
        print(
            f"  Waiting for lock ({remaining}s left): {info}", file=sys.stderr
        )
        time.sleep(_POLL_INTERVAL_S)


def release() -> None:
    """Remove the lockfile.  Safe to call when the lock is not held."""
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def start_heartbeat(stop_event: threading.Event) -> threading.Thread:
    """Refresh the lockfile mtime every :data:`HEARTBEAT_INTERVAL_S` seconds.

    The returned thread is a daemon — it does not need to be joined, and
    it terminates when the program exits.  Set ``stop_event`` when the
    caller wants to stop refreshing early (e.g. just before
    :func:`release`).

    Uses ``os.utime`` rather than ``Path.touch`` so a heartbeat tick that
    races with :func:`release` cannot resurrect a zombie lockfile.
    """

    def _beat() -> None:
        while not stop_event.wait(HEARTBEAT_INTERVAL_S):
            try:
                os.utime(LOCK_FILE, None)
            except OSError:
                # FileNotFoundError when the lock was already released —
                # nothing to do, the heartbeat will simply tick again.
                pass

    t = threading.Thread(target=_beat, daemon=True, name="routine-lock-heartbeat")
    t.start()
    return t


# ── High-level context manager ──────────────────────────────────────────


@contextmanager
def routine_lock(script_name: str, *, wait_s: int = 0) -> Iterator[bool]:
    """Acquire the lock for the duration of a ``with`` block.

    Yields ``True`` if the lock was acquired (heartbeat thread is then
    running) or ``False`` if ``wait_s`` ran out.  In both cases the
    block executes; check the yielded value to decide whether to do
    work or bail out early:

    .. code-block:: python

        with routine_lock("my-script", wait_s=600) as got_it:
            if not got_it:
                return  # busy — try again later
            do_protected_work()

    On exit (normal or via exception), the heartbeat is stopped and the
    lockfile removed.
    """
    acquired = acquire(script_name, wait_s=wait_s)
    stop = threading.Event()
    if acquired:
        start_heartbeat(stop)
    try:
        yield acquired
    finally:
        stop.set()
        if acquired:
            release()
