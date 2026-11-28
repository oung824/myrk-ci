"""Task file closeout is checked through its read-only command interface."""

import json
import ctypes
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
GIT_LOCAL_ENV = subprocess.run(["git", "rev-parse", "--local-env-vars"], check=True, capture_output=True,
                               text=True, encoding="utf-8").stdout.splitlines()
ENV = {**{key: value for key, value in os.environ.items() if key not in GIT_LOCAL_ENV},
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *arguments], env=ENV, capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Closeout test")
    git(root, "config", "user.email", "closeout@example.invalid")
    git(root, "config", "core.autocrlf", "true")
    (root / ".gitignore").write_text(".scratch/\n.closeout/\n", encoding="utf-8")
    (root / "source.txt").write_text("original\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial source")
    yield root
    for repository in (root, root.parent / "foreign"):
        for path in (repository / ".git" / "objects").rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IWRITE | stat.S_IREAD)


def cli(root: Path, *arguments: str, extra_env: dict[str, str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/check_closeout.py"),
                             *arguments, "--root", str(root)], env={**ENV, **(extra_env or {})}, capture_output=True,
                            text=True, encoding="utf-8", timeout=30)
    assert not result.stderr, result.stderr
    return result, json.loads(result.stdout)


def snapshot(root: Path, *arguments: str, target_ref: str = "refs/heads/main") -> Path:
    result, document = cli(root, "snapshot", "--task-id", "task-1", "--target-ref", target_ref, *arguments)
    assert result.returncode == 0, document
    destination = root / ".closeout" / "baseline.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(document), encoding="utf-8")
    return destination


def manifest(root: Path, baseline: Path, files: list[dict]) -> Path:
    destination = root / ".closeout" / "resources.json"
    facts = json.loads(baseline.read_text(encoding="utf-8"))
    controls = [{"path": str(path), "outcome": "committed"} for path in (baseline, destination)]
    destination.write_text(json.dumps({"project_root": facts["project_root"], "paths": [], "closeout": {
        "baseline": str(baseline), "task_id": "task-1", "target_ref": facts["target_ref"], "files": [*files, *controls]}}), encoding="utf-8")
    git(root, "--literal-pathspecs", "add", "-f", "--", str(baseline), str(destination))
    git(root, "--literal-pathspecs", "commit", "--only", "-m", "Save closeout controls", "--", str(baseline), str(destination))
    return destination


def test_resource_cleanup_passes_but_uncommitted_report_prevents_closeout(repo: Path) -> None:
    source = repo / ".closeout" / "resource-checker"
    (source / "scripts").mkdir(parents=True)
    (source / "myrk").mkdir()
    for relative in ("scripts/check_resources.py", "myrk/project_paths.py", "myrk/runtime_ownership.py"):
        shutil.copyfile(ROOT / relative, source / relative)
    baseline = snapshot(repo, "--scope", ".scratch/task")
    report = repo / ".scratch/task/report.md"
    report.parent.mkdir(parents=True)
    report.write_text("Required delivery evidence\n", encoding="utf-8")
    resource = manifest(repo, baseline, [])
    result = subprocess.run([sys.executable, "-B", str(source / "scripts/check_resources.py"), str(resource)],
                            cwd=repo, env=ENV, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    result, document = cli(repo, "check", str(resource), "--commit", "HEAD")
    assert result.returncode != 0
    assert any("report.md" in error for error in document["errors"])


@pytest.mark.parametrize("name", ["delivery.txt", "[draft].txt"])
def test_committed_file_requires_the_target_blob_to_match_local_text(repo: Path, name: str) -> None:
    baseline = snapshot(repo)
    delivery = repo / name
    delivery.write_bytes(b"delivered\r\n")
    git(repo, "--literal-pathspecs", "add", name)
    git(repo, "commit", "-m", "Deliver text")
    resource = manifest(repo, baseline, [{"path": name, "outcome": "committed"}])
    result, document = cli(repo, "check", str(resource), "--commit", "HEAD")
    assert result.returncode == 0, document
    delivery.write_text("different content\n", encoding="utf-8")
    result, document = cli(repo, "check", str(resource), "--commit", "HEAD")
    assert result.returncode != 0
    assert any(name in error and "content" in error.lower() for error in document["errors"])


def test_changes_already_committed_since_baseline_still_need_a_disposition(repo: Path) -> None:
    baseline = snapshot(repo)
    (repo / "delivery.txt").write_text("delivery\n", encoding="utf-8")
    git(repo, "add", "delivery.txt")
    git(repo, "commit", "-m", "Deliver text")
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0
    assert any("delivery.txt" in error for error in document["errors"])


def test_checked_commit_must_be_the_declared_target_refs_current_tip(repo: Path) -> None:
    baseline = snapshot(repo)
    git(repo, "checkout", "-b", "other")
    git(repo, "commit", "--allow-empty", "-m", "Other branch")
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "check", str(resource), "--commit", "HEAD")
    assert result.returncode != 0
    assert any("target" in error.lower() for error in document["errors"])


