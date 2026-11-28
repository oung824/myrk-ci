"""Exercise lightweight checks and the actual Git hook in an isolated repository."""

import json
from collections.abc import Iterator
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1]
POWERSHELL = r"C:\Program Files\PowerShell\7\pwsh.exe"
GIT_LOCAL_ENV = set(subprocess.run(
    ["git", "rev-parse", "--local-env-vars"], check=True, capture_output=True, text=True, encoding="utf-8",
).stdout.splitlines())


def fixture_environment() -> dict[str, str]:
    return {**{key: value for key, value in os.environ.items() if key not in GIT_LOCAL_ENV | {"MYRK_CLOSEOUT_MANIFEST"}},
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "MYRK_PYTHON": sys.executable}


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8",
        env=fixture_environment(),
    )


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "repository with spaces"
    root.mkdir()
    try:
        for name in (
            "scripts/pre_commit.py", "scripts/pre_commit.ps1", "scripts/install_hooks.ps1",
            "scripts/check_navigation.py", "scripts/check_closeout.py", "scripts/closeout_inventory.py",
            "scripts/closeout_runs.py", "scripts/closeout_borrowed.py",
            ".githooks/pre-commit", ".githooks/.gitattributes",
            "scripts/check_comments.py", "desktop/scripts/comment-tokens.mjs",
        ):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, target)
        (root / "scripts/comment_baseline.json").write_text(
            json.dumps({".githooks/pre-commit": ["#!/bin/sh"]}) + "\n", encoding="utf-8",
        )
        for name in ("AGENTS.md", "CODING_STANDARDS.md", "GLOSSARY.md", "docs/agents/domain.md"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# 文档\n", encoding="utf-8")
        (root / "README.md").write_text("# 示例\n\n当前采用第9版记录格式。\n", encoding="utf-8")
        (root / "myrk").mkdir()
        (root / "myrk/conversation.py").write_text("record = ConversationRecord(version=9)\n", encoding="utf-8")
        assert git(root, "init", "--quiet").returncode == 0
        assert git(root, "config", "--local", "core.autocrlf", "true").returncode == 0
        assert git(root, "add", "--all").returncode == 0
        initial = git(root, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "--quiet", "-m", "Fixture baseline")
        assert initial.returncode == 0, initial.stdout + initial.stderr
        installed = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-File", str(root / "scripts/install_hooks.ps1")],
            capture_output=True, text=True, encoding="utf-8",
            env=fixture_environment(),
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr
        yield root
    finally:
        objects = root / ".git/objects"
        assert objects.resolve().is_relative_to(root.resolve())
        for path in objects.rglob("*"):
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                path.chmod(mode | stat.S_IWRITE)


def plan(root: Path) -> list[dict[str, object]]:
    result = subprocess.run(
        [sys.executable, "-B", str(root / "scripts/pre_commit.py"), "--plan"],
        capture_output=True, text=True, encoding="utf-8", env=fixture_environment(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)["commands"]


@pytest.mark.parametrize(("path", "content"), [
    ("myrk/new_feature.py", "value = 1\n"),
    ("desktop/src/renderer/NewPanel.tsx", "export const value = 1;\n"),
    ("tests/support/agent_acceptance/core.py", "value = 1\n"),
])
def test_actual_hook_commits_backend_without_starting_tests(repository: Path, path: str, content: str) -> None:
    marker = repository.parent / "test-runner-started.txt"
    runner = repository / "scripts/run_checks.py"
    runner.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\nraise SystemExit(73)\n",
        encoding="utf-8",
    )
    desktop = repository / "desktop"
    desktop.mkdir(exist_ok=True)
    poison = subprocess.list2cmdline([sys.executable, "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('started'); raise SystemExit(73)"])
    (desktop / "package.json").write_text(json.dumps({"scripts": {"test": poison, "typecheck": poison}}), encoding="utf-8")
    assert git(repository, "add", "scripts/run_checks.py", "desktop/package.json").returncode == 0
    baseline = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c",
                   "user.email=codex@local.invalid", "commit", "-m", "Unexpected test runner fixture")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert git(repository, "add", "--", path).returncode == 0

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Commit without product tests")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), "Creating a commit must not start the test or typecheck runner."
    assert "scripts/run_checks.py" not in result.stdout + result.stderr
    assert "typecheck" not in result.stdout + result.stderr
    assert git(repository, "show", "HEAD:" + path).stdout == content


def test_commit_plan_lists_explicit_closeout_preflight_before_checks(repository: Path) -> None:
    manifest = repository / ".scratch/task/resources.json"
    environment = {**fixture_environment(), "MYRK_CLOSEOUT_MANIFEST": str(manifest)}
    result = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/pre_commit.py"), "--plan"],
        capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    commands = json.loads(result.stdout)["commands"]
    assert commands[0]["argv"] == [sys.executable, "-B", "scripts/check_closeout.py", "preflight", str(manifest),
                                    "--root", str(repository)]
    assert not manifest.exists(), "Planning must not create the declared manifest."


