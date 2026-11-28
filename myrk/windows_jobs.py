"""Query, terminate, and wait for Windows jobs held by the caller."""

from __future__ import annotations

from collections.abc import Iterator
import ctypes
from ctypes import wintypes
from time import monotonic


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_longlong * 4),
        ("page_faults", wintypes.DWORD),
        ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD),
        ("terminated_processes", wintypes.DWORD),
    ]


def active_job_processes(kernel: ctypes.WinDLL, job: int) -> int:
    query = kernel.QueryInformationJobObject
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    accounting = _BasicAccounting()
    if not query(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(accounting.active_processes)


def terminate_job(kernel: ctypes.WinDLL, job: int) -> None:
    terminate = kernel.TerminateJobObject
    terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
    if not terminate(job, 1):
        raise ctypes.WinError(ctypes.get_last_error())


def job_exit_poll_delays(kernel: ctypes.WinDLL, job: int, *, timeout: float | None = None) -> Iterator[float]:
    """Yield the delay before each retry until the system reports an empty job."""
    deadline = monotonic() + timeout if timeout is not None else None
    while active := active_job_processes(kernel, job):
        if deadline is not None and monotonic() >= deadline:
            raise OSError(f"Owned check job still contains {active} processes.")
        yield 0.01
