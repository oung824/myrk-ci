"""Observe task files and verify their declared final disposition without writing."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, NoReturn


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.closeout_borrowed import verify_borrowed_checks
from scripts.closeout_inventory import (
    GIT_ENVIRONMENT, GIT_INDEX_FILES, archive_paths, checkout_path, committed_blobs, committed_filters, context, diff_names,
    final_index_changes, git, inventory, lexical_path, linked_archive_blobs, local_path, observe, path_and_parent_names,
    proposed_paths, scoped_path, staged_blobs,
)
from scripts.closeout_runs import LAUNCHES, RESULTS, verify_task_runs


def shared_check_directories(source: Path, project: Path, task_scope: bool) -> tuple[Path, ...]:
    if not task_scope:
        return ()
    return tuple(root / directory for root in dict.fromkeys((source, project)) for directory in (LAUNCHES, RESULTS))


def snapshot(options: argparse.Namespace, source: Path, project: Path) -> dict[str, Any]:
    observed_at = datetime.now(timezone.utc).isoformat()
    if not options.task_id.strip() or not options.target_ref.startswith("refs/"):
        raise ValueError("snapshot needs a task ID and a full target ref.")
    git(source, "check-ref-format", options.target_ref)
    git(source, "rev-parse", "--verify", options.target_ref + "^{commit}")
    scopes = [local_path(value, project, source) for value in options.scope]
    if options.task_files and not scopes:
        raise ValueError("Task file inventory needs at least one --scope.")
    if not options.verify_runs and not options.task_files:
        scopes.append(local_path(str(RESULTS), project, project))
    scopes = list(dict.fromkeys(scopes))
    path_for_metadata = lexical_path if options.task_files else local_path
    metadata = [path_for_metadata(value, project, source) for value in options.metadata_only]
    excluded = shared_check_directories(source, project, options.verify_runs)
    files = inventory(source, project, scopes, metadata, excluded, task_scopes=tuple(scopes) if options.task_files else None)
    result = {"ok": True, "version": 1, "task_id": options.task_id, "source_root": str(source), "project_root": str(project),
            "start_commit": git(source, "rev-parse", "HEAD").decode().strip(), "target_ref": options.target_ref,
            "scopes": [str(path) for path in scopes], "metadata_only": [str(path) for path in metadata]}
    if options.task_files:
        result["file_scope"] = "task"
    if options.verify_runs:
        result["runs_observed_at"] = observed_at
        result["shared_check_scope"] = "task"
        verification = verify_task_runs(source, project, result, {}, {}, {}, metadata)
        for path in sorted(verification.paths - files.keys()):
            files[path] = observe(local_path(str(path), project, project), metadata)
    result["files"] = list(files.values())
    return result


def check(options: argparse.Namespace, source: Path, project: Path) -> dict[str, Any]:
    preflight = options.command == "preflight"
    manifest_path = local_path(options.manifest, project, source)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    closeout = manifest["closeout"]
    baseline_path = local_path(closeout["baseline"], project, project)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if (type(baseline["version"]) is not int or baseline["version"] != 1
            or not isinstance(baseline["task_id"], str) or not baseline["task_id"].strip()
            or baseline["source_root"] != str(source) or baseline["project_root"] != str(project)
            or closeout["task_id"] != baseline["task_id"] or closeout["target_ref"] != baseline["target_ref"]
            or manifest["project_root"] != str(project)):
        raise ValueError("The baseline task, roots or target ref do not match the manifest.")
    if (any(not isinstance(baseline[key], list) for key in ("scopes", "metadata_only", "files"))
            or not isinstance(closeout["files"], list)):
        raise ValueError("The baseline scopes, metadata_only and files, and closeout files must be lists.")
    reference = closeout["target_ref"]
    if not isinstance(reference, str) or not reference.startswith("refs/"):
        raise ValueError("The target must be a full Git ref.")
    git(source, "check-ref-format", reference)
    reference_tip = git(source, "rev-parse", "--verify", reference + "^{commit}").decode().strip()
    target = reference_tip if preflight else git(source, "rev-parse", "--verify", options.commit + "^{commit}").decode().strip()
    if target != reference_tip:
        raise ValueError("The checked commit must equal the target ref's current tip.")
    git(source, "merge-base", "--is-ancestor", baseline["start_commit"], target)
    revisions = [target]
    head = target
    if preflight:
        head = git(source, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        git(source, "merge-base", "--is-ancestor", baseline["start_commit"], head)
        revisions = list(dict.fromkeys([target, head]))
    task_scope = "runs_observed_at" in baseline
    if not task_scope and closeout.get("verify_original_bytes") is True:
        raise ValueError("verify_original_bytes requires a baseline from snapshot --verify-runs.")
    excluded = shared_check_directories(source, project, task_scope)

    def shared(path: Path) -> bool:
        return any(path.is_relative_to(directory) for directory in excluded)

    if baseline.get("file_scope") not in (None, "task"):
        raise ValueError("Unknown baseline file scope.")
    scopes = [local_path(value, project, project) for value in baseline["scopes"]]
    task_files = baseline.get("file_scope") == "task"
    if task_files and not scopes:
        raise ValueError("The task file baseline has no scopes.")
    if not task_scope and not task_files:
        scopes.append(local_path(str(RESULTS), project, project))
    path_for_metadata = lexical_path if task_files else local_path
    metadata = [path_for_metadata(value, project, project) for value in baseline["metadata_only"]]
    entries = closeout["files"]
    fragments: list[Path] = []
    if "file_lists" in closeout:
        from scripts.closeout_file_lists import read_file_lists

        fragments, extra = read_file_lists(closeout["file_lists"], manifest_path, project, metadata, local_path)
        entries = [*entries, *extra]
    required = {baseline_path, manifest_path, *fragments}
    declared = {local_path(entry["path"], project, project): entry for entry in entries}
    if len(declared) != len(entries):
        raise ValueError("Duplicate file dispositions are not accepted.")
    original_records = {project / entry["path"]: entry for entry in baseline["files"]}
    associated = required | {path for path, entry in declared.items() if not shared(path) or entry["outcome"] != "external"}
    verification = None
    if task_scope:
        verification = verify_task_runs(source, project, baseline, closeout, original_records, declared, metadata)
        associated.update(verification.paths)
        if baseline.get("shared_check_scope") == "task":
            associated.update(path for path in original_records if shared(path))
    elif "check_runs" in closeout or "borrowed_checks" in closeout:
        raise ValueError("The check requirements need a baseline established with snapshot --verify-runs.")
    borrowed = verify_borrowed_checks(
        baseline["task_id"], project, baseline.get("runs_observed_at", ""), closeout.get("borrowed_checks", []), metadata,
    )
    archive = archive_paths(closeout, declared, verification.originals if verification else set(),
                            borrowed.paths if not borrowed.errors else set())
    associated.update(borrowed.paths & declared.keys())
    task_scopes = tuple(scopes) if task_files else None
    current = inventory(source, project, scopes, metadata, excluded, task_scopes=task_scopes)
    proposed: set[Path] = set()
    proposed_names: set[str] = set()
    if preflight:
        proposed_names, proposed = proposed_paths(source, project, head, () if task_files else (source / LAUNCHES, source / RESULTS))
        associated.update(proposed)
    declared = {path: entry for path, entry in declared.items() if path in associated}

    def relevant(path: Path) -> bool:
        if path in associated:
            return True
        return not shared(path) and (task_scopes is None or scoped_path(path, task_scopes))

    for revision in revisions:
        for changed_name in diff_names(source, baseline["start_commit"], revision):
            path = source / changed_name
            if relevant(path):
                path = local_path(str(path), project, project)
                if path not in current:
                    current[path] = observe(path, metadata)
    original = {local_path(str(path), project, project): entry for path, entry in original_records.items()
                if relevant(path)}
    for path in sorted(proposed - current.keys()):
        current[path] = observe(path, metadata)
    if verification is not None:
        for path in sorted(verification.paths - current.keys()):
            current[path] = observe(local_path(str(path), project, project), metadata)
    candidates = set(current) | original.keys() | required
    unclassified = sorted(candidates - declared.keys())
    errors = [f"Unclassified file: {path}" for path in unclassified]
    if verification is not None:
        errors.extend(verification.errors)
    errors.extend(borrowed.errors)
    linked_archive = archive if source != project else set()
    indexes = {checkout: staged_blobs(checkout) for checkout in dict.fromkeys((source, project))} if preflight else {}
    index_paths = {checkout: path_and_parent_names(index) for checkout, index in indexes.items()}
    index_changes = {} if preflight else final_index_changes(source, project, target, linked_archive)
    tree = {} if preflight else committed_blobs(source, target)
    archive_blobs = (
        linked_archive_blobs(source, project, head, target, linked_archive, proposed_names, indexes[source])
        if preflight and linked_archive else {}
    )
    attributes = committed_filters(source, project, declared, preflight=preflight)
    for path, entry in declared.items():
        if path in required and (entry["outcome"] != "committed" or entry.get("deletion")):
            errors.append(f"Required closeout file must be committed without deletion: {path}")
            continue
        if path in proposed and entry["outcome"] != "committed":
            kind = "file" if task_files else "check file"
            errors.append(f"Proposed {kind} must be declared committed: {path}")
            continue
        if path.is_dir():
            errors.append(f"Dispositions must name individual files, not directories: {path}")
            continue
        if entry["outcome"] not in ("committed", "removed", "retained_local", "external"):
            errors.append(f"Unknown file outcome: {path}")
            continue
        if entry["outcome"] in ("external", "retained_local"):
            fields = ("owner", "reason", "delete_when") if entry["outcome"] == "retained_local" else ("owner", "reason")
            if any(not isinstance(entry.get(field), str) or not entry[field].strip() for field in fields):
                errors.append(f"{entry['outcome']} needs {'/'.join(fields)}: {path}")
                continue
        checkout, name = checkout_path(path, source, project)
        if not preflight and entry["outcome"] in ("committed", "removed") and name in index_changes[checkout]:
            errors.append(f"Index differs from the target commit: {path}")
            continue
        if entry["outcome"] == "removed" or (entry["outcome"] == "committed" and entry.get("deletion") is True):
            expected_paths = index_paths[checkout] if preflight else tree.keys()
            if path.exists() or name in expected_paths:
                errors.append(f"Removed path must be absent locally and from the {'index' if preflight else 'target tree'}: {path}")
            continue
        if entry["outcome"] == "retained_local":
            category = entry.get("category")
            if entry.get("required_evidence") or category not in ("dependency", "active_runtime", "credential", "duplicate_evidence", "timing_index"):
                errors.append(f"Necessary evidence or unsupported category cannot be retained_local: {path}")
                continue
            if category == "timing_index" and (
                path not in {project / ".scratch/check-runs/timings" / name for name in ("backend.json", "desktop.json")}
                or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024
            ):
                errors.append(f"Timing retention requires an approved project index no larger than 2 MiB: {path}")
                continue
            if category == "duplicate_evidence":
                equivalent = local_path(entry.get("equivalent_evidence"), project, project)
                record = declared.get(equivalent, {})
                digest = observe(path, metadata).get("sha256")
                if (record.get("outcome") != "committed" or record.get("deletion") or not digest
                        or observe(equivalent, metadata).get("sha256") != digest):
                    errors.append(f"Duplicate evidence needs equal committed equivalent_evidence: {path}")
                    continue
            if category == "credential":
                if not observe(path, metadata)["metadata_only"] or not path.is_file():
                    errors.append(f"Credential retention needs an existing metadata-only file: {path}")
                continue
        if entry["outcome"] in ("external", "retained_local"):
            observed = observe(path, metadata)
            if entry["outcome"] == "external" and observed["kind"] == "missing" and original.get(path, {}).get("kind") == "missing":
                continue
            if observed["metadata_only"]:
                prior = original.get(path, {})
                if any(observed.get(key) != prior.get(key) for key in ("kind", "size", "mtime_ns")):
                    errors.append(f"Protected file metadata differs from its baseline: {path}")
                continue
            expected = entry.get("sha256") or original.get(path, {}).get("sha256")
            if not expected or observed.get("sha256") != expected:
                errors.append(f"External content needs explicit current sha256 acknowledgement: {path}")
        if entry["outcome"] == "committed":
            expected_blob = archive_blobs.get(path, indexes[checkout].get(name)) if preflight else tree.get(name)
            if expected_blob is None or not path.is_file():
                errors.append(f"Committed content is missing: {path}")
                continue
            if any(path.is_relative_to(item) for item in metadata):
                errors.append(f"Cannot verify committed content of metadata-only file: {path}")
                continue
            attribute = attributes[checkout if preflight else source][name]
            if attribute not in (b"unspecified", b"unset"):
                errors.append(f"Custom clean filter is not accepted: {path}")
                continue
            option = "--no-filters" if path in archive else "--path=" + name
            actual = git(checkout if preflight else source, "hash-object", option, "--", str(path)).decode().strip()
            if actual != expected_blob:
                version = "Index" if preflight else "Committed"
                difference = "content differs from original bytes" if path in archive else "content differs from local content"
                errors.append(f"{version} {difference}: {path}")
    result = {"ok": not errors, "project_root": str(project), "source_root": str(source), "errors": errors,
              "checked": len(declared), "files": list(current.values())}
    if verification is not None:
        result["check_runs"] = verification.runs
    if "borrowed_checks" in closeout:
        result["borrowed_checks"] = borrowed.checks
        result["borrowed_paths"] = sorted(map(str, borrowed.paths))
    if preflight:
        result.update(candidates=[{"path": str(path), "observed": current.get(path) or observe(path, metadata),
                                   "declaration": declared.get(path)} for path in sorted(candidates | declared.keys())],
                      unclassified=[str(path) for path in unclassified])
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def main() -> int:
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for name in ("snapshot", "check", "preflight", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="Source checkout root")
        if name == "snapshot":
            command.add_argument("--task-id", required=True)
            command.add_argument("--target-ref", required=True)
            command.add_argument("--scope", action="append", default=[], help="Source-relative ignored-file scope")
            command.add_argument("--metadata-only", action="append", default=[], help="Source-relative metadata-only path")
            command.add_argument("--verify-runs", action="store_true", help="Trace this task's check launches and final receipts")
            command.add_argument("--task-files", action="store_true", help="Limit file inventory to declared --scope paths")
        elif name == "audit":
            command.add_argument("--previous", help="Compare with a saved project audit JSON file")
        else:
            command.epilog = "See docs/agents/worktree-cleanup.md."
            command.add_argument("manifest")
            if name == "check":
                command.add_argument("--commit", default="HEAD")
            else:
                command.add_argument("--index-file", help="Source checkout index")
    phase = None
    try:
        options = parser.parse_args()
        phase = options.command
        for variable in git(Path.cwd(), "rev-parse", "--local-env-vars").decode("ascii").splitlines():
            GIT_ENVIRONMENT.pop(variable, None)
        source, project = context(options.root)
        if phase == "preflight" and options.index_file:
            index_file = local_path(options.index_file, project, source)
            if not index_file.is_file():
                raise ValueError(f"Proposed index file does not exist: {index_file}")
            GIT_INDEX_FILES[source] = index_file
        if phase == "snapshot":
            result = snapshot(options, source, project)
        elif phase == "audit":
            from scripts.closeout_audit import audit

            result = audit(project, source, options.previous)
        else:
            result = check(options, source, project)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"ok": False, "errors": [str(error)]}
    if phase == "preflight":
        result.update(phase="preflight", final_verification_required=True)
        result.setdefault("candidates", [])
        result.setdefault("unclassified", [])
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
