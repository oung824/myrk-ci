"""Check archived originals and staged changes across linked worktrees."""

from pathlib import Path

import pytest

from tests.test_closeout import cli, snapshot
from tests.test_closeout_hook import CONTROL_NAMES, commit, preflight_result, write_manifest
from tests.test_pre_commit import git, repository


@pytest.mark.parametrize("target_deletes", [False, True], ids=["target-updates", "target-deletes"])
@pytest.mark.parametrize("in_worktree", [False, True], ids=["shared-root", "linked-worktree"])
def test_linked_worktree_rejects_old_original_after_target_changes(
    repository: Path, target_deletes: bool, in_worktree: bool,
) -> None:
    name = "archived-original.txt"
    target_original = repository / name
    old_bytes = b"Original at branch split\r\n"
    new_bytes = b"Original updated on target\r\n"
    target_original.write_bytes(old_bytes)
    (repository / ".gitattributes").write_bytes(f"/{name} -text -diff\n".encode())
    added = git(repository, "add", "--", name, ".gitattributes")
    assert added.returncode == 0, added.stderr
    archived = commit(repository)
    assert archived.returncode == 0, archived.stdout + archived.stderr

    worktree = repository / ".scratch/worktrees/delivery"
    created = git(repository, "worktree", "add", "-b", "delivery", str(worktree))
    assert created.returncode == 0, created.stderr
    ref = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    baseline = snapshot(worktree, "--task-files", "--scope", ".closeout", "--scope", "delivery.txt", target_ref=ref)
    original = worktree / name if in_worktree else target_original
    if target_deletes:
        staged = git(repository, "rm", "--", name)
        expected = f"Committed content is missing: {original}"
    else:
        target_original.write_bytes(new_bytes)
        staged = git(repository, "add", "--", name)
        expected = f"Index content differs from original bytes: {original}"
    assert staged.returncode == 0, staged.stderr
    updated = commit(repository)
    assert updated.returncode == 0, updated.stdout + updated.stderr
    original.write_bytes(old_bytes)

    delivery = worktree / "delivery.txt"
    delivery.write_bytes(b"New delivery\n")
    added = git(worktree, "add", "--", delivery.name)
    assert added.returncode == 0, added.stderr
    resources = write_manifest(worktree, baseline, [
        {"path": str(delivery), "outcome": "committed"},
        {"path": str(original), "outcome": "committed", "preserve_bytes": True},
    ])
    head = git(worktree, "rev-parse", "HEAD").stdout
    source_index = git(worktree, "rev-parse", ":" + name).stdout

    rejected = commit(worktree, delivery.name, *CONTROL_NAMES, manifest=resources)

    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    report = preflight_result(rejected)
    assert report["errors"] == [expected], report
    assert git(worktree, "rev-parse", "HEAD").stdout == head

    if not target_deletes:
        original.write_bytes(new_bytes)
        delivered = commit(worktree, delivery.name, *CONTROL_NAMES, manifest=resources)
        assert delivered.returncode == 0, delivered.stdout + delivered.stderr
        assert preflight_result(delivered)["ok"] is True
        assert git(worktree, "rev-parse", ":" + name).stdout == source_index
        merged = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                     "merge", "--no-edit", "delivery")
        assert merged.returncode == 0, merged.stderr
        checked, report = cli(worktree, "check", str(resources), "--commit", ref)
        assert checked.returncode == 0, report

        if in_worktree:
            aligned = git(worktree, "add", "--", name)
            assert aligned.returncode == 0, aligned.stderr
            assert git(worktree, "diff", "--cached", "--name-only", "--", name).stdout.strip() == name
            checked, report = cli(worktree, "check", str(resources), "--commit", ref)
            assert checked.returncode == 0, report


@pytest.mark.parametrize("delete", [False, True], ids=["staged-edit", "staged-delete"])
@pytest.mark.parametrize("in_worktree", [False, True], ids=["shared-root", "linked-worktree"])
def test_linked_worktree_final_check_rejects_newly_staged_original_changes(
    repository: Path, delete: bool, in_worktree: bool,
) -> None:
    name = "archived-original.txt"
    payload = b"Original evidence\r\n"
    (repository / name).write_bytes(payload)
    (repository / ".gitattributes").write_bytes(f"/{name} -text -diff\n".encode())
    added = git(repository, "add", "--", name, ".gitattributes")
    assert added.returncode == 0, added.stderr
    archived = commit(repository)
    assert archived.returncode == 0, archived.stdout + archived.stderr

    worktree = repository / ".scratch/worktrees/delivery"
    created = git(repository, "worktree", "add", "-b", "delivery", str(worktree))
    assert created.returncode == 0, created.stderr
    baseline = snapshot(worktree, "--task-files", "--scope", ".closeout", target_ref="refs/heads/delivery")
    checkout = worktree if in_worktree else repository
    original = checkout / name
    resources = write_manifest(worktree, baseline, [{"path": str(original), "outcome": "committed", "preserve_bytes": True}])
    delivered = commit(worktree, *CONTROL_NAMES, manifest=resources)
    assert delivered.returncode == 0, delivered.stdout + delivered.stderr
    checked, report = cli(worktree, "check", str(resources), "--commit", "HEAD")
    assert checked.returncode == 0, report

    if delete:
        staged = git(checkout, "rm", "--cached", "--", name)
    else:
        original.write_bytes(b"Different staged evidence\r\n")
        staged = git(checkout, "add", "--", name)
        original.write_bytes(payload)
    assert staged.returncode == 0, staged.stderr
    assert original.read_bytes() == payload

    checked, report = cli(worktree, "check", str(resources), "--commit", "HEAD")

    assert checked.returncode == 1, report
    assert report["errors"] == [f"Index differs from the target commit: {original}"], report
    assert git(checkout, "diff", "--cached", "--name-only", "--", name).stdout.strip() == name
