"""Read-only Windows process observation for acceptance callers.

Importing this module does not open processes, create files, or start checks.
"""

from dataclasses import dataclass, replace
import ctypes
from ctypes import wintypes
import json
import sys
from time import monotonic
from typing import Annotated, Literal, Protocol

from pydantic import BeforeValidator, ConfigDict, Field, PlainSerializer, TypeAdapter


def _parse_filetime(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("created_filetime must be a decimal string, never a JSON number")
    return int(value)


FileTime = Annotated[int, BeforeValidator(_parse_filetime), PlainSerializer(str, return_type=str)]
ProcessState = Literal["running", "exited", "unknown"]
Ownership = Literal["owned", "excluded", "unknown"]


@dataclass(frozen=True)
class SystemError:
    operation: str
    code: int
    parameters: tuple[tuple[str, int | bool], ...] = ()


@dataclass(frozen=True)
class OpenResult:
    handle: int | None
    error: SystemError | None = None


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created_filetime: FileTime | None
    image: str | None


@dataclass(frozen=True)
class SpawnEvidence:
    """A caller-held spawn or controlled fixture receipt, never a PID guess."""
    identity: ProcessIdentity
    launcher_pid: int
    argv: tuple[str, ...]
    cwd: str
    source: Literal["caller_spawn", "fixture_receipt"]


@dataclass(frozen=True)
class ParentEvidence:
    identity: ProcessIdentity
    reported_parent_pid: int


@dataclass(frozen=True)
class IdentityResult:
    identity: ProcessIdentity
    errors: tuple[SystemError, ...] = ()


@dataclass(frozen=True)
class WaitResult:
    completed: bool | None
    error: SystemError | None = None


@dataclass(frozen=True)
class ExitResult:
    code: int | None
    error: SystemError | None = None


class ProcessSystem(Protocol):
    def open(self, pid: int) -> OpenResult: ...
    def identity(self, handle: int, pid: int) -> IdentityResult: ...
    def wait(self, handle: int, timeout_ms: int) -> WaitResult: ...
    def exit_code(self, handle: int) -> ExitResult: ...
    def close(self, handle: int) -> None: ...


@dataclass(frozen=True)
class Observation:
    pid: int
    state: ProcessState
    ownership: Ownership = "unknown"
    reasons: tuple[str, ...] = ()
    errors: tuple[SystemError, ...] = ()
    identity: ProcessIdentity | None = None
    completed: bool | None = None
    exit_code: int | None = None
    expected: ProcessIdentity | None = None
    direct_spawn: SpawnEvidence | None = None
    parent: ParentEvidence | None = None


def _same_identity(first: ProcessIdentity, second: ProcessIdentity) -> bool:
    return (first.pid == second.pid and first.created_filetime is not None
            and first.created_filetime == second.created_filetime
            and first.image is not None and second.image is not None
            and first.image.casefold() == second.image.casefold())


class ProcessObserver:
    def __init__(self, system: ProcessSystem | None = None) -> None:
        self.system = system if system is not None else WindowsProcesses()
        self.observations: dict[int, Observation] = {}
        self.handles: dict[int, int] = {}

    def capture(self, pid: int, *, expected: ProcessIdentity | None = None,
                direct_spawn: SpawnEvidence | None = None,
                parent: ParentEvidence | None = None) -> Observation:
        if pid in self.observations:
            previous = self.observations[pid]
            self.observations[pid] = replace(previous, expected=expected or previous.expected,
                                                 direct_spawn=direct_spawn or previous.direct_spawn,
                                                 parent=parent or previous.parent)
            return self._observe(pid, 0)
        opened = self.system.open(pid)
        if opened.handle is None:
            self.observations[pid] = Observation(pid, "unknown", reasons=("open_failed",),
                                                 errors=(opened.error,) if opened.error else (),
                                                 expected=expected, direct_spawn=direct_spawn, parent=parent)
            return self.observations[pid]
        self.handles[pid] = opened.handle
        identity = self.system.identity(opened.handle, pid)
        self.observations[pid] = Observation(pid, "unknown", identity=identity.identity, errors=identity.errors,
                                             expected=expected, direct_spawn=direct_spawn, parent=parent)
        return self._observe(pid, 0)

    def _observe(self, pid: int, timeout_ms: int) -> Observation:
        result = self.observations[pid]
        if pid not in self.handles:
            return result
        wait = self.system.wait(self.handles[pid], timeout_ms)
        code = self.system.exit_code(self.handles[pid])
        errors = tuple(dict.fromkeys((*result.errors, *(error for error in (wait.error, code.error) if error))))
        state: ProcessState
        reasons: tuple[str, ...]
        if result.identity is None or result.identity.created_filetime is None or result.identity.image is None:
            state, reasons = "unknown", ("identity_incomplete",)
        elif wait.completed is None or code.code is None:
            state, reasons = "unknown", ("status_query_failed",)
        elif wait.completed:
            state, reasons = "exited", ()
        else:
            state, reasons = "running", ()
        ownership: Ownership = "unknown"
        if result.direct_spawn is not None and result.identity is not None and _same_identity(result.identity, result.direct_spawn.identity):
            ownership = "owned"
        expected = result.expected or (result.direct_spawn.identity if result.direct_spawn else None)
        if (expected is not None and result.identity is not None
                and result.identity.created_filetime is not None and expected.created_filetime is not None
                and result.identity.image is not None and expected.image is not None
                and not _same_identity(result.identity, expected)):
            ownership, reasons = "excluded", (*reasons, "identity_mismatch")
        if result.parent is not None and result.identity is not None:
            parent_time, child_time = result.parent.identity.created_filetime, result.identity.created_filetime
            if parent_time is not None and child_time is not None and child_time < parent_time:
                ownership, reasons = "excluded", (*reasons, "child_predates_exact_parent")
        result = replace(result, state=state, ownership=ownership, reasons=reasons, completed=wait.completed,
                         exit_code=code.code, errors=errors)
        self.observations[pid] = result
        return result

    def status(self) -> tuple[Observation, ...]:
        return tuple(self._observe(pid, 0) for pid in self.observations)

    def wait(self, timeout_ms: int) -> tuple[Observation, ...]:
        if not 0 <= timeout_ms <= 60000:
            raise ValueError("wait timeout_ms must be between 0 and 60000")
        deadline = monotonic() + timeout_ms / 1000
        return tuple(self._observe(pid, max(0, int((deadline - monotonic()) * 1000)))
                     for pid in self.observations)

    def close(self) -> None:
        for handle in self.handles.values():
            self.system.close(handle)
        self.handles.clear()


class WindowsProcesses:
    """The only operating-system boundary; deliberately has no termination API."""
    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def open(self, pid: int) -> OpenResult:
        parameters = (("access", 0x101000), ("inherit", False), ("pid", pid))
        if not 0 <= pid <= 0xFFFFFFFF:
            return OpenResult(None, SystemError("OpenProcess.invalid_pid", 87, parameters))
        handle = self.kernel.OpenProcess(0x101000, False, pid)
        if not handle:
            return OpenResult(None, SystemError("OpenProcess", ctypes.get_last_error(), parameters))
        return OpenResult(int(handle))

    def identity(self, handle: int, pid: int) -> IdentityResult:
        times = [wintypes.FILETIME() for _ in range(4)]
        errors = []
        created = None
        if self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        else:
            errors.append(SystemError("GetProcessTimes", ctypes.get_last_error()))
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        image = None
        if self.kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            image = name.value
        else:
            errors.append(SystemError("QueryFullProcessImageNameW", ctypes.get_last_error()))
        return IdentityResult(ProcessIdentity(pid, created, image), tuple(errors))

    def wait(self, handle: int, timeout_ms: int) -> WaitResult:
        code = self.kernel.WaitForSingleObject(handle, timeout_ms)
        if code == 0:
            return WaitResult(True)
        if code == 258:
            return WaitResult(False)
        return WaitResult(None, SystemError("WaitForSingleObject", ctypes.get_last_error(),
                                            (("timeout_ms", timeout_ms), ("result", code))))

    def exit_code(self, handle: int) -> ExitResult:
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return ExitResult(None, SystemError("GetExitCodeProcess", ctypes.get_last_error()))
        return ExitResult(int(code.value))

    def close(self, handle: int) -> None:
        if not self.kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


@dataclass(frozen=True)
class CaptureCommand:
    action: Literal["capture"]
    pid: int
    expected: ProcessIdentity | None = None
    direct_spawn: SpawnEvidence | None = None
    parent: ParentEvidence | None = None


@dataclass(frozen=True)
class StatusCommand:
    action: Literal["status", "close"]


@dataclass(frozen=True)
class WaitCommand:
    action: Literal["wait"]
    timeout_ms: int


def serve() -> None:
    """Consume one JSON command per line; output complete unfiltered reports.

    A direct_spawn value asserts a *real* caller-held spawn or controlled fixture
    receipt. Command/path fields alone are not ownership evidence. Callers must
    capture while the directly launched process is still alive, checking their
    spawn object before and after capture. Never manufacture a receipt from a
    PID/path scan. This local check protocol is not an untrusted-client service.
    """
    commands = TypeAdapter(Annotated[CaptureCommand | StatusCommand | WaitCommand,
                                     Field(discriminator="action")],
                           config=ConfigDict(extra="forbid", strict=True))
    reports = TypeAdapter(tuple[Observation, ...])
    observer = ProcessObserver()
    try:
        for line in sys.stdin:
            command = commands.validate_json(line)
            if isinstance(command, CaptureCommand):
                observer.capture(command.pid, expected=command.expected,
                                 direct_spawn=command.direct_spawn, parent=command.parent)
            if isinstance(command, WaitCommand):
                processes = observer.wait(command.timeout_ms)
            else:
                processes = observer.status()
            print(json.dumps({"processes": reports.dump_python(processes, mode="json"),
                              "complete": bool(processes) and all(item.ownership == "excluded" or item.state == "exited" for item in processes)},
                             ensure_ascii=False), flush=True)
            if command.action == "close":
                break
    finally:
        observer.close()


if __name__ == "__main__":
    serve()