def test_commit_plan_passes_hook_proposed_index_explicitly(repository: Path) -> None:
    proposed = repository / ".git/proposed-index"
    environment = {**fixture_environment(), "GIT_INDEX_FILE": str(proposed)}
    prepared = subprocess.run(["git", "-C", str(repository), "read-tree", "HEAD"],
                              capture_output=True, text=True, encoding="utf-8", env=environment)
    assert prepared.returncode == 0, prepared.stderr
    environment.update(MYRK_CLOSEOUT_MANIFEST=".scratch/task/resources.json", GIT_INDEX_FILE=".git/proposed-index")
    result = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/pre_commit.py"), "--plan"], cwd=repository,
        capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    command = json.loads(result.stdout)["commands"][0]["argv"]
    assert command[-2:] == ["--index-file", str(proposed)]


def test_actual_hook_stops_at_closeout_preflight_before_lightweight_workers(repository: Path) -> None:
    marker = repository.parent / "navigation-worker-ran.txt"
    navigation = repository / "scripts/check_navigation.py"
    original = navigation.read_text(encoding="utf-8")
    instrumented = original.replace('if __name__ == "__main__":\n',
        f'if __name__ == "__main__":\n    Path({str(marker)!r}).write_text("ran")\n')
    assert instrumented != original
    navigation.write_text(instrumented, encoding="utf-8")
    assert git(repository, "add", "scripts/check_navigation.py").returncode == 0
    source = repository / "myrk/new_feature.py"
    source.write_text("value = 1\n", encoding="utf-8")
    assert git(repository, "add", "myrk/new_feature.py").returncode == 0
    manifest = repository / ".scratch/task/resources.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")
    environment = {**fixture_environment(), "MYRK_CLOSEOUT_MANIFEST": str(manifest)}
    staged_before = git(repository, "ls-files", "--stage").stdout
    rejected = subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
         "commit", "-m", "Invalid closeout"],
        capture_output=True, text=True, encoding="utf-8", env=environment,
    )
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    output = rejected.stdout + rejected.stderr
    assert "scripts/check_closeout.py preflight" in output
    document = json.loads(next(line for line in reversed(output.splitlines()) if line.startswith("{")))
    assert document["phase"] == "preflight", document
    assert document["ok"] is False, document
    assert not marker.exists(), "A failed closeout preflight must stop before starting the navigation worker."
    assert "Myrk pre-commit: git diff --cached --check" not in output
    assert git(repository, "ls-files", "--stage").stdout == staged_before


@pytest.mark.parametrize("include_delivery", [True, False])
def test_actual_commit_only_closeout_uses_proposed_content_and_keeps_other_staged_work(
    repository: Path, include_delivery: bool,
) -> None:
    delivery = repository / "delivery.txt"
    delivery.write_text("Original delivery\n", encoding="utf-8")
    assert git(repository, "add", "delivery.txt").returncode == 0
    initial = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c",
                  "user.email=codex@local.invalid", "commit", "-m", "Tracked delivery fixture")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    other = repository / "other.txt"
    other.write_text("Other task's staged work\n", encoding="utf-8")
    assert git(repository, "add", "other.txt").returncode == 0
    task = repository / ".scratch/task"
    task.mkdir(parents=True)
    reference = git(repository, "symbolic-ref", "HEAD").stdout.strip()
    observed = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/check_closeout.py"), "snapshot", "--task-id", "hook-fixture",
         "--target-ref", reference, "--scope", ".scratch/task"],
        capture_output=True, text=True, encoding="utf-8", env=fixture_environment(),
    )
    assert observed.returncode == 0, observed.stdout + observed.stderr
    baseline = task / "baseline.json"
    baseline.write_text(observed.stdout, encoding="utf-8")
    delivery.write_text("Updated delivery\n", encoding="utf-8")
    resource = task / "resources.json"
    resource.write_text(json.dumps({"project_root": str(repository), "closeout": {
        "baseline": str(baseline), "task_id": "hook-fixture", "target_ref": reference,
        "files": [{"path": str(path), "outcome": "committed"} for path in (baseline, resource, delivery)]
                 + [{"path": str(other), "outcome": "external", "owner": "other-task", "reason": "Other staged work"}],
    }}), encoding="utf-8")
    control_names = [str(path.relative_to(repository)) for path in (baseline, resource)]
    assert git(repository, "add", "--", *control_names, *([] if include_delivery else ["delivery.txt"])).returncode == 0
    staged_before = git(repository, "ls-files", "--stage").stdout
    head_before = git(repository, "rev-parse", "HEAD").stdout
    result = subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
         "commit", "--only", "-m", "Deliver selected files", "--", *control_names,
         *(["delivery.txt"] if include_delivery else [])],
        capture_output=True, text=True, encoding="utf-8",
        env={**fixture_environment(), "MYRK_CLOSEOUT_MANIFEST": str(resource)},
    )
    output = result.stdout + result.stderr
    assert "scripts/check_closeout.py preflight" in output, output
    document = json.loads(next(line for line in output.splitlines() if line.startswith("{")))
    assert document["phase"] == "preflight", document
    if include_delivery:
        assert result.returncode == 0, output
        assert document["ok"] is True, document
        assert git(repository, "show", "HEAD:delivery.txt").stdout == "Updated delivery\n"
        assert git(repository, "diff", "--cached", "--name-only").stdout.strip() == "other.txt"
    else:
        assert result.returncode != 0, output
        assert document["ok"] is False, document
        assert any(str(delivery) in error for error in document["errors"]), document
        assert git(repository, "rev-parse", "HEAD").stdout == head_before
        assert git(repository, "ls-files", "--stage").stdout == staged_before


