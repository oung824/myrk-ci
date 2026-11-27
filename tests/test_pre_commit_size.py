"""Exercise the file size limit through the installed Git hook."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

from tests.test_pre_commit import SOURCE, fixture_environment, git, repository


AUTHOR = ("-c", "user.name=Codex", "-c", "user.email=codex@local.invalid")
AT_LIMIT = ("测" * 6666 + "x\n").encode("utf-8")
OVER_LIMIT = b"x" + AT_LIMIT
TOKEN_EXEMPTIONS = ".githooks/token-limit-exemptions.txt"


def stage_exemptions(repository: Path) -> Path:
    exemptions = repository / TOKEN_EXEMPTIONS
    shutil.copyfile(SOURCE / TOKEN_EXEMPTIONS, exemptions)
    assert git(repository, "add", "--", TOKEN_EXEMPTIONS).returncode == 0
    return exemptions


def test_hook_limits_staged_utf8_bytes_and_explains_the_split(repository: Path) -> None:
    at_limit = "at limit.txt"
    over_limit = "超限文件.txt"
    (repository / at_limit).write_bytes(AT_LIMIT)
    (repository / over_limit).write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--", at_limit, over_limit).returncode == 0

    plan = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/pre_commit.py"), "--plan"],
        cwd=repository, capture_output=True, text=True, encoding="utf-8", env=fixture_environment(), check=True,
    )
    assert json.loads(plan.stdout)["staged_file_size_limit_bytes"] == 20_000

    (repository / over_limit).write_bytes(b"shorter unstaged version\n")
    rejected = git(repository, *AUTHOR, "commit", "-m", "Commit oversized file")
    assert rejected.returncode != 0
    assert f'"{over_limit}"  20,001 字节（5,000.25 个 token）' in rejected.stderr
    assert at_limit not in rejected.stderr
    assert "按职责拆分代码" in rejected.stderr
    assert "更新导入" in rejected.stderr
    assert "git add -- <原文件> <新文件>" in rejected.stderr

    assert git(repository, "add", "--", over_limit).returncode == 0
    accepted = git(repository, *AUTHOR, "commit", "-m", "Commit files within the limit")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def test_hook_reads_generated_file_exemptions_from_the_staged_list(repository: Path) -> None:
    exemptions = stage_exemptions(repository)
    task = repository / ".scratch/task"
    task.mkdir(parents=True)
    for number in range(100):
        (task / f"snapshot-input-{number:03d}.txt").write_text("Snapshot input\n", encoding="utf-8")
    reference = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    snapshot = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/check_closeout.py"), "snapshot", "--task-id", "hook-fixture",
         "--target-ref", reference, "--scope", ".scratch/task"],
        cwd=repository, capture_output=True, text=True, encoding="utf-8", env=fixture_environment(), check=True,
    )
    snapshot_bytes = snapshot.stdout.encode("utf-8")
    assert len(snapshot_bytes) > len(AT_LIMIT)
    baselines = [".scratch/task/baseline.json", ".scratch/task/worktree-baseline.json"]
    for path in baselines:
        (repository / path).write_bytes(snapshot_bytes)
    assert git(repository, "add", "--force", "--", *baselines).returncode == 0

    custom_output = "tool-output.txt"
    reports = [
        ".scratch/check-runs/results/desktop-run-test-reports/outputFile/final.json",
        ".scratch/check-runs/results/p-test.failure.txt",
        ".scratch/check-runs/results/p-test.reports/report.xml",
        ".scratch/check-runs/results/parallel-test.json",
    ]
    paths = ["requirements.lock", "desktop/package-lock.json", "desktop/protocol.generated.ts", *reports, custom_output]
    for path in paths:
        output = repository / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--", *paths).returncode == 0
    with exemptions.open("a", encoding="utf-8") as stream:
        stream.write(f"\n# Tool output\n{custom_output}\n")

    rejected = git(repository, *AUTHOR, "commit", "-m", "Commit generated files before marking the custom output")
    assert rejected.returncode != 0
    assert f'"{custom_output}"  20,001 字节' in rejected.stderr
    assert all(path not in rejected.stderr for path in [*paths[:-1], *baselines])

    assert git(repository, "add", "--", TOKEN_EXEMPTIONS).returncode == 0
    accepted = git(repository, *AUTHOR, "commit", "-m", "Commit exempt files")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def test_hook_ignores_local_git_attributes_for_exemptions(repository: Path) -> None:
    stage_exemptions(repository)
    user_attributes = repository.parent / "user-attributes"
    user_attributes.write_text("user-exempt.txt -myrk-token-limit\n", encoding="utf-8")
    assert git(repository, "config", "--local", "core.attributesFile", str(user_attributes)).returncode == 0
    info_attributes = repository / ".git/info/attributes"
    info_attributes.write_text("info-exempt.txt -myrk-token-limit\n", encoding="utf-8")
    paths = ["user-exempt.txt", "info-exempt.txt"]
    for path in paths:
        (repository / path).write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--", *paths).returncode == 0
    attributes = git(repository, "check-attr", "--cached", "myrk-token-limit", "--", *paths)
    assert attributes.stdout.splitlines() == [f"{path}: myrk-token-limit: unset" for path in paths]

    rejected = git(repository, *AUTHOR, "commit", "-m", "Commit files with local exemption attributes")
    assert rejected.returncode != 0
    assert all(f'"{path}"  20,001 字节' in rejected.stderr for path in paths)


def test_hook_reports_closeout_preflight_failure_before_size_limit(repository: Path) -> None:
    path = "oversized.txt"
    (repository / path).write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--", path).returncode == 0
    manifest = repository / ".scratch/task/resources.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")

    rejected = subprocess.run(
        ["git", "-C", str(repository), *AUTHOR, "commit", "-m", "Commit with invalid closeout and oversized file"],
        capture_output=True, text=True, encoding="utf-8",
        env={**fixture_environment(), "MYRK_CLOSEOUT_MANIFEST": str(manifest)},
    )
    output = rejected.stdout + rejected.stderr
    assert rejected.returncode != 0
    assert "scripts/check_closeout.py preflight" in output
    preflight = json.loads(next(line for line in output.splitlines() if line.startswith("{")))
    assert preflight["phase"] == "preflight"
    assert preflight["ok"] is False
    assert f'"{path}"  20,001 字节' not in output


def test_hook_checks_only_the_index_proposed_for_commit(repository: Path) -> None:
    selected = "selected.txt"
    other = "other.txt"
    (repository / selected).write_bytes(AT_LIMIT)
    (repository / other).write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--", selected, other).returncode == 0

    accepted = git(repository, *AUTHOR, "commit", "-m", "Commit one path", "--only", "--", selected)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert git(repository, "diff", "--cached", "--name-only").stdout.strip() == other

    rejected = git(repository, *AUTHOR, "commit", "-m", "Commit other path")
    assert rejected.returncode != 0
    assert f'"{other}"  20,001 字节' in rejected.stderr


def test_hook_ignores_unstaged_intent_to_add_and_deleted_files(repository: Path) -> None:
    deleted = "deleted.txt"
    (repository / deleted).write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--", deleted).returncode == 0
    initial = git(repository, *AUTHOR, "-c", "core.hooksPath=.initial-hooks", "commit", "-m", "Old oversized file")
    assert initial.returncode == 0, initial.stdout + initial.stderr

    assert git(repository, "rm", "--", deleted).returncode == 0
    (repository / "intent.txt").write_bytes(OVER_LIMIT)
    (repository / "untracked.txt").write_bytes(OVER_LIMIT)
    assert git(repository, "add", "--intent-to-add", "--", "intent.txt").returncode == 0
    accepted = git(repository, *AUTHOR, "commit", "-m", "Remove old oversized file")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
