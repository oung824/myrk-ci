"""Verify task ownership at the resource check command."""

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path.resolve() / "isolated-project"
    subprocess.run(["git", "init", "--quiet", "--template=", str(root)], check=True, capture_output=True)
    (root / "scripts").mkdir()
    (root / "myrk").mkdir()
    source = Path(__file__).resolve().parents[1]
    for relative in ("scripts/check_resources.py", "myrk/project_paths.py", "myrk/runtime_ownership.py"):
        shutil.copyfile(source / relative, root / relative)
    (root / "myrk/__init__.py").write_text("", encoding="utf-8")
    yield root
    for path in (root / ".git/objects").glob("[0-9a-f][0-9a-f]/*"):
        if path.is_file():
            path.chmod(0o600)


def write_json(path: Path, document: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def run_command(project: Path, *arguments: str) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, "-B", str(project / "scripts/check_resources.py"), *arguments],
        cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=25,
    )
    assert completed.stdout, completed.stderr
    return completed.returncode, json.loads(completed.stdout)


def runtime_owner(
    project: Path, relative: str, task_id: str, pid: int, **metadata: str,
) -> tuple[Path, dict[str, Any]]:
    directory = project / relative
    owner = {
        "task_id": task_id,
        "source_root": str(project),
        "project_root": str(project),
        "runtime_directory": str(directory),
        "pid": pid,
        "started_at": datetime.now(timezone.utc).isoformat(),
        **metadata,
    }
    write_json(directory / "owner.json", owner)
    return directory, owner


def cleanup_receipt(project: Path, directory: Path, owner: dict[str, Any]) -> Path:
    return write_json(project / ".scratch/runtime/cleanup-failures" / f"{directory.name}.json", {
        "runtime_directory": str(directory),
        "owner": owner,
        "failed_at": datetime.now(timezone.utc).isoformat(),
        "error_type": "PermissionError",
    })


def test_resource_command_defers_identified_external_failures_to_project_audit(project: Path) -> None:
    with subprocess.Popen([sys.executable, "-B", "-c", "pass"]) as exited:
        exited.wait(timeout=10)
    runtime, _ = runtime_owner(project, ".scratch/check-runs/runtime-tmp/b-foreign", "foreign", exited.pid)
    supervisor, owner = runtime_owner(
        project, ".scratch/runtime/myrk-run-foreign", "foreign", exited.pid, created_filetime="132537600000000000",
    )
    receipt = cleanup_receipt(project, supervisor, owner)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    originals = {path: path.read_bytes() for path in (runtime / "owner.json", supervisor / "owner.json", receipt, manifest)}

    status, report = run_command(project, str(manifest))
    assert status == 0, report
    assert report["resource_scope"] == "task"
    for key, expected in (("discovered_resources", runtime), ("cleanup_failures", receipt)):
        row = next(entry for entry in report[key] if entry["path"] == str(expected))
        assert row["ownership"] == "external"

    for arguments in (("--project",), ("--project", str(manifest))):
        status, report = run_command(project, *arguments)
        assert status == 1, report
        assert report["resource_scope"] == "project" and report["task_id"] is None, report
        assert any(str(runtime) in error for error in report["errors"])
        assert any(str(receipt) in error for error in report["errors"])
    assert {path: path.read_bytes() for path in originals} == originals


def test_receipt_rejects_a_number_in_place_of_a_boolean(project: Path) -> None:
    runtime, owner = runtime_owner(project, ".scratch/runtime/myrk-run-foreign", "foreign", 0xFFFFFFFF)
    write_json(runtime / "owner.json", {**owner, "metadata": {"recovered": True, "attempts": 1}})
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    cleanup_receipt(project, runtime, {**owner, "metadata": {"attempts": 1.0, "recovered": True}})
    status, report = run_command(project, str(manifest))
    assert status == 0 and report["cleanup_failures"][0]["ownership"] == "external", report

    cleanup_receipt(project, runtime, {**owner, "metadata": {"attempts": 1, "recovered": 1}})
    status, report = run_command(project, str(manifest))
    assert status == 1, report
    failure = report["cleanup_failures"][0]
    assert failure["ownership"] == "unresolved"
    assert "conflicts with the current owner record" in failure["ownership_error"]