@pytest.mark.parametrize("path", [
    "README.md", "myrk/new_feature.py", "myrk/desktop_service.py", "myrk/recovery.py",
    "mypy.ini", ".mypy.ini", "setup.cfg", "tests/test_new_feature.py",
    "scripts/pre_commit.py", "scripts/check_closeout.py", "scripts/parallel_checks.py", "scripts/pytest_shards.py",
    "desktop/src/renderer/NewPanel.tsx", "desktop/src/main/new.test.ts", "desktop/src/shared/protocol.ts",
    "tests/support/agent_acceptance/runner.py", "tests/acceptance/materials/facts.txt",
    "tests/acceptance/plans/live-plan.json", ".githooks/pre-commit",
])
def test_commit_plan_is_lightweight_for_every_changed_domain(repository: Path, path: str) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    assert git(repository, "add", "--", path).returncode == 0

    assert plan(repository) == [
        {"argv": ["git", "diff", "--cached", "--check"], "cwd": str(repository)},
        {"argv": [sys.executable, "-B", "scripts/check_navigation.py"], "cwd": str(repository)},
        {"argv": [sys.executable, "-B", "scripts/check_comments.py", "--staged"], "cwd": str(repository)},
    ]


def test_acceptance_commit_allows_unrelated_untracked_desktop_source(repository: Path) -> None:
    support = repository / "tests/support/agent_acceptance/core.py"
    support.parent.mkdir(parents=True)
    support.write_text("staged_acceptance = True\n", encoding="utf-8")
    assert git(repository, "add", "--", str(support.relative_to(repository))).returncode == 0
    desktop = repository / "desktop/src/shared/protocol.ts"
    desktop.parent.mkdir(parents=True)
    desktop.write_text("// Another task's untracked desktop source\n", encoding="utf-8")
    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Independent acceptance change")
    assert result.returncode == 0, result.stdout + result.stderr
    assert desktop.read_text(encoding="utf-8") == "// Another task's untracked desktop source\n"
    assert git(repository, "ls-files", "--error-unmatch", "desktop/src/shared/protocol.ts").returncode != 0


def test_actual_git_hook_rejects_broken_links_and_allows_the_staged_fix(repository: Path) -> None:
    readme = repository / "README.md"
    initial = readme.read_text(encoding="utf-8")
    readme.write_text(initial + "\n[missing](missing.md)\n", encoding="utf-8")
    assert git(repository, "add", "README.md").returncode == 0

    rejected = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "-m", "Broken navigation")
    assert rejected.returncode != 0
    assert "missing local target missing.md" in rejected.stdout + rejected.stderr

    readme.write_text(initial + "\n[domain](docs/agents/domain.md)\n", encoding="utf-8")
    unstaged = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "-m", "Unstaged fix")
    assert unstaged.returncode != 0
    assert "README.md" in unstaged.stdout + unstaged.stderr
    assert "未暂存" in unstaged.stdout + unstaged.stderr

    assert git(repository, "add", "README.md").returncode == 0
    accepted = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "-m", "Valid navigation")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "Myrk pre-commit:" in accepted.stdout + accepted.stderr


def test_actual_git_hook_rejects_a_documented_record_format_mismatch(repository: Path) -> None:
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("第9版", "第8版"), encoding="utf-8")
    assert git(repository, "add", "README.md").returncode == 0

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Mismatched documented record format")

    assert result.returncode != 0
    assert "README conversation format 8 differs from writer format 9" in result.stdout + result.stderr
