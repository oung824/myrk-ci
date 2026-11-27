"""Read-only verification of a task's project-local resource manifest."""

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE_ROOT))
from myrk.project_paths import project_root

ROOT = project_root(SOURCE_ROOT)


def project_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A resource path must be a non-empty string.")
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    for location in (path, *path.parents):
        if location.is_symlink() or location.is_junction():
            raise ValueError(f"Linked resource or ancestor is not accepted: {location}")
    path = path.resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"Resource is outside the project: {path}")
    return path


def owner_state(owner: dict[str, Any]) -> tuple[str, str]:
    """Observe a recorded owner without acquiring process termination rights."""
    pid = owner.get("pid")
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
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
        expected = owner.get("created_filetime")
        if expected is not None:
            if not isinstance(expected, str) or not expected.isdecimal():
                return "unknown", "The owner creation time is malformed."
            return ("active", "The recorded process identity is still active.") if str(created) == expected else ("exited", "The owner exited and its process ID has been reused.")
        recorded = owner.get("process_started_at") or owner.get("started_at") or owner.get("created_at")
        if not isinstance(recorded, str):
            return "unknown", "The runtime owner has no creation or ownership timestamp."
        recorded_time = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
        if recorded_time.tzinfo is None:
            return "unknown", "The owner timestamp has no timezone."
        created_epoch = created / 10_000_000 - 11_644_473_600
        if owner.get("process_started_at"):
            matches = abs(created_epoch - recorded_time.timestamp()) <= 2
        else:
            matches = created_epoch <= recorded_time.timestamp()
        return ("active", "The live process creation time matches the ownership record.") if matches else ("exited", "The owner exited and its process ID has been reused.")
    finally:
        kernel.CloseHandle(handle)


def discover_resources(listed: set[Path]) -> list[dict[str, Any]]:
    discovered: dict[Path, dict[str, Any]] = {}
    roots = {base / suffix for base in (ROOT, SOURCE_ROOT) for suffix in (".scratch/runtime", ".scratch/check-runs/runtime-tmp")}
    for root in sorted(roots):
        try:
            project_path(str(root))
            if not root.exists():
                continue
            for current, directories, _ in os.walk(root, followlinks=False):
                location = Path(current)
                directories[:] = [name for name in directories if not (location / name).is_symlink() and not (location / name).is_junction()]
                marker = location / "owner.json"
                candidate = marker.exists() or (location.parent == root and location.name.startswith(("b-", "p-", "run-", "myrk-run-")))
                if not candidate or location in discovered:
                    continue
                directories.clear()
                entry: dict[str, Any] = {"path": str(location), "listed": location in listed, "state": "unknown"}
                discovered[location] = entry
                try:
                    project_path(str(marker))
                    owner = json.loads(marker.read_text(encoding="utf-8"))
                    if not isinstance(owner, dict) or project_path(owner.get("runtime_directory")) != location or project_path(owner.get("project_root")) != ROOT:
                        raise ValueError("The runtime owner does not match its project and directory.")
                    entry["owner"] = owner.get("owner") or owner.get("purpose")
                    entry["pid"] = owner.get("pid")
                    entry["state"], entry["reason"] = owner_state(owner)
                except (OSError, ValueError) as error:
                    entry["reason"] = str(error)
        except (OSError, ValueError) as error:
            discovered[root] = {"path": str(root), "listed": False, "state": "unknown", "reason": str(error)}
    return list(discovered.values())


def check_manifest(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("The manifest must be a JSON object.")
    root = document.get("project_root")
    if not isinstance(root, str) or not Path(root).is_absolute() or project_path(root) != ROOT:
        raise ValueError(f"project_root must be this script's project root: {ROOT}")
    entries: list[tuple[str, dict[str, Any], Path]] = []
    branch_entries: list[tuple[dict[str, Any], str]] = []
    for section, key in (("paths", "path"), ("worktrees", "path"), ("branches", "name")):
        resources = document.get(section, [])
        if not isinstance(resources, list):
            raise ValueError(f"{section} must be a list.")
        for resource in resources:
            if not isinstance(resource, dict) or resource.get("status") not in ("removed", "retained"):
                raise ValueError(f"Each {section} entry needs status removed or retained.")
            name = resource.get(key)
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Each {section} entry needs a non-empty {key}.")
            if resource["status"] == "retained":
                for field in ("owner", "reason", "delete_when"):
                    if not isinstance(resource.get(field), str) or not resource[field].strip():
                        raise ValueError(f"Retained resource {name} needs a non-empty {field}.")
            if section == "branches":
                branch_entries.append((resource, name))
            else:
                entries.append((section, resource, project_path(name)))
    errors = []
    worktrees: set[Path] = set()
    branches: set[str] = set()
    if document.get("worktrees") or document.get("branches"):
        for arguments in (
            ["worktree", "list", "--porcelain", "-z"],
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        ):
            process = subprocess.run(
                ["git", "-C", str(ROOT), *arguments],
                capture_output=True, text=True, encoding="utf-8",
            )
            if process.returncode:
                raise ValueError(f"Cannot inspect Git resources: {process.stderr.strip()}")
            if arguments[0] == "worktree":
                worktrees.update(Path(field[9:]).resolve() for field in process.stdout.split("\0") if field.startswith("worktree "))
            else:
                branches.update(process.stdout.splitlines())
    for section, resource, location in entries:
        expected = resource["status"] == "retained"
        exists = location.exists()
        if exists != expected:
            errors.append(f"{location}: expected {resource['status']}, but {'exists' if exists else 'is missing'}.")
        if section == "worktrees" and (location in worktrees) != expected:
            errors.append(f"{location}: Git worktree registration does not match {resource['status']}.")
    for resource, branch in branch_entries:
        expected = resource["status"] == "retained"
        if (branch in branches) != expected:
            errors.append(f"{branch}: Git branch does not match {resource['status']}.")
    discovered = discover_resources({location for _, _, location in entries})
    for resource in discovered:
        if not resource["listed"] and resource["state"] != "active":
            errors.append(f"Unlisted runtime resource ({resource['state']}): {resource['path']}. {resource['reason']}")
    return {"ok": not errors, "project_root": str(ROOT), "checked": len(entries) + len(branch_entries), "errors": errors, "discovered_resources": discovered}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="Project-local JSON resource manifest")
    options = parser.parse_args()
    try:
        manifest = project_path(options.manifest)
        result = check_manifest(json.loads(manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        result = {"ok": False, "errors": [str(error)]}
    result["cleanup_failures"] = []
    try:
        receipt_root = project_path(str(ROOT / ".scratch/runtime/cleanup-failures"))
        if receipt_root.exists():
            if not receipt_root.is_dir():
                raise ValueError(f"Runtime cleanup receipt root is not a directory: {receipt_root}")
            condition = "After the owning process has exited, verify its ordinary runtime directory is no longer locked, remove it, and clear the receipt only after verifying removal."
            for receipt in sorted(receipt_root.glob("*.json")):
                receipt = project_path(str(receipt))
                result["cleanup_failures"].append({"path": str(receipt), "delete_when": condition})
                result["errors"].append(f"Runtime cleanup failure receipt remains: {receipt}. Retry: {condition}")
    except (OSError, ValueError) as error:
        result["errors"].append(str(error))
    result["ok"] = not result["errors"]
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
