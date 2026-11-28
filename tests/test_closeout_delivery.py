"""Check delivered files against Git commits and staged content."""

import json
from pathlib import Path

import pytest

from tests.test_closeout import cli, git, manifest, repo, snapshot


@pytest.mark.parametrize("outcome", ["removed", "committed"])
def test_source_deletion_must_also_exist_in_the_target_commit(repo: Path, outcome: str) -> None:
    baseline = snapshot(repo)
    (repo / "source.txt").unlink()
    entry = {"path": "source.txt", "outcome": outcome}
    if outcome == "committed":
        entry["deletion"] = True
    resource = manifest(repo, baseline, [entry])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    git(repo, "add", "source.txt")
    git(repo, "commit", "-m", "Remove source")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document


def test_target_commit_must_descend_from_the_baseline_start(repo: Path) -> None:
    baseline = snapshot(repo)
    old_tree = git(repo, "rev-parse", "HEAD^{tree}")
    unrelated = git(repo, "commit-tree", old_tree, "-m", "Unrelated root commit")
    git(repo, "update-ref", "refs/heads/main", unrelated)
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    assert any("ancestor" in error for error in document["errors"])


def test_closeout_control_files_must_be_committed_even_when_ignored_outside_scopes(repo: Path) -> None:
    baseline = snapshot(repo)
    resource = repo / ".closeout/resources.json"
    resource.write_text(json.dumps({"project_root": str(repo), "closeout": {"baseline": str(baseline),
        "task_id": "task-1", "target_ref": "refs/heads/main", "files": []}}), encoding="utf-8")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    assert all(any(str(path) in error for error in document["errors"]) for path in (baseline, resource))

    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document
    correct = resource.read_text(encoding="utf-8")
    for path in (baseline, resource):
        for outcome, deletion in (("retained_local", False), ("external", False), ("committed", True), ("removed", False)):
            declared = json.loads(correct)
            entry = next(entry for entry in declared["closeout"]["files"] if Path(entry["path"]) == path)
            entry.update(outcome=outcome, deletion=deletion, owner="task-1", reason="Still needed",
                         delete_when="Fixture teardown", category="dependency")
            resource.write_text(json.dumps(declared), encoding="utf-8")
            git(repo, "--literal-pathspecs", "commit", "--only", "-m", "Declare control-file disposition", "--",
                str(baseline), str(resource))
            result, document = cli(repo, "check", str(resource))
            assert result.returncode != 0, document
            assert any(str(path) in error and "Required closeout" in error for error in document["errors"]), document


@pytest.mark.parametrize("outcome", ["committed", "removed"])
def test_delivered_files_require_the_target_index_version_while_external_staging_is_preserved(repo: Path, outcome: str) -> None:
    (repo / "other.txt").write_text("Another task's staged file\n", encoding="utf-8")
    git(repo, "add", "other.txt")
    baseline = snapshot(repo)
    original_blob = git(repo, "rev-parse", "HEAD:source.txt")
    source = repo / "source.txt"
    if outcome == "committed":
        source.write_text("Delivered content\n", encoding="utf-8")
    else:
        source.unlink()
    git(repo, "commit", "--only", "-m", "Deliver source disposition", "--", "source.txt")
    resource = manifest(repo, baseline, [
        {"path": "source.txt", "outcome": outcome},
        {"path": "other.txt", "outcome": "external", "owner": "other-task", "reason": "Other active staged work"}])
    if outcome == "committed":
        source.write_text("New staged version\n", encoding="utf-8")
        git(repo, "add", "source.txt")
        git(repo, "restore", "--worktree", "--source=HEAD", "--", "source.txt")
    else:
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{original_blob},source.txt")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    assert any("source.txt" in error and "index" in error.lower() for error in document["errors"]), document
    git(repo, "restore", "--staged", "--source=HEAD", "--", "source.txt")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document


