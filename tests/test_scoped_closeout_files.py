"""Check the file boundary used by ordinary closeout."""

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess

from tests.test_closeout import cli, git, manifest, repo, snapshot
from tests.test_closeout_hook import CONTROL_NAMES, commit, preflight_result, write_manifest
from tests.test_pre_commit import git as hook_git, repository


@contextmanager
def linked_directory(link: Path, target: Path) -> Iterator[None]:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        yield
    finally:
        if link.is_symlink():
            link.unlink()
        else:
            link.rmdir()


def test_foreign_files_do_not_block_scoped_closeout(repo: Path) -> None:
    unrelated = repo / "source.txt"
    unrelated.write_text("Another task's edit at snapshot\n", encoding="utf-8")
    nested = repo / "foreign-history"
    nested.mkdir()
    git(nested, "init", "-b", "main")
    (nested / "output.txt").write_text("Another repository's output\n", encoding="utf-8")
    baseline = snapshot(repo, "--task-files", "--scope", ".closeout", "--verify-runs")
    facts = json.loads(baseline.read_text(encoding="utf-8"))
    assert facts["file_scope"] == "task"
    assert facts["runs_observed_at"]
    assert facts["files"] == []

    target = repo.parent / "foreign-dependency"
    target.mkdir()
    foreign_output = target / "output.txt"
    foreign_output.write_text("Other task's original output\n", encoding="utf-8")
    link = repo / "foreign-dependency"
    owned = repo / ".closeout/report.txt"
    owned.write_text("This task's evidence\n", encoding="utf-8")
    resources = manifest(repo, baseline, [{"path": ".closeout/report.txt", "outcome": "committed"}])
    git(repo, "add", "-f", "--", ".closeout/report.txt")

    with linked_directory(link, target):
        process, report = cli(repo, "preflight", str(resources))
        assert process.returncode == 0, report
        assert {item["path"] for item in report["candidates"]} == set(map(str, (baseline, resources, owned)))
        git(repo, "commit", "-m", "Deliver task report")
        unrelated.write_text("Another task's later edit\n", encoding="utf-8")
        foreign_output.write_text("Other task's later output\n", encoding="utf-8")
        process, report = cli(repo, "check", str(resources))
        assert process.returncode == 0, report


def test_scoped_closeout_rejects_undeclared_files_and_directory_links(repo: Path) -> None:
    baseline = snapshot(repo, "--task-files", "--scope", ".closeout")
    resources = manifest(repo, baseline, [])
    undeclared = baseline.with_name("undeclared.txt")
    undeclared.write_text("Unclassified task evidence\n", encoding="utf-8")
    for phase in ("preflight", "check"):
        process, report = cli(repo, phase, str(resources))
        assert process.returncode == 1, report
        assert any("Unclassified" in error and str(undeclared) in error for error in report["errors"]), report

    target = repo.parent / "private-dependency"
    target.mkdir()
    (target / "private.txt").write_text("External fixture\n", encoding="utf-8")
    link = baseline.with_name("linked")
    with linked_directory(link, target):
        for phase in ("preflight", "check"):
            process, report = cli(repo, phase, str(resources))
            assert process.returncode == 1, report
            assert any("Linked" in error and str(link) in error for error in report["errors"]), report


def test_scoped_commit_only_checks_all_proposed_files_and_preserves_other_staging(repository: Path) -> None:
    selected_name, unrelated_name = "selected.txt", "unrelated.txt"
    selected, unrelated = repository / selected_name, repository / unrelated_name
    selected.write_text("Original selected file\n", encoding="utf-8")
    unrelated.write_text("Original other task file\n", encoding="utf-8")
    staged = hook_git(repository, "add", "--", selected_name, unrelated_name)
    assert staged.returncode == 0, staged.stderr
    initial = commit(repository)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    ref = hook_git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(repository, "--task-files", "--scope", ".closeout", target_ref=ref)
    selected.write_text("Final selected file\n", encoding="utf-8")
    unrelated.write_text("Other task's staged edit\n", encoding="utf-8")
    staged = hook_git(repository, "add", "--", unrelated_name)
    assert staged.returncode == 0, staged.stderr
    resources = write_manifest(repository, baseline, [])

    rejected = commit(repository, selected_name, *CONTROL_NAMES, manifest=resources)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    report = preflight_result(rejected)
    assert report["unclassified"] == [str(selected)], report

    resources = write_manifest(repository, baseline, [{"path": selected_name, "outcome": "committed"}])
    accepted = commit(repository, selected_name, *CONTROL_NAMES, manifest=resources)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    report = preflight_result(accepted)
    assert str(unrelated) not in {item["path"] for item in report["candidates"]}
    assert hook_git(repository, "show", f"HEAD:{selected_name}").stdout == "Final selected file\n"
    assert hook_git(repository, "diff", "--cached", "--name-only").stdout.splitlines() == [unrelated_name]
    final, report = cli(repository, "check", str(resources))
    assert final.returncode == 0, report
