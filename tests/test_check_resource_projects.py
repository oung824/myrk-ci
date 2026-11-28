"""Check runtime ownership claims involving independent Git projects."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
import shutil
import subprocess

import pytest

from myrk.project_paths import project_root
from myrk.runtime_ownership import RuntimeScope, verify_runtime_ownership
from tests.test_check_resources import project, run_command, runtime_owner, write_json


def create_independent_project(project: Path, *, separate_git_dir: bool = False) -> Path:
    nested = project / ".scratch/independent-project"
    nested.mkdir(parents=True)
    options = ["--separate-git-dir", str(nested / "git-metadata")] if separate_git_dir else []
    subprocess.run(
        ["git", "init", "--quiet", "--template=", *options, str(nested)],
        check=True, capture_output=True,
    )
    assert project_root(nested) == nested
    assert (nested / ".git").is_file() == separate_git_dir
    return nested


def bind_separate_primary_worktree(project: Path) -> None:
    for arguments in (
        ("--local", "extensions.worktreeConfig", "true"),
        ("--worktree", "core.worktree", str(project)),
    ):
        subprocess.run(
            ["git", "-C", str(project), "config", *arguments], check=True, capture_output=True, timeout=25,
        )


@contextmanager
def registered_worktrees(project: Path, *names: str, separate_git_dir: bool) -> Iterator[tuple[Path, ...]]:
    checkouts = tuple(project / ".scratch/worktrees" / name for name in names)
    common = project / ("git-metadata" if separate_git_dir else ".git")
    git = ["git", "-C", str(project), "-c", f"core.hooksPath={common / 'no-hooks'}"]
    try:
        subprocess.run(
            [*git, "-c", "user.name=Resource Test", "-c", "user.email=resource@example.invalid",
             "-c", "commit.gpgSign=false", "commit", "--quiet", "--allow-empty", "-m", "Fixture"],
            check=True, capture_output=True, timeout=25,
        )
        with ExitStack() as cleanup:
            for checkout in checkouts:
                subprocess.run(
                    [*git, "worktree", "add", "--quiet", "--detach", str(checkout)],
                    check=True, capture_output=True, timeout=25,
                )
                cleanup.callback(
                    subprocess.run, [*git, "worktree", "remove", "--force", str(checkout)],
                    check=True, capture_output=True, timeout=25,
                )
            yield checkouts
    finally:
        for path in (common / "objects").glob("[0-9a-f][0-9a-f]/*"):
            if path.is_file():
                path.chmod(0o600)


def assert_runtime_needs_verification(project: Path, manifest: Path, runtime: Path, *, reason: str = "") -> None:
    for argument, ownership in ((str(manifest), "unresolved"), ("--project", "project")):
        status, report = run_command(project, argument)
        assert status == 1 and len(report["errors"]) == 1 and str(runtime) in report["errors"][0], report
        row, = report["discovered_resources"]
        assert row["path"] == str(runtime) and row["ownership"] == ownership and row["state"] == "unknown", report
        if reason:
            assert reason in row["reason"], report


def assert_runtime_is_excluded(
    source: Path, manifest: Path, marker: Path, original: bytes, *, expected_project: Path | None = None,
) -> None:
    for argument in (str(manifest), "--project"):
        status, report = run_command(source, argument)
        assert status == 0 and report["discovered_resources"] == [], report
        if expected_project is not None:
            assert report["project_root"] == str(expected_project), report
    assert marker.read_bytes() == original


@pytest.mark.parametrize("separate_git_dir", [False, True], ids=["standard-metadata", "separate-metadata"])
@pytest.mark.parametrize("configured", [False, True], ids=["discovered-root", "configured-root"])
def test_resource_command_excludes_verified_independent_project(
    project: Path, monkeypatch: pytest.MonkeyPatch, separate_git_dir: bool, configured: bool,
) -> None:
    monkeypatch.delenv("MYRK_RUNTIME_ROOT", raising=False)
    nested = create_independent_project(project, separate_git_dir=separate_git_dir)
    runtime_path = nested / ".scratch/runtime/myrk-run-other"
    runtime, _ = runtime_owner(
        project, str(runtime_path.relative_to(project)), "another-task", 0xFFFFFFFF,
        purpose="supervisor-runtime", project_root=str(nested), source_root=str(nested),
    )
    if configured:
        monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(runtime.parent))
    marker = runtime / "owner.json"
    original = marker.read_bytes()
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    assert_runtime_is_excluded(project, manifest, marker, original)


@pytest.mark.parametrize("invalid_claim", ["runtime-location", "source-project", "project-subdirectory"])
def test_resource_command_reports_invalid_independent_project_claims(
    project: Path, monkeypatch: pytest.MonkeyPatch, invalid_claim: str,
) -> None:
    monkeypatch.delenv("MYRK_RUNTIME_ROOT", raising=False)
    nested = create_independent_project(project)
    claimed_root = nested / "subdirectory" if invalid_claim == "project-subdirectory" else nested
    runtime_root = project if invalid_claim == "runtime-location" else claimed_root
    source_root = project if invalid_claim == "source-project" else nested
    runtime_path = runtime_root / ".scratch/runtime/myrk-run-other"
    runtime, _ = runtime_owner(
        project, str(runtime_path.relative_to(project)), "another-task", 0xFFFFFFFF,
        purpose="supervisor-runtime", project_root=str(claimed_root), source_root=str(source_root),
    )
    marker = runtime / "owner.json"
    original = marker.read_bytes()
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    assert_runtime_needs_verification(project, manifest, runtime)
    assert marker.read_bytes() == original


@pytest.mark.parametrize("purpose", ["", "another-purpose"], ids=["empty-purpose", "wrong-purpose"])
def test_resource_command_reports_invalid_purpose_in_configured_independent_project(
    project: Path, monkeypatch: pytest.MonkeyPatch, purpose: str,
) -> None:
    nested = create_independent_project(project)
    runtime_path = nested / ".scratch/runtime/myrk-run-other"
    runtime, _ = runtime_owner(
        project, str(runtime_path.relative_to(project)), "another-task", 0xFFFFFFFF,
        purpose=purpose, project_root=str(nested), source_root=str(nested),
    )
    monkeypatch.setenv("MYRK_RUNTIME_ROOT", str(runtime.parent))
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    assert_runtime_needs_verification(project, manifest, runtime, reason="Invalid runtime purpose.")


@pytest.mark.parametrize(
    ("separate_git_dir", "override_git"), [(False, False), (True, False), (True, True)],
    ids=["standard-metadata", "separate-metadata", "separate-metadata-with-environment-override"],
)
@pytest.mark.parametrize("use_primary_root", [True, False], ids=["primary-claim", "linked-claim"])
def test_resource_command_excludes_independent_runtime_with_a_registered_linked_source(
    project: Path, monkeypatch: pytest.MonkeyPatch, separate_git_dir: bool, override_git: bool, use_primary_root: bool,
) -> None:
    nested = create_independent_project(project, separate_git_dir=separate_git_dir)
    if separate_git_dir and use_primary_root:
        bind_separate_primary_worktree(nested)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    with (
        registered_worktrees(nested, "linked", separate_git_dir=separate_git_dir) as (linked,),
        monkeypatch.context() as scan_env,
    ):
        claimed_root = nested if use_primary_root else linked
        runtime, _ = runtime_owner(
            project, str(claimed_root.relative_to(project) / ".scratch/runtime/myrk-run-other"),
            "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(claimed_root), source_root=str(linked),
        )
        marker = runtime / "owner.json"
        original = marker.read_bytes()
        if override_git:
            scan_env.setenv("GIT_DIR", str(project / ".git"))
            scan_env.setenv("GIT_WORK_TREE", str(project))

        assert_runtime_is_excluded(project, manifest, marker, original)


@pytest.mark.parametrize("separate_git_dir", [False, True], ids=["standard-metadata", "bound-separate-metadata"])
def test_resource_command_excludes_independent_linked_runtime_with_a_primary_source(
    project: Path, separate_git_dir: bool,
) -> None:
    nested = create_independent_project(project, separate_git_dir=separate_git_dir)
    if separate_git_dir:
        bind_separate_primary_worktree(nested)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    with registered_worktrees(nested, "linked", separate_git_dir=separate_git_dir) as (linked,):
        runtime, _ = runtime_owner(
            project, str(linked.relative_to(project) / ".scratch/runtime/myrk-run-other"),
            "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(linked), source_root=str(nested),
        )
        marker = runtime / "owner.json"
        original = marker.read_bytes()

        assert_runtime_is_excluded(project, manifest, marker, original)


def test_resource_command_accepts_source_copies_inside_their_checkouts(project: Path) -> None:
    audit_source = project / ".scratch/source-copy"
    for directory in ("scripts", "myrk"):
        shutil.copytree(project / directory, audit_source / directory)
    nested = create_independent_project(project)
    recorded_source = nested / "source-copy"
    recorded_source.mkdir()
    runtime, _ = runtime_owner(
        project, str(nested.relative_to(project) / ".scratch/runtime/myrk-run-other"),
        "another-task", 0xFFFFFFFF,
        purpose="supervisor-runtime", project_root=str(nested), source_root=str(recorded_source),
    )
    marker = runtime / "owner.json"
    original = marker.read_bytes()
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    assert_runtime_is_excluded(audit_source, manifest, marker, original, expected_project=project)


def test_runtime_scan_keeps_a_candidate_when_its_git_query_cannot_start(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = create_independent_project(project)
    runtime, _ = runtime_owner(
        project, str(nested.relative_to(project) / ".scratch/runtime/myrk-run-other"),
        "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(nested), source_root=str(nested),
    )

    def missing_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("Git executable unavailable")

    monkeypatch.setattr(subprocess, "run", missing_git)
    report = verify_runtime_ownership(
        project, project, scope=RuntimeScope.for_manifest("current", project_audit=False),
        registered_worktrees=set(), listed_paths=set(), listed_worktrees=set(),
    )

    row, = report.directories
    assert row["path"] == str(runtime) and row["ownership"] == "unresolved", report
    assert "Cannot inspect Git" in row["reason"] and str(runtime) in report.errors[0], report


@pytest.mark.parametrize("separate_git_dir", [False, True], ids=["standard-metadata", "separate-metadata"])
def test_resource_command_keeps_an_unregistered_copy_of_an_independent_worktree_pending(
    project: Path, separate_git_dir: bool,
) -> None:
    nested = create_independent_project(project, separate_git_dir=separate_git_dir)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    with registered_worktrees(nested, "linked", separate_git_dir=separate_git_dir) as (linked,):
        copied = linked.with_name("copied")
        copied.mkdir()
        (copied / ".git").write_bytes((linked / ".git").read_bytes())
        runtime, _ = runtime_owner(
            project, str(nested.relative_to(project) / ".scratch/runtime/myrk-run-other"),
            "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(nested), source_root=str(copied),
        )
        marker = runtime / "owner.json"
        original = marker.read_bytes()

        assert_runtime_needs_verification(project, manifest, runtime, reason="not registered")
        assert marker.read_bytes() == original


def test_resource_command_keeps_a_source_with_another_worktrees_git_directory_pending(project: Path) -> None:
    nested = create_independent_project(project)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    with registered_worktrees(nested, "linked-a", "linked-b", separate_git_dir=False) as (other, source):
        runtime, _ = runtime_owner(
            project, str(nested.relative_to(project) / ".scratch/runtime/myrk-run-other"),
            "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(nested), source_root=str(source),
        )
        marker = runtime / "owner.json"
        original = marker.read_bytes()
        pointer = source / ".git"
        original_pointer = pointer.read_bytes()

        try:
            with pointer.open("r+b") as stream:
                stream.write((other / ".git").read_bytes())
                stream.truncate()
            assert_runtime_needs_verification(project, manifest, runtime)
            assert marker.read_bytes() == original
        finally:
            with pointer.open("r+b") as stream:
                stream.write(original_pointer)
                stream.truncate()


def test_resource_command_keeps_a_copy_of_the_separate_primary_git_pointer_pending(project: Path) -> None:
    nested = create_independent_project(project, separate_git_dir=True)
    copied = nested.with_name("unregistered")
    copied.mkdir()
    (copied / ".git").write_bytes((nested / ".git").read_bytes())
    runtime, _ = runtime_owner(
        project, str(nested.relative_to(project) / ".scratch/runtime/myrk-run-other"),
        "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(nested), source_root=str(copied),
    )
    marker = runtime / "owner.json"
    original = marker.read_bytes()
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})

    assert_runtime_needs_verification(project, manifest, runtime)
    assert marker.read_bytes() == original


@pytest.mark.parametrize("copy_primary_pointer", [False, True], ids=["unbound-primary", "copied-primary-pointer"])
def test_resource_command_keeps_an_unbound_separate_primary_claim_with_a_linked_source_pending(
    project: Path, copy_primary_pointer: bool,
) -> None:
    nested = create_independent_project(project, separate_git_dir=True)
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    with registered_worktrees(nested, "linked", separate_git_dir=True) as (linked,):
        claimed_root = nested
        if copy_primary_pointer:
            claimed_root = nested.with_name("unregistered")
            claimed_root.mkdir()
            (claimed_root / ".git").write_bytes((nested / ".git").read_bytes())
        runtime, _ = runtime_owner(
            project, str(claimed_root.relative_to(project) / ".scratch/runtime/myrk-run-other"),
            "another-task", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(claimed_root), source_root=str(linked),
        )
        marker = runtime / "owner.json"
        original = marker.read_bytes()

        assert_runtime_needs_verification(project, manifest, runtime)
        assert marker.read_bytes() == original


@pytest.mark.parametrize("separate_git_dir", [False, True], ids=["standard-metadata", "separate-metadata"])
def test_resource_command_keeps_a_linked_runtime_in_the_same_project_audit(
    project: Path, separate_git_dir: bool,
) -> None:
    if separate_git_dir:
        subprocess.run(
            ["git", "-C", str(project), "init", "--quiet", "--template=", "--separate-git-dir", str(project / "git-metadata")],
            check=True, capture_output=True, timeout=25,
        )
    manifest = write_json(project / "resources.json", {"project_root": str(project), "task_id": "current"})
    with registered_worktrees(project, "linked", separate_git_dir=separate_git_dir) as (linked,):
        runtime, _ = runtime_owner(
            project, str(linked.relative_to(project) / ".scratch/runtime/myrk-run-current"),
            "current", 0xFFFFFFFF, purpose="supervisor-runtime", project_root=str(linked), source_root=str(linked),
        )
        marker = runtime / "owner.json"
        original = marker.read_bytes()

        for argument, ownership in ((str(manifest), "task"), ("--project", "project")):
            status, report = run_command(project, argument)
            assert status == 1 and len(report["errors"]) == 1 and str(runtime) in report["errors"][0], report
            row, = report["discovered_resources"]
            assert row["path"] == str(runtime) and row["ownership"] == ownership, report
            assert row["state"] == "exited" and row["task_id"] == "current", report
        assert marker.read_bytes() == original
