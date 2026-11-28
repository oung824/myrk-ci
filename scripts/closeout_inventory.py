"""Inspect the checkout and file contents used by task closeout."""

from collections.abc import Iterable
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any


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


def diff_names(root: Path, *revisions: str, pathspecs: tuple[str, ...] = ()) -> list[str]:
    changed = git(root, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
                  *revisions, "--", *pathspecs)
    return [name.decode("utf-8") for name in changed.split(b"\0") if name]


def proposed_paths(
    source: Path, project: Path, head: str, directories: tuple[Path, ...],
) -> tuple[set[str], set[Path]]:
    names = diff_names(source, "--cached", "--ita-invisible-in-index", head)
    return set(names), {local_path(name, project, source) for name in names
                        if not directories or scoped_path(source / name, directories)}


def clean_filters(checkout: Path, names: Iterable[str]) -> dict[str, bytes]:
    values = git(checkout, "check-attr", "-z", "--stdin", "filter",
                 input_data=b"\0".join(name.encode("utf-8") for name in dict.fromkeys(names)) + b"\0").split(b"\0")
    return {values[index].decode("utf-8"): values[index + 2] for index in range(0, len(values) - 1, 3)}


def checkout_path(path: Path, source: Path, project: Path) -> tuple[Path, str]:
    checkout = source if path.is_relative_to(source) else project
    return checkout, path.relative_to(checkout).as_posix()


def committed_filters(
    source: Path, project: Path, declared: dict[Path, dict[str, Any]], *, preflight: bool,
) -> dict[Path, dict[str, bytes]]:
    attribute_names: dict[Path, list[str]] = {}
    for path, entry in declared.items():
        if entry["outcome"] == "committed" and not entry.get("deletion"):
            checkout, name = checkout_path(path, source, project)
            attribute_names.setdefault(checkout if preflight else source, []).append(name)
    return {checkout: clean_filters(checkout, names) for checkout, names in attribute_names.items()}


def archive_paths(
    closeout: dict[str, Any], declared: dict[Path, dict[str, Any]], originals: set[Path], borrowed: set[Path],
) -> set[Path]:
    automatic = closeout.get("verify_original_bytes", False)
    if type(automatic) is not bool:
        raise ValueError("verify_original_bytes must be a boolean.")
    identified = originals | borrowed if automatic else set()
    archive: set[Path] = set()
    for path, entry in declared.items():
        preserve = entry.get("preserve_bytes", False)
        if type(preserve) is not bool:
            raise ValueError(f"preserve_bytes must be a boolean: {path}")
        committed = entry.get("outcome") == "committed" and not entry.get("deletion")
        if preserve and not committed:
            raise ValueError(f"preserve_bytes needs committed content without deletion: {path}")
        if committed and (preserve or path in identified):
            archive.add(path)
    return archive


def path_and_parent_names(names: Iterable[str]) -> set[str]:
    return {parent.as_posix() for path in map(Path, names) for parent in (path, *path.parents) if str(parent) != "."}


def final_index_changes(source: Path, project: Path, target: str, linked_archive: set[Path]) -> dict[Path, set[str]]:
    changes = {
        checkout: path_and_parent_names(diff_names(checkout, "--cached", target))
        for checkout in dict.fromkeys((source, project))
    }
    archive_names: dict[Path, set[str]] = {}
    for path in linked_archive:
        checkout, name = checkout_path(path, source, project)
        if name in changes[checkout]:
            archive_names.setdefault(checkout, set()).add(name)
    for checkout, names in archive_names.items():
        staged = path_and_parent_names(diff_names(checkout, "--cached", "--ita-invisible-in-index", "HEAD"))
        changes[checkout].difference_update(names - staged)
    return changes