def test_external_change_cannot_hide_new_content_behind_the_same_git_status(repo: Path) -> None:
    path = repo / "source.txt"
    path.write_text("Other task's existing change\n", encoding="utf-8")
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [{"path": "source.txt", "outcome": "external",
        "owner": "other-task", "reason": "Another active task owns this change"}])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document
    path.write_text("An additional unacknowledged change\n", encoding="utf-8")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0
    assert any("source.txt" in error for error in document["errors"])


def test_custom_clean_filter_is_rejected_before_git_can_execute_it(repo: Path) -> None:
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [])
    (repo / ".git/info/attributes").write_text("source.txt filter=writer\n", encoding="utf-8")
    git(repo, "config", "filter.writer.clean", "echo executed > filter-marker.txt")
    (repo / "source.txt").write_text("changed content\n", encoding="utf-8")
    for arguments in (("snapshot", "--task-id", "task-1", "--target-ref", "refs/heads/main"),
                      ("check", str(resource)), ("preflight", str(resource))):
        result, document = cli(repo, *arguments)
        assert result.returncode != 0, document
        assert any("filter" in error.lower() for error in document["errors"])
        assert not (repo / "filter-marker.txt").exists()


def test_shared_results_detect_new_receipts_without_reclassifying_committed_history(repo: Path) -> None:
    results = repo / ".scratch/check-runs/results"
    results.mkdir(parents=True)
    (results / "old.json").write_text("{}", encoding="utf-8")
    git(repo, "add", "-f", ".scratch/check-runs/results/old.json")
    git(repo, "commit", "-m", "Save old receipt")
    baseline = snapshot(repo)
    facts = json.loads(baseline.read_text(encoding="utf-8"))
    assert not facts["files"]
    (results / "new.json").write_text("{}", encoding="utf-8")
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0
    assert any("new.json" in error for error in document["errors"])
    assert not any("old.json" in error for error in document["errors"])


@pytest.mark.parametrize("changes", [
    {"outcome": "unknown"},
    {"outcome": "external", "owner": "", "reason": "Other work"},
    {"outcome": "retained_local", "category": "report"},
    {"outcome": "retained_local", "category": "dependency", "required_evidence": True},
    {"outcome": "retained_local", "category": "duplicate_evidence", "equivalent_evidence": "missing.txt"},
])
def test_invalid_file_disposition_never_reports_completion(repo: Path, changes: dict) -> None:
    path = repo / "extra.txt"
    path.write_text("local artifact\n", encoding="utf-8")
    baseline = snapshot(repo)
    entry = {"path": "extra.txt", "outcome": "retained_local", "owner": "task-1", "reason": "Needed",
             "delete_when": "When task ends", **changes}
    resource = manifest(repo, baseline, [entry])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document


def test_approved_timing_index_can_be_retained_with_an_explicit_fingerprint(repo: Path) -> None:
    index = repo / ".scratch/check-runs/timings/backend.json"
    index.parent.mkdir(parents=True)
    index.write_text("{}\n", encoding="utf-8")
    baseline = snapshot(repo, "--scope", ".scratch/check-runs/timings")
    resource = manifest(repo, baseline, [{
        "path": str(index), "outcome": "retained_local", "category": "timing_index",
        "owner": "test-maintainer", "reason": "Approved bounded scheduling estimates",
        "delete_when": "Scheduling estimates are no longer needed",
    }])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document


@pytest.mark.parametrize("relative,size", [
    (".scratch/check-runs/timings/notes.json", 3),
    (".scratch/other/backend.json", 3),
    (".scratch/check-runs/timings/backend.json", 2 * 1024 * 1024 + 1),
])
def test_timing_retention_does_not_cover_other_files_or_oversized_indexes(
    repo: Path, relative: str, size: int,
) -> None:
    index = repo / relative
    index.parent.mkdir(parents=True)
    index.write_bytes(b" " * size)
    baseline = snapshot(repo, "--scope", relative)
    resource = manifest(repo, baseline, [{
        "path": str(index), "outcome": "retained_local", "category": "timing_index",
        "owner": "test-maintainer", "reason": "Scheduling estimates",
        "delete_when": "Estimates are no longer needed",
    }])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0
    assert any("Timing retention" in error and str(index) in error for error in document["errors"])


