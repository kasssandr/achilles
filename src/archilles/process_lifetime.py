"""Tie spawned children to the lifetime of this process (Windows).

A scheduled routine runs as a console window wrapping ``run_routine``,
which in turn spawns ``watchdog.py`` or ``batch_index.py`` as a child.
Closing that window terminates the wrapper outright: no ``finally``, no
``release()``, no chance to stop the child.  The child then keeps running
unsupervised — and because it is the process that actually loads the
embedding model, it keeps holding GPU memory that the routine lock
believes to be free.  A second routine starts, and two indexing runs
share a 4 GB card until an allocation fails.

Windows solves this with a job object: assign the child to a job whose
handle we hold, set ``KILL_ON_JOB_CLOSE``, and the kernel terminates the
child whenever this process goes away, however abruptly.  The handle must
stay open for the lifetime of the process, so it is kept in a module
global.

On other platforms this is a no-op that reports ``False``; the caller
keeps whatever cleanup it already does.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

#: The job handle, held open so the kernel keeps the job alive.  Closing
#: it — or exiting the process — terminates every assigned child.
_job_handle: int | None = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_kill_on_close_job() -> int | None:
    """Create the job object children get assigned to, or ``None``."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        logger.warning(
            "CreateJobObject failed (%s); children will outlive a hard kill",
            ctypes.get_last_error(),
        )
        return None

    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        handle,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        logger.warning(
            "SetInformationJobObject failed (%s); children will outlive a "
            "hard kill", ctypes.get_last_error(),
        )
        kernel32.CloseHandle(handle)
        return None
    return handle


def tie_child_to_parent(pid: int) -> bool:
    """Make the kernel terminate ``pid`` when this process goes away.

    Returns whether the child is now tied.  Every failure is reported
    rather than raised: losing the tie degrades cleanup, it must never
    take down the run that was about to start.
    """
    global _job_handle

    if sys.platform != "win32":
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    if _job_handle is None:
        _job_handle = _create_kill_on_close_job()
        if _job_handle is None:
            return False

    kernel32.OpenProcess.restype = wintypes.HANDLE
    child = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
    )
    if not child:
        logger.warning("OpenProcess(%s) failed (%s); child not tied to this "
                       "process", pid, ctypes.get_last_error())
        return False

    try:
        if not kernel32.AssignProcessToJobObject(_job_handle, child):
            logger.warning(
                "AssignProcessToJobObject(%s) failed (%s); child not tied to "
                "this process", pid, ctypes.get_last_error(),
            )
            return False
    finally:
        kernel32.CloseHandle(child)

    logger.debug("Child %s tied to this process' job object", pid)
    return True
