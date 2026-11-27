"""Controlled fixtures disclose their real startup identity before doing work."""

from dataclasses import asdict
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep

from pydantic import TypeAdapter

from tests.support.process_observation import (
    CaptureCommand,
    IdentityResult,
    Observation,
    ParentEvidence,
    ProcessIdentity,
    ProcessObserver,
    SpawnEvidence,
    SystemError,
    WindowsProcesses,
)


def _parent_pid(system: WindowsProcesses, pid: int) -> tuple[int | None, SystemError | None]:
    """Read this held startup object's documented Toolhelp parent field."""
    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]
    kernel = system.kernel
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot is None or snapshot == ctypes.c_void_p(-1).value:
        return None, SystemError("CreateToolhelp32Snapshot", ctypes.get_last_error(), (("pid", pid),))
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            if entry.th32ProcessID == pid:
                return int(entry.th32ParentProcessID), None
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        return None, SystemError("Process32NextW.target_missing", ctypes.get_last_error(), (("pid", pid),))
    finally:
        system.close(int(snapshot))


def write_process_receipt(directory: Path, source: str) -> Path | None:
    """Wait until the caller holds the fixture's exact identity, or requests stop.

    MYRK_PROCESS_OWNER_FILE is the caller's complete original Observation JSON.
    A .json.ack sibling contains its complete public self Observation. Only an
    owned self with the exact same identity becomes the owner for later children.
    The unique normal-exit flag releases either startup wait without terminating
    anything; the fixture can then consume the product's pending shutdown.
    """
    directory.mkdir(parents=True, exist_ok=True)
    owner_file = Path(os.environ["MYRK_PROCESS_OWNER_FILE"])
    stop = Path(os.environ["MYRK_TEST_NORMAL_EXIT_FLAG"])
    deadline = monotonic() + 30
    while not owner_file.exists():
        if stop.exists():
            return None
        if monotonic() >= deadline:
            raise TimeoutError(f"Missing exact owner observation: {owner_file}")
        sleep(0.02)
    owner = TypeAdapter(Observation).validate_json(owner_file.read_bytes(), strict=True, extra="forbid")
    system = WindowsProcesses()
    owner_check = ProcessObserver(system)
    opened = system.open(os.getpid())
    handles = [opened.handle] if opened.handle is not None else []
    try:
        checked_owner = owner_check.capture(owner.pid, expected=owner.identity, direct_spawn=owner.direct_spawn)
        own = system.identity(opened.handle, os.getpid()) if opened.handle is not None else IdentityResult(
            ProcessIdentity(os.getpid(), None, None), (opened.error,) if opened.error is not None else (),
        )
        parent_pid = os.getppid()
        owner_matches = checked_owner.ownership == "owned" and checked_owner.state == "running"
        isolated_owner = (owner.ownership == "owned" and owner.identity is not None
                          and checked_owner.identity is None
                          and any(error.operation == "OpenProcess" and error.code == 5 for error in checked_owner.errors))
        owner_matches = owner_matches or isolated_owner
        errors = [*own.errors, *checked_owner.errors]
        nodes: list[tuple[ProcessIdentity, int | None, tuple[str, ...] | None, str | None]] = [
            (own.identity, parent_pid, tuple(sys.orig_argv), str(Path.cwd())),
        ]
        ancestor_pid: int | None = parent_pid
        for _ in range(6):
            if ancestor_pid is None or ancestor_pid == owner.pid:
                break
            parent_opened = system.open(ancestor_pid)
            parent_identity = ProcessIdentity(ancestor_pid, None, None)
            if parent_opened.handle is not None:
                handles.append(parent_opened.handle)
                parent_result = system.identity(parent_opened.handle, ancestor_pid)
                parent_identity = parent_result.identity
                errors.extend(parent_result.errors)
            elif parent_opened.error is not None:
                errors.append(parent_opened.error)
            actual_parent, parent_error = _parent_pid(system, ancestor_pid)
            if parent_error is not None:
                errors.append(parent_error)
            nodes.append((parent_identity, actual_parent, None, None))
            ancestor_pid = actual_parent
        anchored = owner_matches and ancestor_pid == owner.pid
        identities = {identity.pid: identity for identity, _, _, _ in nodes}
        if checked_owner.identity is not None or isolated_owner:
            identities[owner.pid] = checked_owner.identity or owner.identity
        records: list[CaptureCommand] = []
        for identity, reported_parent, argv, cwd in reversed(nodes):
            actual_parent_identity = identities.get(reported_parent) if reported_parent is not None else None
            records.append(CaptureCommand(
                "capture", identity.pid, expected=identity,
                direct_spawn=SpawnEvidence(identity, reported_parent, argv or (), cwd or "", "fixture_receipt") if anchored and reported_parent is not None else None,
                parent=ParentEvidence(actual_parent_identity, reported_parent) if actual_parent_identity is not None and reported_parent is not None else None,
            ))
        stamp = str(own.identity.created_filetime) if own.identity.created_filetime is not None else "unknown"
        path = directory / f"process-{os.getpid()}-{stamp}.json"
        temporary = path.with_suffix(".pending")
        source_path = Path(source).resolve()
        backend = sys.modules.get("myrk.desktop_service")
        backend_path = Path(backend.__file__).resolve() if backend is not None and backend.__file__ is not None else None
        temporary.write_text(json.dumps({
            "version": 1,
            "source": str(source_path),
            "source_sha256": sha256(source_path.read_bytes()).hexdigest(),
            "backend_source": str(backend_path) if backend_path is not None else None,
            "backend_sha256": sha256(backend_path.read_bytes()).hexdigest() if backend_path is not None else None,
            "interpreter_pid": os.getpid(),
            "reported_parent_pid": parent_pid,
            "owner_observation": TypeAdapter(Observation).dump_python(checked_owner, mode="json"),
            "reached_owner": ancestor_pid == owner.pid,
            "intermediate_limit": 6,
            "unfollowed_parent_pid": ancestor_pid if ancestor_pid != owner.pid else None,
            "launch_details": [{"pid": identity.pid, "reported_parent_pid": reported_parent,
                                "argv": argv, "cwd": cwd,
                                "source": "self_runtime" if argv is not None else "unknown"}
                               for identity, reported_parent, argv, cwd in nodes],
            "records": TypeAdapter(tuple[CaptureCommand, ...]).dump_python(tuple(records), mode="json"),
            "errors": [asdict(error) for error in errors],
        }, ensure_ascii=False), encoding="utf-8")
        if isolated_owner and ancestor_pid != owner.pid:
            request = path.with_suffix(".ancestry-request")
            temporary.replace(request)
            deadline = monotonic() + 10
            while not path.exists() and not stop.exists():
                if monotonic() >= deadline:
                    raise TimeoutError(f"External ancestry was not verified: {request}")
                sleep(.01)
        else:
            temporary.replace(path)
        acknowledgement = Path(str(path) + ".ack")
        deadline = monotonic() + 30
        while not acknowledgement.exists() and not stop.exists():
            if monotonic() >= deadline:
                raise TimeoutError(f"Fixture observation was not acknowledged: {path}")
            sleep(0.02)
        if acknowledgement.exists():
            acknowledged = TypeAdapter(Observation).validate_json(acknowledgement.read_bytes(), strict=True, extra="forbid")
            if acknowledged.pid == own.identity.pid and acknowledged.ownership == "owned" and acknowledged.identity == own.identity:
                os.environ["MYRK_PROCESS_OWNER_FILE"] = str(acknowledgement)
        return path
    finally:
        owner_check.close()
        for handle in handles:
            system.close(handle)
