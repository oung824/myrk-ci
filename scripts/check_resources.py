"""Read-only verification of a task's project-local resource manifest."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SOURCE_ROOT))
from myrk.project_paths import project_root, require_project_path
from myrk.runtime_ownership import RuntimeScope, owner_state as owner_state, verify_runtime_ownership


ROOT = project_root(SOURCE_ROOT)


def project_path(value: object) -> Path:
    return require_project_path(ROOT, value)


def manifest_task(document: dict[str, Any]) -> str | None:
    closeout = document.get("closeout", {})
    if not isinstance(closeout, dict):
        raise ValueError("The closeout manifest must be an object.")
    values = [section["task_id"] for section in (document, closeout) if "task_id" in section]
    if any(not isinstance(value, str) or not value or value != value.strip() for value in values):
        raise ValueError("The resource manifest task ID must be a non-empty string.")
    if values and any(value != values[0] for value in values):
        raise ValueError("The resource and file closeout task IDs differ.")
    return values[0] if values else None


def check_manifest(document: object, *, project: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("The manifest must be a JSON object.")
    root = document.get("project_root")
    if not isinstance(root, str) or not Path(root).is_absolute() or project_path(root) != ROOT:
        raise ValueError(f"project_root must be this script's project root: {ROOT}")
    scope = RuntimeScope.for_manifest(manifest_task(document), project_audit=project)
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
    if scope.scan_additional_runtime_roots or document.get("worktrees") or document.get("branches"):
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
                worktrees.update(Path(field[9:]) for field in process.stdout.split("\0") if field.startswith("worktree "))
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
    runtime = verify_runtime_ownership(
        ROOT, SOURCE_ROOT, scope=scope, registered_worktrees=worktrees,
        listed_paths={location for section, _, location in entries if section == "paths"},
        listed_worktrees={location for section, _, location in entries if section == "worktrees"},
    )
    errors.extend(runtime.errors)
    return {"ok": not errors, "project_root": str(ROOT), "checked": len(entries) + len(branch_entries), "errors": errors,
            "resource_scope": "project" if scope.task_id is None else "task", "task_id": scope.task_id,
            "discovered_resources": runtime.directories, "cleanup_failures": runtime.cleanup_failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", help="Project-local JSON resource manifest")
    parser.add_argument("--project", action="store_true", help="Audit runtime resources and cleanup receipts across the project")
    options = parser.parse_args()
    if options.manifest is None and not options.project:
        parser.error("Provide a task manifest or use --project.")
    try:
        document = {"project_root": str(ROOT)} if options.manifest is None else json.loads(
            project_path(options.manifest).read_text(encoding="utf-8"),
        )
        result = check_manifest(document, project=options.project)
    except (OSError, ValueError) as error:
        result = {"ok": False, "errors": [str(error)], "cleanup_failures": []}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
