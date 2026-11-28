"""Resolve project ownership without loading the application or its runtime."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


class RuntimeDirectoryMismatch(ValueError):
    """The recorded absolute runtime directory resolves to another location."""


class IndependentRuntimeProject(ValueError):
    """The runtime belongs to a separate Git project within the checkout."""


def project_root(source: Path) -> Path:
    source = source.resolve()
    root = next((path for path in (source, *source.parents) if (path / ".git").exists()), source)
    marker = root / ".git"
    if marker.is_file():
        registration = marker.read_text(encoding="utf-8").strip()
        if not registration.startswith("gitdir:"):
            raise ValueError("Git 工作目录登记不完整，未建立运行副本。")
        git_directory = (root / registration.partition(":")[2].strip()).resolve()
        common = git_directory / "commondir"
        if common.is_file():
            git_directory = (git_directory / common.read_text(encoding="utf-8").strip()).resolve()
        if git_directory.name == ".git" and git_directory.is_dir():
            root = git_directory.parent
    return root


def require_project_path(root: Path, value: object, *, expected_runtime: Path | None = None) -> Path:
    return _resolve_project_path(root, _resource_path(value), expected_runtime=expected_runtime)


def _resource_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A resource path must be a non-empty string.")
    return Path(value)


def _resolve_project_path(root: Path, path: Path, *, expected_runtime: Path | None = None) -> Path:
    absolute = path.is_absolute()
    if not absolute:
        path = root / path

    resolved = path.resolve() if expected_runtime is not None else None
    if resolved is not None and resolved != expected_runtime:
        error = RuntimeDirectoryMismatch if absolute else ValueError
        raise error("The recorded runtime directory differs from the candidate.")

    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError(f"Linked resource or ancestor is not accepted: {part}")
    if resolved is None:
        resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Resource is outside the project: {resolved}")
    return resolved


def _git_output(checkout: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(checkout), *arguments],
            env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
            capture_output=True, text=True, encoding="utf-8",
        )
    except OSError as error:
        raise ValueError(f"Cannot inspect Git at {checkout}: {error}") from error
    if result.returncode != 0:
        raise ValueError(f"Cannot verify the Git checkout at {checkout}: {result.stderr.strip()}")
    return result.stdout


@dataclass(frozen=True)
class _GitWorktree:
    root: Path
    git_directory: Path
    common_directory: Path


def _verified_git_worktree(checkout: Path, *, require_worktree_root: bool = False) -> _GitWorktree:
    lines = _git_output(
        checkout, "rev-parse", "--is-inside-work-tree", "--path-format=absolute",
        "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
    ).splitlines()
    if len(lines) != 4 or lines[0] != "true":
        raise ValueError(f"Cannot verify the Git checkout at {checkout}.")
    paths = [Path(line) for line in lines[1:]]
    if not all(path.is_absolute() for path in paths):
        raise ValueError(f"Git returned an incomplete checkout location for {checkout}.")
    top_level, git_dir, common = (path.resolve() for path in paths)
    if require_worktree_root and top_level != checkout:
        raise ValueError(f"The recorded checkout is not a Git worktree root: {checkout}")
    if git_dir != common:
        listing = _git_output(checkout, "worktree", "list", "--porcelain", "-z")
        for record in listing.split("\0\0"):
            first, *fields = record.split("\0")
            if first.startswith("worktree ") and not any(
                field.partition(" ")[0] == "prunable" for field in fields
            ):
                registered = Path(first.removeprefix("worktree "))
                if registered == top_level:
                    break
        else:
            raise ValueError(f"The linked Git worktree is not registered at {top_level}.")
        try:
            registered_marker = (git_dir / "gitdir").read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise ValueError(f"Cannot read the Git worktree registration for {top_level}: {error}") from error
        if not registered_marker or (git_dir / registered_marker).resolve() != top_level / ".git":
            raise ValueError(f"The Git directory does not match the worktree registration at {top_level}.")
    return _GitWorktree(top_level, git_dir, common)


def _require_worktree_binding(worktree: _GitWorktree) -> None:
    git_dir = worktree.git_directory
    if git_dir != worktree.common_directory or git_dir == worktree.root / ".git":
        return
    if git_dir != worktree.root:
        bound_root = Path(_git_output(
            git_dir, "--git-dir=.", "rev-parse", "--path-format=absolute", "--show-toplevel",
        ).rstrip("\r\n"))
        if bound_root.is_absolute() and bound_root.resolve() == worktree.root:
            return
    raise ValueError(f"Cannot verify the separate Git directory for the worktree at {worktree.root}.")


def require_runtime_project(
    root: Path, audited_source: Path, runtime: Path, project_value: object, source_value: object,
) -> bool:
    """Check project scope and return whether Git verified the recorded source."""
    claim = _resource_path(project_value)
    project = _resolve_project_path(root, claim)
    if project == root:
        return False
    if not claim.is_absolute() or not runtime.is_relative_to(project):
        raise ValueError("The runtime owner names a different project.")

    source = _resource_path(source_value)
    if not source.is_absolute():
        raise ValueError("The recorded source path must be absolute.")
    source = _resolve_project_path(root, source)
    claimed_worktree = _verified_git_worktree(project, require_worktree_root=True)
    source_worktree = claimed_worktree if source == project else _verified_git_worktree(source)
    common = claimed_worktree.common_directory
    if source_worktree.common_directory != common:
        raise ValueError(f"The recorded project and source belong to different Git repositories: {project}, {source}")
    if source_worktree.root != claimed_worktree.root:
        _require_worktree_binding(claimed_worktree)
        _require_worktree_binding(source_worktree)
    if _verified_git_worktree(audited_source).common_directory == common:
        return True
    raise IndependentRuntimeProject(f"The runtime belongs to the independent Git project: {project}")
