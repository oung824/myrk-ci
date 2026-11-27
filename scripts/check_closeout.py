"""Observe task files and verify their declared final disposition without writing."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, NoReturn


GIT_ENVIRONMENT = dict(os.environ)
GIT_INDEX_FILES: dict[Path, Path] = {}


def git(root: Path, *arguments: str, input_data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(root),
                             *arguments], env={**GIT_ENVIRONMENT, "GIT_OPTIONAL_LOCKS": "0",
                             **({"GIT_INDEX_FILE": str(GIT_INDEX_FILES[root])} if root in GIT_INDEX_FILES else {})},
                             input=input_data, capture_output=True)
    if result.returncode:
        raise ValueError(f"Git failed ({' '.join(arguments)}): {result.stderr.decode('utf-8', errors='replace').strip()}")
    return result.stdout


def local_path(value: object, project: Path, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A path must be a non-empty string.")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError(f"Parent traversal is not accepted: {value}")
    if not path.is_absolute():
        path = base / path
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError(f"Linked file or ancestor is not accepted: {parent}")
    path = path.resolve()
    if not path.is_relative_to(project):
        raise ValueError(f"Path is outside the project: {path}")
    return path


def context(value: str) -> tuple[Path, Path]:
    source = Path(value).absolute()
    common = Path(git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    project = common.parent.resolve()
    source = local_path(str(source), project, project)
    if Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve() != source:
        raise ValueError("--root must be the source checkout root.")
    local_path(str(common), project, project)
    return source, project


def observe(path: Path, metadata: list[Path]) -> dict[str, Any]:
    record: dict[str, Any] = {"path": str(path), "metadata_only": any(path.is_relative_to(item) for item in metadata)}
    if not path.exists():
        return {**record, "kind": "missing"}
    if not path.is_file():
        raise ValueError(f"Expected an ordinary file: {path}")
    details = path.stat()
    record.update(kind="file", size=details.st_size, mtime_ns=details.st_mtime_ns)
    if not record["metadata_only"]:
        record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return record


def inventory(source: Path, project: Path, scopes: list[Path], metadata: list[Path]) -> dict[Path, dict[str, Any]]:
    paths: dict[Path, str] = {}
    for scope in [*scopes, *metadata]:
        if source.is_relative_to(scope) or project.is_relative_to(scope) or ".git" in scope.relative_to(project).parts:
            raise ValueError(f"Scope cannot include a checkout root or Git database: {scope}")
    tracked_paths: set[Path] = set()
    for root in dict.fromkeys((source, project)):
        tracked = git(root, "ls-files", "--cached", "-z")
        tracked_paths.update(local_path(name.decode("utf-8"), project, root) for name in tracked.split(b"\0") if name)
        attributes = git(root, "check-attr", "-z", "--stdin", "filter", input_data=tracked).split(b"\0")
        for index in range(0, len(attributes) - 1, 3):
            if attributes[index + 2] not in (b"unspecified", b"unset"):
                raise ValueError(f"Custom clean filter is not accepted: {root / attributes[index].decode('utf-8')}")
        exclusions = [":(top,exclude,literal)" + path.relative_to(root).as_posix() for path in metadata if path.is_relative_to(root)]
        for entry in git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--", ".", *exclusions).split(b"\0"):
            if entry:
                paths[local_path(entry[3:].decode("utf-8"), project, root)] = entry[:2].decode("ascii")
    for scope in dict.fromkeys([*scopes, *metadata]):
        local_path(str(scope), project, project)
        if scope.is_file():
            if scope not in tracked_paths or any(scope.is_relative_to(item) for item in metadata):
                paths.setdefault(scope, "!!")
        elif scope.exists():
            def scan_error(error: OSError) -> None:
                raise error
            for current, directories, files in os.walk(scope, onerror=scan_error, followlinks=False):
                for name in directories:
                    local_path(str(Path(current) / name), project, project)
                for name in files:
                    path = local_path(str(Path(current) / name), project, project)
                    if path not in tracked_paths or any(path.is_relative_to(item) for item in metadata):
                        paths.setdefault(path, "!!")
    return {path: {**observe(path, metadata), "git_status": status} for path, status in sorted(paths.items())}


def snapshot(options: argparse.Namespace, source: Path, project: Path) -> dict[str, Any]:
    if not options.task_id.strip() or not options.target_ref.startswith("refs/"):
        raise ValueError("snapshot needs a task ID and a full target ref.")
    git(source, "check-ref-format", options.target_ref)
    git(source, "rev-parse", "--verify", options.target_ref + "^{commit}")
    scopes = list(dict.fromkeys([local_path(value, project, source) for value in options.scope] +
                               [local_path(".scratch/check-runs/results", project, project)]))
    metadata = [local_path(value, project, source) for value in options.metadata_only]
    return {"ok": True, "version": 1, "task_id": options.task_id, "source_root": str(source), "project_root": str(project),
            "start_commit": git(source, "rev-parse", "HEAD").decode().strip(), "target_ref": options.target_ref,
            "scopes": [str(path) for path in scopes], "metadata_only": [str(path) for path in metadata],
            "files": list(inventory(source, project, scopes, metadata).values())}


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
    if preflight:
        head = git(source, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        git(source, "merge-base", "--is-ancestor", baseline["start_commit"], head)
        revisions = list(dict.fromkeys([target, head]))
    scopes = [local_path(value, project, project) for value in baseline["scopes"]]
    scopes.append(local_path(".scratch/check-runs/results", project, project))
    metadata = [local_path(value, project, project) for value in baseline["metadata_only"]]
    entries = closeout["files"]
    fragments: list[Path] = []
    if "file_lists" in closeout:
        if not __package__:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts.closeout_file_lists import read_file_lists

        fragments, extra = read_file_lists(closeout["file_lists"], manifest_path, project, metadata, local_path)
        entries = [*entries, *extra]
    current = inventory(source, project, scopes, metadata)
    for revision in revisions:
        for changed_name in git(source, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", baseline["start_commit"], revision, "--").split(b"\0"):
            if changed_name:
                path = local_path(changed_name.decode("utf-8"), project, source)
                current.setdefault(path, observe(path, metadata))
    original = {local_path(entry["path"], project, project): entry for entry in baseline["files"]}
    required = {baseline_path, manifest_path, *fragments}
    candidates = set(current) | original.keys() | required
    declared = {local_path(entry["path"], project, project): entry for entry in entries}
    if len(declared) != len(entries):
        raise ValueError("Duplicate file dispositions are not accepted.")
    unclassified = sorted(candidates - declared.keys())
    errors = [f"Unclassified file: {path}" for path in unclassified]
    index_changes: dict[Path, set[str]] = {}
    indexes: dict[Path, dict[str, str]] = {}
    index_paths: dict[Path, set[str]] = {}
    for checkout in dict.fromkeys((source, project)):
        if preflight:
            index = {}
            for record in git(checkout, "ls-files", "--stage", "-z").split(b"\0"):
                if record:
                    details, name_bytes = record.split(b"\t", 1)
                    _, blob, stage = details.split()
                    index[name_bytes.decode("utf-8")] = blob.decode() if stage == b"0" else ""
            indexes[checkout] = index
            index_paths[checkout] = {parent.as_posix() for name in index
                                     for parent in (Path(name), *Path(name).parents) if str(parent) != "."}
        else:
            changed = git(checkout, "diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv",
                          "--no-renames", target, "--")
            index_changes[checkout] = {parent.as_posix() for value in changed.split(b"\0") if value
                                       for parent in (Path(value.decode("utf-8")), *Path(value.decode("utf-8")).parents)
                                       if str(parent) != "."}
    tree: dict[str, str] = {}
    if not preflight:
        for record in git(source, "ls-tree", "-r", "-t", "-z", "--full-tree", target).split(b"\0"):
            if record:
                details, name_bytes = record.split(b"\t", 1)
                tree[name_bytes.decode("utf-8")] = details.split()[2].decode()
    attribute_names: dict[Path, list[str]] = {}
    for path, entry in declared.items():
        if entry["outcome"] == "committed" and not entry.get("deletion"):
            checkout = source if path.is_relative_to(source) else project
            attribute_names.setdefault(checkout if preflight else source, []).append(path.relative_to(checkout).as_posix())
    attributes: dict[Path, dict[str, bytes]] = {}
    for checkout, names in attribute_names.items():
        values = git(checkout, "check-attr", "-z", "--stdin", "filter",
                     input_data=b"\0".join(name.encode("utf-8") for name in dict.fromkeys(names)) + b"\0").split(b"\0")
        attributes[checkout] = {values[index].decode("utf-8"): values[index + 2] for index in range(0, len(values) - 1, 3)}
    for path, entry in declared.items():
        if path in required and (entry["outcome"] != "committed" or entry.get("deletion")):
            errors.append(f"Required closeout file must be committed without deletion: {path}")
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
        checkout = source if path.is_relative_to(source) else project
        name = path.relative_to(checkout).as_posix()
        if entry["outcome"] in ("committed", "removed") and not preflight and name in index_changes[checkout]:
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
                if declared.get(equivalent, {}).get("outcome") != "committed" or observe(equivalent, metadata).get("sha256") != observe(path, metadata).get("sha256"):
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
            expected = entry.get("sha256") or original.get(path, {}).get("sha256")
            if not expected or observed.get("sha256") != expected:
                errors.append(f"External content needs explicit current sha256 acknowledgement: {path}")
        if entry["outcome"] == "committed":
            expected_blob = indexes[checkout].get(name) if preflight else tree.get(name)
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
            actual = git(checkout if preflight else source, "hash-object", "--path=" + name, "--", str(path)).decode().strip()
            if actual != expected_blob:
                errors.append(f"{'Index' if preflight else 'Committed'} content differs from local content: {path}")
    result = {"ok": not errors, "project_root": str(project), "source_root": str(source), "errors": errors,
              "checked": len(declared), "files": list(current.values())}
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
    for name in ("snapshot", "check", "preflight"):
        command = commands.add_parser(name)
        command.add_argument("--root", default=str(Path(__file__).resolve().parents[1]), help="Source checkout root")
        if name == "snapshot":
            command.add_argument("--task-id", required=True)
            command.add_argument("--target-ref", required=True)
            command.add_argument("--scope", action="append", default=[], help="Source-relative ignored-file scope")
            command.add_argument("--metadata-only", action="append", default=[], help="Source-relative metadata-only path")
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
        result = snapshot(options, source, project) if options.command == "snapshot" else check(options, source, project)
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