def test_explicit_root_uses_its_own_checkout_and_index_despite_foreign_git_environment(repo: Path) -> None:
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [{"path": "source.txt", "outcome": "committed"}])
    foreign = repo.parent / "foreign"
    foreign.mkdir()
    git(foreign, "init", "-b", "main")
    (foreign / "foreign.txt").write_text("Foreign checkout\n", encoding="utf-8")
    git(foreign, "add", "foreign.txt")
    git(foreign, "-c", "user.name=Other task", "-c", "user.email=other@example.invalid", "commit", "-m", "Foreign fixture")
    environment = {"GIT_DIR": str(foreign / ".git"), "GIT_COMMON_DIR": str(foreign / ".git"),
                   "GIT_WORK_TREE": str(foreign), "GIT_INDEX_FILE": str(foreign / ".git/index"),
                   "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects")}
    result, document = cli(repo, "snapshot", "--task-id", "task-1", "--target-ref", "refs/heads/main", extra_env=environment)
    assert result.returncode == 0, document
    assert Path(document["source_root"]) == repo and Path(document["project_root"]) == repo
    (repo / "source.txt").write_text("A staged change in the real checkout\n", encoding="utf-8")
    git(repo, "add", "source.txt")
    git(repo, "restore", "--worktree", "--source=HEAD", "--", "source.txt")
    result, document = cli(repo, "check", str(resource), extra_env=environment)
    assert result.returncode != 0, document
    assert any("source.txt" in error and "index" in error.lower() for error in document["errors"]), document
    git(repo, "restore", "--staged", "--source=HEAD", "--", "source.txt")
    result, document = cli(repo, "check", str(resource), extra_env=environment)
    assert result.returncode == 0, document


@pytest.mark.parametrize("outcome", ["committed", "removed"])
def test_worktree_closeout_checks_common_checkout_index_without_disturbing_external_staging(repo: Path, outcome: str) -> None:
    source = repo / ".scratch/worktrees/delivery"
    git(repo, "worktree", "add", "-b", "delivery", str(source))
    (repo / "other.txt").write_text("Other task's staged common-checkout file\n", encoding="utf-8")
    git(repo, "add", "other.txt")
    baseline = snapshot(source, target_ref="refs/heads/delivery")
    original_blob = git(repo, "rev-parse", "HEAD:source.txt")
    files = [{"path": str(repo / "source.txt"), "outcome": outcome},
             {"path": str(repo / "other.txt"), "outcome": "external", "owner": "other-task", "reason": "Other staged work"}]
    if outcome == "removed":
        (source / "source.txt").unlink()
        git(source, "commit", "--only", "-m", "Remove delivered source", "--", "source.txt")
        git(repo, "rm", "--", "source.txt")
        files.append({"path": str(source / "source.txt"), "outcome": "committed", "deletion": True})
    resource = manifest(source, baseline, files)
    result, document = cli(source, "check", str(resource))
    assert result.returncode == 0, document
    if outcome == "committed":
        (repo / "source.txt").write_text("Different staged common-checkout content\n", encoding="utf-8")
        git(repo, "add", "source.txt")
        git(repo, "restore", "--worktree", "--source=refs/heads/delivery", "--", "source.txt")
    else:
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{original_blob},source.txt")
    result, document = cli(source, "check", str(resource))
    assert result.returncode != 0, document
    assert any(str(repo / "source.txt") in error and "index" in error.lower() for error in document["errors"]), document
    git(repo, "restore", "--staged", "--source=refs/heads/delivery", "--", "source.txt")
    result, document = cli(source, "check", str(resource))
    assert result.returncode == 0, document


def test_preflight_checks_staged_delivery_without_claiming_target_completion(repo: Path) -> None:
    baseline = snapshot(repo)
    delivery = repo / "pending delivery.txt"
    delivery.write_bytes(b"pending delivery\r\n")
    git(repo, "add", "pending delivery.txt")
    resource = manifest(repo, baseline, [{"path": "pending delivery.txt", "outcome": "committed"}])
    before_index = (repo / ".git/index").read_bytes()
    before_head = git(repo, "rev-parse", "HEAD")

    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode == 0, document
    assert document["phase"] == "preflight"
    assert document["final_verification_required"] is True
    assert document["unclassified"] == []
    assert any(Path(entry["path"]) == delivery for entry in document["candidates"])
    assert (repo / ".git/index").read_bytes() == before_index
    assert git(repo, "rev-parse", "HEAD") == before_head

    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    git(repo, "commit", "--only", "-m", "Deliver staged text", "--", "pending delivery.txt")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document


def test_preflight_lists_unknown_reports_and_rejects_evidence_changed_after_staging(repo: Path) -> None:
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [])
    report = repo / ".scratch/check-runs/results/new.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"result": "first"}\n', encoding="utf-8")
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert str(report) in document["unclassified"]
    candidate = next(entry for entry in document["candidates"] if Path(entry["path"]) == report)
    assert candidate["declaration"] is None

    facts = json.loads(resource.read_text(encoding="utf-8"))
    facts["closeout"]["files"].append({"path": str(report), "outcome": "committed"})
    resource.write_text(json.dumps(facts), encoding="utf-8")
    git(repo, "add", "-f", str(resource), str(report))
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode == 0, document
    report.write_text('{"result": "changed after staging"}\n', encoding="utf-8")
    before_index = (repo / ".git/index").read_bytes()
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert any(str(report) in error and "Index content" in error for error in document["errors"]), document
    assert (repo / ".git/index").read_bytes() == before_index


