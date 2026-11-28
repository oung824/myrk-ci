"""Read-only discovery and ownership verification for project-local runtime directories."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
import json
import os
from pathlib import Path
from typing import Any

from myrk.project_paths import (
    IndependentRuntimeProject, RuntimeDirectoryMismatch, project_root, require_project_path, require_runtime_project,
)


SUPERVISOR_RUNTIME_PREFIX = "myrk-run-"
RUNTIME_PREFIXES = ("b-", "p-", "run-", SUPERVISOR_RUNTIME_PREFIX)
LINKED_RUNTIME = "Linked runtime directory."


@dataclass(frozen=True)
class RuntimeScope:
    task_id: str | None
    scan_additional_runtime_roots: bool

    @classmethod
    def for_manifest(cls, task_id: str | None, *, project_audit: bool) -> "RuntimeScope":
        return cls(None if project_audit else task_id, project_audit or task_id is not None)


@dataclass(frozen=True)
class RuntimeVerification:
    directories: list[dict[str, Any]]
    cleanup_failures: list[dict[str, Any]]
    errors: list[str]


@dataclass(frozen=True)
class _AuditCheckout:
    project: Path
    source: Path


def verify_runtime_ownership(
    project: Path, source: Path, *,
    scope: RuntimeScope, registered_worktrees: set[Path],
    listed_paths: set[Path], listed_worktrees: set[Path],
) -> RuntimeVerification:
    """Verify runtime resources in task, project audit, or legacy manifest scope."""
    task = scope.task_id
    sources = {project, source}
    sources.update(location for location in listed_worktrees if location in registered_worktrees)
    if scope.scan_additional_runtime_roots:
        sources.update(location for location in registered_worktrees if location.is_relative_to(project))

    checkout = _AuditCheckout(project, source)
    listed = listed_paths | listed_worktrees
    directories = _discover_resources(
        checkout, listed, task, sources, scan_custom_roots=scope.scan_additional_runtime_roots,
    )
    errors = []
    for resource in directories:
        if (not resource["listed"] and resource["ownership"] != "external"
                and (task is not None or resource["state"] != "active")):
            detail = " Runtime ownership is unresolved." if resource["ownership"] == "unresolved" else ""
            errors.append(f"Unlisted runtime resource ({resource['state']}): {resource['path']}.{detail} {resource['reason']}")
    failures, cleanup_errors = _cleanup_failures(checkout, listed, task)
    errors.extend(cleanup_errors)
    return RuntimeVerification(directories, failures, errors)


@dataclass(frozen=True)
class _OwnerProcess:
    pid: int | None
    has_filetime: bool
    filetime: str | None
    timestamp_text: str | None
    exact_start: bool

    @cached_property
    def timestamp(self) -> datetime | None:
        """Parse a legacy timestamp only after the caller needs creation time."""
        if self.timestamp_text is None:
            return None
        return datetime.fromisoformat(self.timestamp_text.replace("Z", "+00:00"))


def _read_owner_process(owner: dict[str, Any]) -> _OwnerProcess:
    raw_pid = owner.get("pid")
    pid = raw_pid if type(raw_pid) is int and 0 < raw_pid <= 0xFFFFFFFF else None
    expected = owner.get("created_filetime")
    if expected is not None:
        filetime = expected if isinstance(expected, str) and expected.isdecimal() else None
        return _OwnerProcess(pid, True, filetime, None, False)
    started = owner.get("process_started_at")
    recorded = started or owner.get("started_at") or owner.get("created_at")
    return _OwnerProcess(pid, False, None, recorded if isinstance(recorded, str) else None, bool(started))


def owner_state(owner: dict[str, Any]) -> tuple[str, str]:
    """Observe a recorded owner without acquiring process termination rights."""
    return _owner_state(_read_owner_process(owner))


def _owner_state(process: _OwnerProcess) -> tuple[str, str]:
    pid = process.pid
    if pid is None:
        return "unknown", "The runtime owner has no valid process ID."
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x101000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        return ("exited", "The recorded owner process no longer exists.") if code == 87 else ("unknown", f"Cannot inspect owner process {pid}: Windows error {code}.")
    try:
        waited = kernel.WaitForSingleObject(handle, 0)
        if waited == 0:
            return "exited", "The recorded owner process has exited."
        if waited != 258:
            return "unknown", f"Cannot confirm owner process {pid} state: Windows error {ctypes.get_last_error()}."
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return "unknown", f"Cannot read owner process {pid} creation time."
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        if process.has_filetime:
            expected = process.filetime
            if expected is None:
                return "unknown", "The owner creation time is malformed."
            return ("active", "The recorded process identity is still active.") if str(created) == expected else ("exited", "The owner exited and its process ID has been reused.")
        recorded_time = process.timestamp
        if recorded_time is None:
            return "unknown", "The runtime owner has no creation or ownership timestamp."
        if recorded_time.tzinfo is None:
            return "unknown", "The owner timestamp has no timezone."
        created_epoch = created / 10_000_000 - 11_644_473_600
        if process.exact_start:
            matches = abs(created_epoch - recorded_time.timestamp()) <= 2
        else:
            matches = created_epoch <= recorded_time.timestamp()
        return ("active", "The live process creation time matches the ownership record.") if matches else ("exited", "The owner exited and its process ID has been reused.")
    finally:
        kernel.CloseHandle(handle)


@dataclass(frozen=True)
class _RuntimeOwner:
    record: dict[str, Any]
    process: _OwnerProcess
    source_verified_by_git: bool


def _runtime_owner(
    checkout: _AuditCheckout, document: object, location: Path, *, check_purpose: bool = False,
) -> _RuntimeOwner:
    if not isinstance(document, dict):
        raise ValueError("Expected a JSON owner record.")
    require_project_path(checkout.project, document.get("runtime_directory"), expected_runtime=location)
    if check_purpose and document.get("purpose") != "supervisor-runtime":
        raise ValueError("Invalid runtime purpose.")
    source_verified_by_git = require_runtime_project(
        checkout.project, checkout.source, location, document.get("project_root"), document.get("source_root"),
    )
    return _RuntimeOwner(document, _read_owner_process(document), source_verified_by_git)


def _recorded_task(project: Path, owner: _RuntimeOwner) -> str | None:
    task = owner.record.get("task_id")
    process = owner.process
    if (not isinstance(task, str) or not task or task != task.strip()
            or not Path(owner.record["runtime_directory"]).is_absolute()
            or process.pid is None):
        return None
    if not owner.source_verified_by_git:
        source = owner.record.get("source_root")
        if not isinstance(source, str) or not Path(source).is_absolute() or not Path(owner.record["project_root"]).is_absolute():
            return None
        source_path = require_project_path(project, source)
        if source_path.is_file() or project_root(source_path) != project:
            return None
    if process.has_filetime:
        filetime = process.filetime
        return task if filetime is not None and filetime.isascii() and int(filetime) > 0 else None
    timestamp = process.timestamp
    return task if timestamp is not None and timestamp.utcoffset() is not None else None


def _ownership(listed: bool, owner_task: str | None, task: str | None) -> str:
    if task is None:
        return "project"
    if listed:
        return "listed"
    if owner_task is None:
        return "unresolved"
    return "task" if owner_task == task else "external"


def _discover_custom_runtimes(
    checkout: _AuditCheckout, discovered: dict[Path, dict[str, Any]], listed: set[Path], task: str | None,
    configured_root: Path | None,
) -> None:
    covered = {os.path.normcase(str(path)) for path in discovered}
    pending = [str(checkout.project)]
    while pending:
        directory = pending.pop()
        in_configured_root = Path(directory) == configured_root
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.name.lower() == ".git" or os.path.normcase(entry.path) in covered:
                            continue
                        candidate = entry.name.startswith(SUPERVISOR_RUNTIME_PREFIX)
                        if in_configured_root and candidate and (entry.is_symlink() or entry.is_junction()):
                            path = Path(entry.path)
                            discovered[path] = _runtime_entry(path, False, task, LINKED_RUNTIME)
                            continue
                        if not entry.is_dir(follow_symlinks=False) or entry.is_junction():
                            continue
                    except OSError:
                        continue
                    if candidate:
                        location = Path(entry.path)
                        row = _inspect_runtime(
                            checkout, location, location in listed, task, configured_root, custom_scan=True,
                        )
                        if row is not None:
                            discovered[location] = row
                            continue
                    pending.append(entry.path)
        except OSError as error:
            if (configured_root is not None and configured_root.is_relative_to(directory)
                    and not isinstance(error, FileNotFoundError)):
                discovered[configured_root] = _runtime_entry(configured_root, False, task, str(error))


def _runtime_entry(location: Path, listed: bool, task: str | None, reason: str = "") -> dict[str, Any]:
    return {"path": str(location), "listed": listed, "state": "unknown",
            "ownership": _ownership(listed, None, task), "reason": reason}


def _inspect_runtime(
    checkout: _AuditCheckout, location: Path, listed: bool, task: str | None, configured_root: Path | None,
    *, custom_scan: bool = False,
) -> dict[str, Any] | None:
    entry = _runtime_entry(location, listed, task)
    configured_supervisor = location.parent == configured_root and location.name.startswith(SUPERVISOR_RUNTIME_PREFIX)
    try:
        marker = require_project_path(checkout.project, str(location / "owner.json"))
        owner = _runtime_owner(
            checkout, json.loads(marker.read_text(encoding="utf-8")), location,
            check_purpose=custom_scan or configured_supervisor,
        )
    except IndependentRuntimeProject:
        return None
    except (OSError, ValueError) as error:
        if custom_scan and not configured_supervisor and isinstance(error, (RuntimeDirectoryMismatch, FileNotFoundError)):
            return None
        entry["reason"] = str(error)
        return entry
    record = owner.record
    entry.update(owner=record.get("owner") or record.get("purpose"), pid=record.get("pid"))
    try:
        task_id = _recorded_task(checkout.project, owner) if task is not None else record.get("task_id")
        if task is not None or isinstance(task_id, str):
            entry["task_id"] = task_id
        entry["ownership"] = _ownership(listed, task_id, task)
        if entry["ownership"] == "external":
            entry.update(state="not_checked", reason="Runtime ownership is recorded for another task.")
        else:
            entry["state"], entry["reason"] = _owner_state(owner.process)
    except (OSError, ValueError) as error:
        entry["reason"] = str(error)
    return entry


def _discover_resources(
    checkout: _AuditCheckout, listed: set[Path], task: str | None, sources: set[Path], *,
    scan_custom_roots: bool,
) -> list[dict[str, Any]]:
    discovered: dict[Path, dict[str, Any]] = {}
    project = checkout.project
    roots = {base / suffix for base in sources for suffix in (".scratch/runtime", ".scratch/check-runs/runtime-tmp")}
    config = Path(os.getenv("MYRK_RUNTIME_ROOT", "")) if scan_custom_roots else Path()
    configured_root = path if config.is_absolute() and (path := config.resolve()).is_relative_to(project) else None

    def scan_error(error: OSError) -> None:
        raise error

    for root in sorted(roots):
        try:
            require_project_path(project, str(root))
            if not root.exists():
                continue
            for current, directories, files in os.walk(root, onerror=scan_error, followlinks=False):
                location = Path(current)
                linked = {name for name in directories if (location / name).is_symlink() or (location / name).is_junction()}
                directories[:] = [name for name in directories if name not in linked]
                if location == root:
                    for name in sorted(linked.union(files)):
                        path = location / name
                        if name.startswith(RUNTIME_PREFIXES) and (name in linked or path.is_symlink() or path.is_junction()):
                            discovered[path] = _runtime_entry(path, False, task, LINKED_RUNTIME)
                marker = location / "owner.json"
                candidate = os.path.lexists(marker) or (location.parent == root and location.name.startswith(RUNTIME_PREFIXES))
                if not candidate or location in discovered:
                    continue
                directories.clear()
                if entry := _inspect_runtime(checkout, location, location in listed, task, configured_root):
                    discovered[location] = entry
        except (OSError, ValueError) as error:
            discovered[root] = _runtime_entry(root, False, task, str(error))
    if scan_custom_roots:
        _discover_custom_runtimes(checkout, discovered, listed, task, configured_root)
    return list(discovered.values())


def _same_json_value(left: object, right: object) -> bool:
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if left is right:
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((value, right[key]) for key, value in left.items())
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif isinstance(left, bool) != isinstance(right, bool) or left != right:
            return False
    return True


def _cleanup_failures(
    checkout: _AuditCheckout, listed: set[Path], task: str | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[dict[str, Any]] = []
    errors: list[str] = []
    project = checkout.project
    retry = "After the owning process has exited, verify its ordinary runtime directory is no longer locked, remove it, and clear the receipt only after verifying removal."
    root = require_project_path(project, ".scratch/runtime/cleanup-failures")
    if root.exists() and not root.is_dir():
        raise ValueError(f"Runtime cleanup receipt root is not a directory: {root}")
    for receipt in sorted(root.glob("*.json")):
        entry: dict[str, Any] = {"path": str(receipt), "delete_when": retry, "ownership": _ownership(receipt in listed, None, task)}
        failures.append(entry)
        try:
            require_project_path(project, str(receipt))
            document = json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("The runtime cleanup receipt must be an object.")
            location = require_project_path(project, document.get("runtime_directory"))
            if (receipt.stem != location.name or not Path(document["runtime_directory"]).is_absolute()
                    or location.exists() and not location.is_dir()):
                raise ValueError("The runtime cleanup receipt does not match its directory.")
            owner = _runtime_owner(checkout, document.get("owner"), location)
            marker = location / "owner.json"
            if os.path.lexists(marker):
                current_marker = require_project_path(project, str(marker))
                current = _runtime_owner(
                    checkout, json.loads(current_marker.read_text(encoding="utf-8")), location,
                )
                if not _same_json_value(current.record, owner.record):
                    raise ValueError("The runtime cleanup receipt conflicts with the current owner record.")
            entry.update(runtime_directory=str(location), task_id=_recorded_task(project, owner))
            entry["ownership"] = _ownership(location in listed or receipt in listed, entry["task_id"], task)
        except (OSError, ValueError) as error:
            entry["ownership_error"] = str(error)
        if entry["ownership"] != "external":
            errors.append(f"Runtime cleanup failure receipt remains: {receipt}. Retry: {retry}")
    return failures, errors