def staged_blobs(checkout: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for record in git(checkout, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            details, name = record.split(b"\t", 1)
            _, blob, stage = details.split()
            index[name.decode("utf-8")] = blob.decode() if stage == b"0" else ""
    return index


def committed_blobs(checkout: Path, target: str) -> dict[str, str]:
    tree: dict[str, str] = {}
    for record in git(checkout, "ls-tree", "-r", "-t", "-z", "--full-tree", target).split(b"\0"):
        if record:
            details, name = record.split(b"\t", 1)
            tree[name.decode("utf-8")] = details.split()[2].decode()
    return tree


def linked_archive_blobs(
    source: Path, project: Path, head: str, target: str, archive: set[Path],
    changed: set[str], index: dict[str, str],
) -> dict[Path, str | None]:
    names = {path: checkout_path(path, source, project)[1] for path in archive}
    blobs = {path: index.get(name) for path, name in names.items() if name in changed}
    if len(blobs) == len(names):
        return blobs

    target_blobs = committed_blobs(source, target)
    head_blobs = committed_blobs(source, head) if head != target else target_blobs
    base = git(source, "merge-base", head, target).decode().strip() if head != target else target
    base_blobs = head_blobs if base == head else target_blobs if base == target else committed_blobs(source, base)
    for path, name in names.items():
        if path not in blobs:
            target_blob = target_blobs.get(name)
            blobs[path] = head_blobs.get(name) if target_blob == base_blobs.get(name) else target_blob
    return blobs


def lexical_path(value: object, project: Path, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A path must be a non-empty string.")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError(f"Parent traversal is not accepted: {value}")
    if not path.is_absolute():
        path = base / path
    if not path.is_relative_to(project):
        raise ValueError(f"Path is outside the project: {path}")
    return path


def local_path(value: object, project: Path, base: Path) -> Path:
    path = lexical_path(value, project, base)
    for parent in (path, *path.parents):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError(f"Linked file or ancestor is not accepted: {parent}")
    path = path.resolve()
    if not path.is_relative_to(project):
        raise ValueError(f"Path is outside the project: {path}")
    return path


def scoped_path(path: Path, scopes: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(scope) for scope in scopes)


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


def read_json(path: Path, metadata: list[Path]) -> tuple[dict[str, Any], str]:
    if any(path.is_relative_to(item) for item in metadata):
        raise ValueError(f"Cannot read metadata-only check evidence: {path}")
    raw = path.read_bytes()
    document = json.loads(raw)
    if not isinstance(document, dict):
        raise ValueError(f"Expected a check record object: {path}")
    return document, hashlib.sha256(raw).hexdigest()


def check_report_inventory(receipt: dict[str, Any], sealed: set[Path], project: Path) -> None:
    reports = receipt.get("retained_reports", {})
    values = [*receipt.get("failure_logs", []), *receipt.get("report_files", []), *reports.get("files", [])]
    retained = receipt.get("retained")
    if isinstance(retained, dict):
        values.extend(retained.get("paths", []))
    for value in values:
        path = local_path(value, project, project)
        if path not in sealed:
            raise ValueError(f"The required check artifact lacks a producer fingerprint: {path}")
    for value in [*receipt.get("reports", []), *reports.get("paths", [])]:
        path = local_path(value, project, project)
        if not any(file.is_relative_to(path) for file in sealed):
            raise ValueError(f"The required check report lacks a producer inventory: {path}")


def inventory(
    source: Path, project: Path, scopes: list[Path], metadata: list[Path], excluded: tuple[Path, ...] = (),
    *, task_scopes: tuple[Path, ...] | None = None,
) -> dict[Path, dict[str, Any]]:
    def included(path: Path) -> bool:
        return not scoped_path(path, excluded)

    scan_scopes = [*scopes, *metadata] if task_scopes is None else [
        *task_scopes, *(path for path in metadata if scoped_path(path, task_scopes)),
    ]
    paths: dict[Path, str] = {}
    for scope in scan_scopes:
        if source.is_relative_to(scope) or project.is_relative_to(scope) or ".git" in scope.relative_to(project).parts:
            raise ValueError(f"Scope cannot include a checkout root or Git database: {scope}")
    tracked_paths: set[Path] = set()
    for root in dict.fromkeys((source, project)):
        root_scopes = (root,) if task_scopes is None else tuple(
            path for path in task_scopes if included(path) and root == (source if path.is_relative_to(source) else project)
        )
        if not root_scopes:
            continue
        pathspecs = (".",) if task_scopes is None else tuple(
            ":(top,literal)" + path.relative_to(root).as_posix() for path in root_scopes
        )
        tracked_args = () if task_scopes is None else ("--", *pathspecs)
        tracked = [
            name for name in git(root, "ls-files", "--cached", "-z", *tracked_args).split(b"\0")
            if name and included(root / name.decode("utf-8")) and scoped_path(root / name.decode("utf-8"), root_scopes)
        ]
        tracked_paths.update(local_path(name.decode("utf-8"), project, root) for name in tracked)
        attributes = git(root, "check-attr", "-z", "--stdin", "filter",
                         input_data=b"\0".join(tracked + [b""])).split(b"\0")
        for index in range(0, len(attributes) - 1, 3):
            if attributes[index + 2] not in (b"unspecified", b"unset"):
                raise ValueError(f"Custom clean filter is not accepted: {root / attributes[index].decode('utf-8')}")
        exclusions = [":(top,exclude,literal)" + path.relative_to(root).as_posix()
                      for path in (*metadata, *excluded) if path.is_relative_to(root)]
        status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames",
                     "--", *pathspecs, *exclusions)
        for entry in status.split(b"\0"):
            if entry:
                name = entry[3:].decode("utf-8")
                if scoped_path(root / name, root_scopes):
                    paths[local_path(name, project, root)] = entry[:2].decode("ascii")
    for scope in dict.fromkeys(scan_scopes):
        if not included(scope):
            continue
        local_path(str(scope), project, project)
        if scope.is_file():
            if scope not in tracked_paths or any(scope.is_relative_to(item) for item in metadata):
                paths.setdefault(scope, "!!")
        elif scope.exists():
            def scan_error(error: OSError) -> None:
                raise error
            for current, directories, files in os.walk(scope, onerror=scan_error, followlinks=False):
                if task_scopes is not None and (".git" in directories or ".git" in files):
                    raise ValueError(f"Scope cannot include a nested Git checkout: {current}")
                directories[:] = [name for name in directories if included(Path(current) / name)]
                for name in directories:
                    local_path(str(Path(current) / name), project, project)
                for name in files:
                    if not included(Path(current) / name):
                        continue
                    path = local_path(str(Path(current) / name), project, project)
                    if path not in tracked_paths or any(path.is_relative_to(item) for item in metadata):
                        paths.setdefault(path, "!!")
    return {path: {**observe(path, metadata), "git_status": status} for path, status in sorted(paths.items())}