def test_metadata_only_credentials_are_observed_without_reading_and_neighbors_remain_visible(repo: Path) -> None:
    secret = repo / "credential.txt"
    secret.write_text("fixture credential\n", encoding="utf-8")
    git(repo, "add", "credential.txt")
    git(repo, "commit", "-m", "Synthetic credential fixture")
    secret.write_text("fixture credential updated before snapshot\n", encoding="utf-8")
    (repo / "neighbor.txt").write_text("other task\n", encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
                                  ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(secret), 0x80000000, 0, None, 3, 128, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        baseline = snapshot(repo, "--metadata-only", "credential.txt")
        records = {Path(entry["path"]).name: entry for entry in json.loads(baseline.read_text(encoding="utf-8"))["files"]}
        assert records["credential.txt"]["metadata_only"] is True
        assert "sha256" not in records["credential.txt"]
        assert records["neighbor.txt"]["git_status"] == "??"
        kernel.CloseHandle(handle)
        handle = None
        resource = manifest(repo, baseline, [
            {"path": "credential.txt", "outcome": "retained_local", "category": "credential", "owner": "task-1",
             "reason": "Local synthetic credential", "delete_when": "Fixture teardown"},
            {"path": "neighbor.txt", "outcome": "external", "owner": "other-task", "reason": "Other active work"}])
        handle = kernel.CreateFileW(str(secret), 0x80000000, 0, None, 3, 128, None)
        assert handle != ctypes.c_void_p(-1).value
        result, document = cli(repo, "check", str(resource))
        assert result.returncode == 0, document
        assert "fixture credential" not in result.stdout
        result, document = cli(repo, "preflight", str(resource))
        assert result.returncode == 0, document
        assert "fixture credential" not in result.stdout
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)


@pytest.mark.parametrize("scope", [".", ".git", ".git/objects", "../outside"])
def test_scopes_cannot_sweep_the_checkout_git_database_or_outside_paths(repo: Path, scope: str) -> None:
    result, document = cli(repo, "snapshot", "--task-id", "task-1", "--target-ref", "refs/heads/main", "--scope", scope)
    assert result.returncode != 0, document


def test_directory_dispositions_do_not_exempt_new_children(repo: Path) -> None:
    scope = repo / ".scratch/task"
    scope.mkdir(parents=True)
    (scope / "old.txt").write_text("other task\n", encoding="utf-8")
    baseline = snapshot(repo, "--scope", ".scratch/task")
    (scope / "new.txt").write_text("new evidence\n", encoding="utf-8")
    resource = manifest(repo, baseline, [{"path": ".scratch/task", "outcome": "external", "owner": "other",
        "reason": "Other task's directory"}])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0
    assert any("new.txt" in error for error in document["errors"]), document


def test_linked_worktree_observes_common_project_changes_and_receipts(repo: Path) -> None:
    source = repo / ".scratch/worktrees/delivery"
    git(repo, "worktree", "add", "-b", "delivery", str(source))
    (repo / "source.txt").write_text("Other task in main checkout\n", encoding="utf-8")
    baseline = snapshot(source, target_ref="refs/heads/delivery")
    facts = json.loads(baseline.read_text(encoding="utf-8"))
    assert Path(facts["source_root"]) == source
    assert Path(facts["project_root"]) == repo
    assert any(Path(entry["path"]) == repo / "source.txt" for entry in facts["files"])
    results = repo / ".scratch/check-runs/results"
    results.mkdir(parents=True)
    (results / "receipt.json").write_text("{}", encoding="utf-8")
    resource = manifest(source, baseline, [{"path": str(repo / "source.txt"), "outcome": "external", "owner": "other",
        "reason": "Existing main checkout work"}])
    result, document = cli(source, "check", str(resource))
    assert result.returncode != 0
    assert any("receipt.json" in error for error in document["errors"]), document


def test_commands_leave_index_head_refs_objects_and_local_files_unchanged(repo: Path) -> None:
    (repo / "source.txt").write_text("Other task\n", encoding="utf-8")
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [{"path": "source.txt", "outcome": "external", "owner": "other",
        "reason": "Existing other task change"}])
    git(repo, "config", "core.fsmonitor", "echo executed > fsmonitor-marker.txt")
    before = {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    result, document = cli(repo, "snapshot", "--task-id", "task-1", "--target-ref", "refs/heads/main")
    assert result.returncode == 0, document
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode == 0, document
    after = {path.relative_to(repo): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    assert after == before


def test_existing_external_source_deletion_is_preserved(repo: Path) -> None:
    (repo / "source.txt").unlink()
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [{"path": "source.txt", "outcome": "external", "owner": "other",
        "reason": "Another task owns the existing deletion"}])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document


def test_shared_results_scope_is_mandatory_when_reading_a_baseline(repo: Path) -> None:
    baseline = snapshot(repo)
    document = json.loads(baseline.read_text(encoding="utf-8"))
    document["scopes"] = []
    baseline.write_text(json.dumps(document), encoding="utf-8")
    results = repo / ".scratch/check-runs/results"
    results.mkdir(parents=True)
    (results / "receipt.json").write_text("{}", encoding="utf-8")
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0
    assert any("receipt.json" in error for error in document["errors"])


def test_scope_rejects_a_junction_instead_of_reading_its_target(repo: Path) -> None:
    target = repo.parent / "outside"
    target.mkdir()
    (target / "protected.txt").write_text("outside fixture project\n", encoding="utf-8")
    link = repo / ".scratch/linked"
    link.parent.mkdir()
    result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    try:
        result, document = cli(repo, "snapshot", "--task-id", "task-1", "--target-ref", "refs/heads/main",
                               "--scope", ".scratch/linked")
        assert result.returncode != 0, document
        assert any("Linked" in error for error in document["errors"])
        assert (target / "protected.txt").read_text(encoding="utf-8") == "outside fixture project\n"
    finally:
        link.rmdir()