def test_resource_command_blocks_current_and_unresolved_owners_and_receipts(project: Path) -> None:
    current, owner = runtime_owner(
        project, ".scratch/runtime/myrk-run-current", "current", os.getpid(), created_filetime="132537600000000000",
    )
    conflicting = cleanup_receipt(project, current, {**owner, "task_id": "foreign"})
    ambiguous = project / ".scratch/check-runs/runtime-tmp/b-unclaimed"
    ambiguous.mkdir(parents=True)
    malformed = conflicting.with_name("invalid.json")
    malformed.write_text("{", encoding="utf-8")
    ordinary_file = project / ".scratch/runtime/myrk-run-file"
    ordinary_file.write_text("ordinary file", encoding="utf-8")
    invalid_directory = cleanup_receipt(
        project, ordinary_file, {**owner, "task_id": "foreign", "runtime_directory": str(ordinary_file)},
    )
    manifest = write_json(
        project / "resources.json", {"project_root": str(project), "closeout": {"task_id": "current"}},
    )

    status, report = run_command(project, str(manifest))
    assert status == 1, report
    assert report["resource_scope"] == "task"
    for path in (current, ambiguous, conflicting, malformed, invalid_directory):
        assert any(str(path) in error for error in report["errors"]), report


def test_resource_command_requires_an_active_current_runtime_to_be_registered(project: Path) -> None:
    runtime, _ = runtime_owner(project, ".scratch/check-runs/runtime-tmp/b-current", "current", os.getpid())
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    status, report = run_command(project, str(manifest))
    assert status == 1, report
    assert any(str(runtime) in error for error in report["errors"]), report


def test_resource_command_preserves_explicit_disposition_for_an_external_path(project: Path) -> None:
    runtime, _ = runtime_owner(project, ".scratch/check-runs/runtime-tmp/b-listed", "foreign", os.getpid())
    manifest = write_json(project / "resources.json", {
        "project_root": str(project), "task_id": "current", "paths": [{"path": str(runtime), "status": "removed"}],
    })

    status, report = run_command(project, str(manifest))
    assert status == 1, report
    assert any(str(runtime) in error for error in report["errors"]), report


def test_resource_command_rejects_a_conflicting_manifest_task_identity(project: Path) -> None:
    manifest = write_json(project / "resources.json", {
        "project_root": str(project), "task_id": "current", "closeout": {"task_id": "foreign"},
    })

    status, report = run_command(project, str(manifest))
    assert status == 1 and report["errors"], report


def test_resource_command_distinguishes_legacy_manifest_from_explicit_project_audit(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = project / ".scratch/owned-runtime"
    candidate = runtime_root / "myrk-run-unverified"
    candidate.mkdir(parents=True)
    monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(runtime_root))
    manifest = write_json(project / "resources.json", {"project_root": str(project)})

    status, report = run_command(project, str(manifest))
    assert status == 0 and report["resource_scope"] == "project" and report["task_id"] is None, report
    assert report["discovered_resources"] == [], report

    status, report = run_command(project, "--project", str(manifest))
    assert status == 1 and report["resource_scope"] == "project" and report["task_id"] is None, report
    row, = report["discovered_resources"]
    assert row["path"] == str(candidate) and row["ownership"] == "project" and row["state"] == "unknown", report
    assert any(str(candidate) in error for error in report["errors"]), report


@pytest.mark.parametrize("parent", [".scratch/runtime", ".scratch/owned-runtime", "owned-runtime"],
                         ids=["default", "worktree-override", "project-override"])