def test_removed_directory_cannot_hide_files_still_present_in_the_target_tree(repo: Path) -> None:
    directory = repo / "delivered directory"
    directory.mkdir()
    child = directory / "child.txt"
    child.write_text("still in the target\n", encoding="utf-8")
    git(repo, "add", "delivered directory")
    git(repo, "commit", "-m", "Deliver directory")
    baseline = snapshot(repo)
    child.unlink()
    directory.rmdir()
    resource = manifest(repo, baseline, [{"path": "delivered directory", "outcome": "removed"},
                                        {"path": str(child), "outcome": "removed"}])
    for phase in ("check", "preflight"):
        result, document = cli(repo, phase, str(resource))
        assert result.returncode != 0, document
        assert any(str(directory) in error and "Removed path" in error for error in document["errors"]), document


def test_batch_verification_reports_only_the_changed_mixed_delivery(repo: Path) -> None:
    baseline = snapshot(repo)
    contents = {"中文 文件.txt": b"text\r\n", "[draft].txt": b"draft\n", "binary.bin": b"\x00\xff\x01"}
    for name, content in contents.items():
        (repo / name).write_bytes(content)
        git(repo, "--literal-pathspecs", "add", "--", name)
    git(repo, "commit", "-m", "Deliver mixed files")
    resource = manifest(repo, baseline, [{"path": name, "outcome": "committed"} for name in contents])
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document
    (repo / "[draft].txt").write_text("changed\n", encoding="utf-8")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    assert len(document["errors"]) == 1, document
    assert "[draft].txt" in document["errors"][0]


def test_common_checkout_content_keeps_the_source_checkouts_attribute_rules(repo: Path) -> None:
    (repo / ".gitattributes").write_text("source.txt text eol=lf\n", encoding="utf-8")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "Normalize source text")
    source = repo / ".scratch/worktrees/delivery"
    git(repo, "worktree", "add", "-b", "delivery", str(source))
    baseline = snapshot(source, target_ref="refs/heads/delivery")
    (source / ".gitattributes").write_text("source.txt -text\n", encoding="utf-8")
    git(source, "add", ".gitattributes")
    git(source, "commit", "-m", "Keep delivery source bytes")
    (repo / "source.txt").write_bytes(b"original\r\n")
    resource = manifest(source, baseline, [
        {"path": str(source / ".gitattributes"), "outcome": "committed"},
        {"path": str(repo / "source.txt"), "outcome": "committed"}])
    result, document = cli(source, "check", str(resource))
    assert result.returncode != 0, document
    assert any(str(repo / "source.txt") in error and "content" in error.lower() for error in document["errors"]), document
    (repo / "source.txt").write_bytes(b"original\n")
    result, document = cli(source, "check", str(resource))
    assert result.returncode == 0, document


def test_successful_preflight_does_not_exempt_receipts_created_afterwards(repo: Path) -> None:
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode == 0, document
    receipt = repo / ".scratch/check-runs/results/hook-result.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}\n", encoding="utf-8")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    assert any(str(receipt) in error for error in document["errors"]), document


def test_preflight_observes_committed_branch_changes_before_target_integration(repo: Path) -> None:
    baseline = snapshot(repo)
    git(repo, "checkout", "-b", "implementation")
    delivery = repo / "branch-evidence.txt"
    delivery.write_text("Already committed on the implementation branch\n", encoding="utf-8")
    git(repo, "add", "branch-evidence.txt")
    git(repo, "commit", "-m", "Save branch evidence")
    resource = manifest(repo, baseline, [])
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert str(delivery) in document["unclassified"]
    facts = json.loads(resource.read_text(encoding="utf-8"))
    facts["closeout"]["files"].append({"path": str(delivery), "outcome": "committed"})
    resource.write_text(json.dumps(facts), encoding="utf-8")
    git(repo, "add", "-f", str(resource))
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode == 0, document
    result, document = cli(repo, "check", str(resource))
    assert result.returncode != 0, document
    assert any("target" in error.lower() for error in document["errors"]), document


@pytest.mark.parametrize("contents", [None, "{}", "not json"])
def test_preflight_structural_failures_keep_the_machine_readable_output_contract(repo: Path, contents: str | None) -> None:
    resource = repo / "invalid-manifest.json"
    if contents is not None:
        resource.write_text(contents, encoding="utf-8")
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert document["errors"]
    assert document["phase"] == "preflight"
    assert document["final_verification_required"] is True
    assert document["candidates"] == []
    assert document["unclassified"] == []
