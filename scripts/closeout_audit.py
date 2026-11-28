"""Report shared check records across project checkouts."""

import json
import os
from pathlib import Path
from typing import Any

from scripts.closeout_inventory import git, local_path, observe
from scripts.closeout_runs import LAUNCHES, RESULTS, load_launches


def audit(project: Path, source: Path, previous: str | None) -> dict[str, Any]:
    roots = {project, source}
    for field in git(project, "worktree", "list", "--porcelain", "-z").split(b"\0"):
        if field.startswith(b"worktree "):
            root = Path(field[9:].decode("utf-8"))
            if root.is_relative_to(project):
                roots.add(root)

    bindings: dict[Path, list[tuple[str, str | None, str | None]]] = {}
    launches = load_launches(project, [])
    for invocation, launch in launches.items():
        if "unreadable" in launch:
            continue
        task = launch.get("task_id")
        task_id = task if isinstance(task, str) and task.strip() else None
        bindings[project / LAUNCHES / (invocation + ".json")] = [(invocation, task_id, None)]
        terminal = launch.get("terminal")
        artifacts = terminal.get("required_artifacts", {}) if isinstance(terminal, dict) else {}
        hashes = dict(artifacts) if isinstance(artifacts, dict) else {}
        if isinstance(launch.get("result_path"), str):
            hashes[launch["result_path"]] = terminal.get("result_sha256") if isinstance(terminal, dict) else None
        for value, digest in hashes.items():
            path = Path(value)
            if path.is_relative_to(project / RESULTS) and ".." not in path.parts:
                bindings.setdefault(path, []).append((invocation, task_id, digest if isinstance(digest, str) else None))

    records: dict[str, dict[str, Any]] = {}

    def record(path: Path) -> None:
        key = str(path)
        entry: dict[str, Any] = {"path": key, "ownership": "unresolved"}
        matches = bindings.get(path, [])
        try:
            entry.update(observe(local_path(key, project, project), []))
        except (OSError, ValueError) as error:
            entry["error"] = str(error)
        if len(matches) > 1 and len({(task, digest) for _, task, digest in matches}) == 1:
            producers = {invocation for invocation, _, _ in matches}
            matches = [match for match in matches if launches[match[0]].get("parent_invocation_id") in producers]
        if len(matches) == 1:
            invocation, task, digest = matches[0]
            entry["invocation_id"] = invocation
            if digest and "sha256" in entry:
                entry["settled_fingerprint_matches"] = entry["sha256"] == digest
            if task:
                if "sha256" in entry and (path.parent == project / LAUNCHES or entry.get("settled_fingerprint_matches")):
                    entry.update(ownership="task", task_id=task)
                else:
                    entry["claimed_task_id"] = task
        records[key] = entry

    def scan_error(error: OSError) -> None:
        raise error

    for root in sorted(roots):
        for location in (LAUNCHES, RESULTS):
            directory = root / location
            local_path(str(directory), project, project)
            if not directory.exists():
                continue
            for parent, children, names in os.walk(directory, onerror=scan_error, followlinks=False):
                for name in children[:]:
                    child = Path(parent) / name
                    if child.is_symlink() or child.is_junction():
                        record(child)
                        children.remove(name)
                for name in names:
                    record(Path(parent) / name)

    result = {"ok": True, "version": 1, "project_root": str(project),
              "records": [records[key] for key in sorted(records)]}
    if previous is not None:
        path = local_path(previous, project, source)
        baseline = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(baseline, dict) or baseline.get("version") != 1 or baseline.get("ok") is not True
                or baseline.get("project_root") != str(project)
                or not isinstance(baseline.get("records"), list)):
            raise ValueError(f"Expected a successful audit from this project: {path}")
        prior = {item["path"]: item for item in baseline["records"]}
        result["changes"] = {"added": sorted(records.keys() - prior.keys()), "removed": sorted(prior.keys() - records.keys()),
                             "changed": sorted(key for key in records.keys() & prior.keys() if records[key] != prior[key])}
    return result