def test_resource_command_checks_ownership_in_an_unlisted_worktree(project: Path, parent: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.name=Resource Test", "-c", "user.email=resource@example.invalid",
         "-c", f"core.hooksPath={project / '.git' / 'no-hooks'}", "commit", "--allow-empty", "-qm", "Baseline"],
        check=True, capture_output=True,
    )
    worktree = project / ".scratch/worktrees/foreign"
    subprocess.run(
        ["git", "-C", str(project), "worktree", "add", "--quiet", "--detach", str(worktree)],
        check=True, capture_output=True,
    )
    runtime_root = (worktree if parent.startswith(".scratch/") else project) / parent
    runtime, foreign = runtime_owner(project, str(runtime_root.relative_to(project) / "myrk-run-foreign"),
                                     "foreign", 0xFFFFFFFF, source_root=str(worktree), purpose="supervisor-runtime")
    archived_copy = worktree / ".scratch/archive/myrk-run-foreign"
    write_json(archived_copy / "owner.json", foreign)
    manifest = write_json(project / "resources.json", {
        "project_root": str(project), "task_id": "current",
    })

    status, report = run_command(project, str(manifest))
    assert status == 0, report
    assert any(row["path"] == str(runtime) and row["ownership"] == "external"
               for row in report["discovered_resources"]), report
    assert all(row["path"] != str(archived_copy) for row in report["discovered_resources"]), report

    owned, _ = runtime_owner(project, str(runtime_root.relative_to(project) / "myrk-run-current"),
                             "current", 0xFFFFFFFF, source_root=str(worktree), purpose="supervisor-runtime")
    status, report = run_command(project, str(manifest))
    assert status == 1, report
    assert any(row["path"] == str(owned) and row["ownership"] == "task" and not row["listed"]
               for row in report["discovered_resources"]), report
    assert len(report["errors"]) == 1 and str(owned) in report["errors"][0], report

    status, report = run_command(project, "--project")
    assert status == 1 and any(str(runtime) in error for error in report["errors"]), report


@contextmanager
def directory_link(link: Path, target: Path) -> Iterator[None]:
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        yield
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()


@pytest.mark.parametrize("custom", [False, True], ids=["default", "configured"])
def test_resource_command_reports_unclaimed_runtime_junction_without_following_it(
    project: Path, monkeypatch: pytest.MonkeyPatch, custom: bool,
) -> None:
    target = project / "outside-runtime"
    target.mkdir()
    original = write_json(target / "owner.json", {})
    root = project / (".scratch/owned-runtime" if custom else ".scratch/runtime")
    link = root / "myrk-run-linked"
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    if custom:
        monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(root))

    with directory_link(link, target):
        for argument in (str(manifest), "--project"):
            status, report = run_command(project, argument)
            assert status == 1 and any(str(link) in error for error in report["errors"]), report
            row, = report["discovered_resources"]
            assert row["path"] == str(link) and row["state"] == "unknown", report
        assert original.read_text(encoding="utf-8") == "{}"
        original.unlink()
        target.rmdir()
        status, report = run_command(project, str(manifest))
        assert status == 1 and any(str(link) in error for error in report["errors"]), report


@pytest.mark.parametrize("runtime_root", [".scratch/physical-runtime", ".scratch/runtime"], ids=["custom", "default"])
def test_resource_command_resolves_a_configured_alias_inside_the_project(
    project: Path, monkeypatch: pytest.MonkeyPatch, runtime_root: str,
) -> None:
    root = project / runtime_root
    root.mkdir(parents=True)
    alias = project / ".scratch/runtime-alias"
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(alias))

    with directory_link(alias, root):
        status, report = run_command(project, str(manifest))
        assert status == 0 and report["discovered_resources"] == [], report
        runtime = root / "myrk-run-unverified"
        runtime.mkdir()
        for argument in (str(manifest), "--project"):
            status, report = run_command(project, argument)
            assert status == 1 and any(str(runtime) in error for error in report["errors"]), report
            row, = report["discovered_resources"]
            assert row["path"] == str(runtime), report
        runtime_owner(project, str(runtime.relative_to(project)), "foreign", os.getpid(), purpose="")
        for argument, ownership in ((str(manifest), "unresolved"), ("--project", "project")):
            status, report = run_command(project, argument)
            assert status == 1, report
            row, = report["discovered_resources"]
            assert row["ownership"] == ownership and row["state"] == "unknown", report


def test_resource_command_reports_a_renamed_run_in_the_configured_root(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, _ = runtime_owner(project, ".scratch/owned-runtime/myrk-run-original", "current",
                                0xFFFFFFFF, purpose="supervisor-runtime")
    runtime = original.with_name("myrk-run-renamed")
    original.rename(runtime)
    monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(runtime.parent))
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    for argument, ownership in ((str(manifest), "unresolved"), ("--project", "project")):
        status, report = run_command(project, argument)
        assert status == 1 and len(report["errors"]) == 1 and str(runtime) in report["errors"][0], report
        row, = report["discovered_resources"]
        assert row["path"] == str(runtime) and row["ownership"] == ownership and row["state"] == "unknown", report


def test_resource_command_ignores_a_copied_marker_whose_original_parent_is_a_link(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MYRK_RUNTIME_ROOT", raising=False)
    original_parent = project / ".scratch/old-parent"
    runtime_owner(project, ".scratch/archive/myrk-run-past", "foreign", 0xFFFFFFFF,
                  runtime_directory=str(original_parent / "myrk-run-past"), purpose="supervisor-runtime")
    target = project / ".scratch/link-target"
    target.mkdir(parents=True)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    with directory_link(original_parent, target):
        for argument in (str(manifest), "--project"):
            status, report = run_command(project, argument)
            assert status == 0 and report["discovered_resources"] == [], report


@pytest.mark.parametrize("linked", [False, True], ids=["directory", "directory-link"])
def test_resource_command_reports_a_runtime_with_a_wrong_project_root(
    project: Path, monkeypatch: pytest.MonkeyPatch, linked: bool,
) -> None:
    monkeypatch.delenv("MYRK_RUNTIME_ROOT", raising=False)
    other_directory = project / ".scratch/other-directory"
    other_directory.mkdir(parents=True)
    claimed_project = project / ".scratch/project-alias" if linked else other_directory
    runtime, _ = runtime_owner(project, ".scratch/owned-runtime/myrk-run-current", "current", 0xFFFFFFFF,
                               purpose="supervisor-runtime", project_root=str(claimed_project))
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    claim = directory_link(claimed_project, other_directory) if linked else nullcontext()

    with claim:
        for argument, ownership in ((str(manifest), "unresolved"), ("--project", "project")):
            status, report = run_command(project, argument)
            assert status == 1 and len(report["errors"]) == 1 and str(runtime) in report["errors"][0], report
            row, = report["discovered_resources"]
            assert row["path"] == str(runtime) and row["ownership"] == ownership and row["state"] == "unknown", report


def test_resource_command_matches_equivalent_absolute_owner_paths(project: Path) -> None:
    runtime = project / ".scratch/owned-runtime/myrk-run-current"
    runtime_owner(
        project, str(runtime.relative_to(project)), "current", 0xFFFFFFFF, purpose="supervisor-runtime",
        project_root=str(project / ".scratch" / ".."), runtime_directory=str(runtime / ".." / runtime.name),
    )
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    status, report = run_command(project, str(manifest))
    assert status == 1 and any(str(runtime) in error for error in report["errors"]), report
    row, = report["discovered_resources"]
    assert row["path"] == str(runtime) and row["ownership"] == "task", report


@pytest.mark.parametrize("failure", ["damaged-record", "missing-configured-record", "empty-purpose"])
def test_resource_command_reports_unverified_custom_runtime(
    project: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    runtime = project / ".scratch/owned-runtime/myrk-run-unverified"
    runtime.mkdir(parents=True)
    (project / ".scratch/archive/myrk-run-without-marker").mkdir(parents=True)
    if failure == "damaged-record":
        monkeypatch.delenv("MYRK_RUNTIME_ROOT", raising=False)
        (runtime / "owner.json").write_text('{"purpose": "supervisor-runtime",', encoding="utf-8")
    else:
        monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(runtime.parent))
        if failure == "empty-purpose":
            runtime_owner(project, str(runtime.relative_to(project)), "current", 0xFFFFFFFF, purpose="")
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    for argument, ownership in ((str(manifest), "unresolved"), ("--project", "project")):
        status, report = run_command(project, argument)
        assert status == 1, report
        assert len(report["errors"]) == 1 and str(runtime) in report["errors"][0], report
        row, = report["discovered_resources"]
        assert row["path"] == str(runtime) and row["ownership"] == ownership and row["state"] == "unknown", report
