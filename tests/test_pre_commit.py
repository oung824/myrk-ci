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

from scripts.check_comments import scan


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
            "scripts/check_navigation.py", "scripts/check_closeout.py", ".githooks/pre-commit", ".githooks/.gitattributes",
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


@pytest.mark.parametrize("path", [
    ".githooks/pre-commit", "scripts/pre_commit.ps1", "scripts/pre_commit.py", "scripts/check_navigation.py",
    "scripts/check_closeout.py", "myrk/conversation.py", "scripts/check_comments.py",
    "scripts/comment_baseline.json", "desktop/scripts/comment-tokens.mjs",
])
def test_actual_git_hook_rejects_unstaged_inputs_it_reads(repository: Path, path: str) -> None:
    source = repository / path
    source.write_text(source.read_text(encoding="utf-8") + "\n# Another task's unstaged change\n", encoding="utf-8")
    (repository / "delivery.txt").write_text("This task's delivery\n", encoding="utf-8")
    assert git(repository, "add", "delivery.txt").returncode == 0
    staged_before = git(repository, "ls-files", "--stage").stdout
    head_before = git(repository, "rev-parse", "HEAD").stdout
    environment = fixture_environment()
    if path == "scripts/check_closeout.py":
        environment["MYRK_CLOSEOUT_MANIFEST"] = ".scratch/task/resources.json"

    result = subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
         "commit", "-m", "Unstaged lightweight check input"],
        capture_output=True, text=True, encoding="utf-8", env=environment,
    )

    assert result.returncode != 0
    assert path in result.stdout + result.stderr
    assert "未暂存" in result.stdout + result.stderr
    assert "Myrk pre-commit:" not in result.stdout + result.stderr
    assert git(repository, "rev-parse", "HEAD").stdout == head_before
    assert git(repository, "ls-files", "--stage").stdout == staged_before


def test_commit_rejects_untracked_input_in_the_checked_domain(repository: Path) -> None:
    (repository / "README.md").write_text("当前采用第9版记录格式。\n\n说明。\n", encoding="utf-8")
    assert git(repository, "add", "README.md").returncode == 0
    (repository / "docs/new-guide.md").write_text("# 未跟踪说明\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/pre_commit.py")],
        capture_output=True, text=True, encoding="utf-8", env=fixture_environment(),
    )
    assert result.returncode != 0
    assert "docs/new-guide.md" in result.stdout + result.stderr


@pytest.mark.parametrize("path", ["ignored-guide.md", "docs/ignored-guide.md"])
def test_actual_git_hook_rejects_ignored_navigation_inputs_missing_from_the_index(repository: Path, path: str) -> None:
    source = repository / path
    source.write_text("# Ignored navigation input\n", encoding="utf-8")
    (repository / ".git/info/exclude").write_text(path + "\n", encoding="utf-8")
    assert git(repository, "check-ignore", "--", path).returncode == 0
    (repository / "delivery.txt").write_text("This task's delivery\n", encoding="utf-8")
    assert git(repository, "add", "delivery.txt").returncode == 0

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Ignored input missing from proposed commit")

    assert result.returncode != 0
    assert path in result.stdout + result.stderr
    assert "暂存版本" in result.stdout + result.stderr
    assert "Myrk pre-commit:" not in result.stdout + result.stderr


@pytest.mark.parametrize("ignored", [False, True])
def test_actual_git_hook_rejects_a_link_target_missing_from_the_commit(repository: Path, ignored: bool) -> None:
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n[future](myrk/future.py)\n", encoding="utf-8")
    (repository / "myrk/future.py").write_text("future = True\n", encoding="utf-8")
    assert git(repository, "add", "README.md").returncode == 0
    if ignored:
        (repository / ".git/info/exclude").write_text("myrk/future.py\n", encoding="utf-8")
    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "-m", "Uncommitted target")
    assert result.returncode != 0
    assert "myrk/future.py" in result.stdout + result.stderr
    assert "暂存版本" in result.stdout + result.stderr


def test_actual_git_hook_allows_unstaged_body_changes_for_an_existing_file_link(repository: Path) -> None:
    unlinked = repository / "myrk/unrelated.py"
    unlinked.write_text("value = 1\n", encoding="utf-8")
    assert git(repository, "add", "myrk/unrelated.py").returncode == 0
    baseline = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "-m", "Unrelated fixture source")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    unlinked.write_text("value = 2\n", encoding="utf-8")
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n[implementation](myrk/unrelated.py)\n", encoding="utf-8")
    assert git(repository, "add", "README.md").returncode == 0
    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "-m", "Independent documentation")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("path", "reference", "original", "updated"), [
    ("myrk/linked.py", "myrk/linked.py:1", "value = 1\n", "value = 2\n"),
    ("materials/reference.md", "materials/reference.md#guide", "# Guide\n\nOriginal\n", "# Guide\n\nUpdated\n"),
])
def test_actual_git_hook_rejects_unstaged_link_targets_when_it_reads_their_content(
    repository: Path, path: str, reference: str, original: str, updated: str,
) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(original, encoding="utf-8")
    assert git(repository, "add", "--", path).returncode == 0
    baseline = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c",
                   "user.email=codex@local.invalid", "commit", "-m", "Linked content fixture")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    target.write_text(updated, encoding="utf-8")
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + f"\n[reference]({reference})\n", encoding="utf-8")
    assert git(repository, "add", "README.md").returncode == 0

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Unstaged content used to validate a reference")

    assert result.returncode != 0
    assert path in result.stdout + result.stderr
    assert "未暂存" in result.stdout + result.stderr


@pytest.mark.parametrize("recreate_ignored", [False, True])
def test_actual_git_hook_allows_staged_deletion_only_while_the_working_tree_path_stays_absent(
    repository: Path, recreate_ignored: bool,
) -> None:
    path = "myrk/obsolete.py"
    target = repository / path
    target.write_text("obsolete = True\n", encoding="utf-8")
    assert git(repository, "add", "--", path).returncode == 0
    baseline = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c",
                   "user.email=codex@local.invalid", "commit", "-m", "Obsolete source fixture")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    assert git(repository, "rm", "--", path).returncode == 0
    if recreate_ignored:
        (repository / ".git/info/exclude").write_text(path + "\n", encoding="utf-8")
        target.write_text("obsolete = False\n", encoding="utf-8")
        assert git(repository, "check-ignore", "--", path).returncode == 0
    staged_before = git(repository, "ls-files", "--stage").stdout
    head_before = git(repository, "rev-parse", "HEAD").stdout

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Delete obsolete source")

    if recreate_ignored:
        assert result.returncode != 0
        assert path in result.stdout + result.stderr
        assert "未暂存" in result.stdout + result.stderr
        assert git(repository, "rev-parse", "HEAD").stdout == head_before
        assert git(repository, "ls-files", "--stage").stdout == staged_before
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert git(repository, "show", "HEAD:" + path).returncode != 0
        assert not target.exists()


def test_shell_hook_keeps_lf_line_endings(repository: Path) -> None:
    result = git(repository, "check-attr", "eol", "--", ".githooks/pre-commit")
    assert result.returncode == 0
    assert result.stdout.strip().endswith(": eol: lf")


@pytest.mark.parametrize("shared", [
    "pyproject.toml", "conftest.py", "requirements.txt", "requirements-dev.txt", "requirements.lock",
    "mypy.ini", ".mypy.ini", "setup.cfg",
    "tests/conftest.py", "scripts/run_checks.py", "scripts/parallel_checks.py", "scripts/pytest_shards.py",
    "scripts/check_resources.py", "myrk/__init__.py", "myrk/check_plugin.py",
    "myrk/check_outputs.py", "myrk/project_paths.py",
])
def test_guidance_commit_allows_unrelated_unstaged_check_infrastructure(repository: Path, shared: str) -> None:
    target = repository / shared
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Committed check input\n", encoding="utf-8")
    assert git(repository, "add", "--", shared).returncode == 0
    initial = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "--quiet", "-m", "Check input fixture")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target.write_text("# Unstaged repair\n", encoding="utf-8")
    hook = repository / "scripts/pre_commit.py"
    hook.write_text(hook.read_text(encoding="utf-8") + "\nGUIDANCE_FIXTURE = True\n", encoding="utf-8")
    assert git(repository, "add", "scripts/pre_commit.py").returncode == 0

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Guidance change alongside another task")
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_text(encoding="utf-8") == "# Unstaged repair\n"
    assert git(repository, "show", "HEAD:" + shared).stdout == "# Committed check input\n"


@pytest.mark.parametrize("configuration", ["mypy.ini", ".mypy.ini", "setup.cfg"])
@pytest.mark.parametrize("staged", ["myrk/new_feature.py", "scripts/pre_commit.py"])
@pytest.mark.parametrize("ignored", [False, True])
def test_actual_git_hook_allows_unrelated_untracked_mypy_config(
    repository: Path, configuration: str, staged: str, ignored: bool,
) -> None:
    source = repository / staged
    with source.open("a", encoding="utf-8") as stream:
        stream.write("\nselected = True\n")
    assert git(repository, "add", "--", staged).returncode == 0
    (repository / configuration).write_text("[mypy]\nignore_errors = True\n", encoding="utf-8")
    if ignored:
        (repository / ".git/info/exclude").write_text(configuration + "\n", encoding="utf-8")

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Commit alongside another task's type-check configuration")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repository / configuration).read_text(encoding="utf-8") == "[mypy]\nignore_errors = True\n"
    assert git(repository, "ls-files", "--error-unmatch", configuration).returncode != 0


def test_installer_preserves_existing_hooks(repository: Path) -> None:
    assert git(repository, "config", "--local", "core.hooksPath", ".other-hooks").returncode == 0
    result = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-File", str(repository / "scripts/install_hooks.ps1")],
        capture_output=True, text=True, encoding="utf-8",
        env=fixture_environment(),
    )
    assert result.returncode != 0
    assert git(repository, "config", "--local", "--get", "core.hooksPath").stdout.strip() == ".other-hooks"


@pytest.mark.parametrize("backend_path", ["myrk/desktop_service.py", "tests/desktop_fixture.py"])
def test_desktop_commit_allows_unrelated_unstaged_backend_changes(repository: Path, backend_path: str) -> None:
    target = repository / backend_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Committed desktop dependency\n", encoding="utf-8")
    assert git(repository, "add", "--", backend_path).returncode == 0
    initial = git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid", "commit", "--quiet", "-m", "Desktop dependency fixture")
    assert initial.returncode == 0, initial.stdout + initial.stderr
    target.write_text("# Unstaged backend repair\n", encoding="utf-8")
    desktop = repository / "desktop/src/main/python-service.test.ts"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text("export const selected = true\n", encoding="utf-8")
    assert git(repository, "add", "--", str(desktop.relative_to(repository))).returncode == 0
    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Independent desktop change")
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_text(encoding="utf-8") == "# Unstaged backend repair\n"
    assert git(repository, "show", "HEAD:" + backend_path).stdout == "# Committed desktop dependency\n"


def test_fixture_git_commands_ignore_foreign_repository_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    foreign = repository.parent / "foreign repository"
    foreign.mkdir()
    assert git(foreign, "init", "--quiet").returncode == 0
    (foreign / "foreign.txt").write_text("Foreign fixture\n", encoding="utf-8")
    (repository / "selected.txt").write_text("Selected fixture\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git/index"))
    observed = git(repository, "status", "--porcelain")
    assert observed.returncode == 0, observed.stderr
    assert "selected.txt" in observed.stdout
    assert "foreign.txt" not in observed.stdout


@pytest.mark.parametrize("include_closeout", [False, True])
def test_mixed_guidance_and_backend_tests_reject_unstaged_backend_repair(repository: Path, include_closeout: bool) -> None:
    names = ["tests/test_navigation.py", "tests/test_pre_commit.py", "tests/test_approval_modes.py"]
    if include_closeout:
        names.append("tests/test_closeout.py")
    for name in names:
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Staged selected test\n", encoding="utf-8")
    assert git(repository, "add", "--", *names).returncode == 0
    (repository / "tests/test_approval_modes.py").write_text("# Unstaged repair to a selected test\n", encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, "-B", str(repository / "scripts/pre_commit.py")],
        capture_output=True, text=True, encoding="utf-8", env=fixture_environment(),
    )
    assert rejected.returncode != 0
    assert "tests/test_approval_modes.py" in rejected.stdout + rejected.stderr
    assert "未暂存" in rejected.stdout + rejected.stderr
    assert "Myrk pre-commit:" not in rejected.stdout + rejected.stderr


def test_check_worker_clears_hook_git_environment_without_changing_its_repository(repository: Path) -> None:
    navigation = repository / "scripts/check_navigation.py"
    probe = repository.parent / "worker-environment.json"
    isolated = repository.parent / "worker-bare.git"
    original = navigation.read_text(encoding="utf-8")
    instrumented = original.replace('if __name__ == "__main__":\n',
        'if __name__ == "__main__":\n'
        "    import os, subprocess\n"
        f"    observed = {{name: os.environ[name] for name in {sorted(GIT_LOCAL_ENV)!r} if name in os.environ}}\n"
        f"    subprocess.run(['git', 'init', '--bare', {str(isolated)!r}], check=True, capture_output=True)\n"
        f"    Path({str(probe)!r}).write_text(json.dumps({{'git': observed, 'task': os.environ.get('MYRK_TASK_ID'), "
        "'python': os.environ.get('MYRK_PYTHON')}), encoding='utf-8')\n")
    assert instrumented != original
    navigation.write_text(instrumented, encoding="utf-8")
    assert git(repository, "add", "scripts/check_navigation.py").returncode == 0
    configuration = (repository / ".git/config").read_bytes()
    environment = {**fixture_environment(), "GIT_DIR": str(repository / ".git"),
        "GIT_WORK_TREE": str(repository), "GIT_INDEX_FILE": str(repository / ".git/index"),
        "MYRK_TASK_ID": "isolated-hook-worker"}
    result = subprocess.run([sys.executable, "-B", str(repository / "scripts/pre_commit.py")],
        capture_output=True, text=True, encoding="utf-8", env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(probe.read_bytes())
    assert observed == {"git": {}, "task": "isolated-hook-worker", "python": sys.executable}
    assert (repository / ".git/config").read_bytes() == configuration
    assert (isolated / "HEAD").is_file()


def test_comment_lint_checks_only_the_index_proposed_for_commit(repository: Path) -> None:
    source = repository / "myrk/conversation.py"
    source.write_text(source.read_text(encoding="utf-8") + "marker = '# literal'\n", encoding="utf-8")
    other = repository / "myrk/other.py"
    other.write_text("value = True  # new prose\n", encoding="utf-8")
    assert git(repository, "add", "myrk/other.py").returncode == 0
    author = ("-c", "user.name=Codex", "-c", "user.email=codex@local.invalid")

    selected = git(repository, *author, "commit", "--only", "-m", "Commit one path", "myrk/conversation.py")
    assert selected.returncode == 0, selected.stdout + selected.stderr
    assert git(repository, "diff", "--cached", "--name-only").stdout.strip() == "myrk/other.py"
    rejected = git(repository, *author, "commit", "-m", "Commit new source comment")
    assert rejected.returncode != 0
    assert "myrk/other.py:1: source comment is not in the baseline" in rejected.stderr


def test_comment_driver_recognizes_shell_word_boundaries() -> None:
    path = ".githooks/pre-commit"
    source = (b"#!/bin/sh\nexec 'path with # literal' ordinary#word \\#escaped # inline\n# standalone\n"
              b"exec launcher \\\n# continued command comment\nexec launcher\\\n#literal\n")

    assert scan({path: source}) == {path: [
        {"text": "#!/bin/sh", "line": 1},
        {"text": "# inline", "line": 2},
        {"text": "# standalone", "line": 3},
        {"text": "# continued command comment", "line": 5},
    ]}


def test_comment_driver_rejects_staged_shell_prose(repository: Path) -> None:
    path = ".githooks/pre-commit"
    launcher = repository / path
    launcher.write_bytes(launcher.read_bytes() + b"# new prose\n")
    assert git(repository, "add", path).returncode == 0

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Add a shell comment")

    assert result.returncode != 0
    assert f"{path}:3: source comment is not in the baseline" in result.stderr


@pytest.mark.parametrize(("encoding", "bom"), [("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
def test_comment_driver_accepts_utf16_powershell(encoding: str, bom: bytes) -> None:
    path = "scripts/launch.ps1"
    source = bom + 'Write-Output "a#b"\n# 说明\n'.encode(encoding)

    assert scan({path: source}) == {path: [{"text": "# 说明", "line": 2}]}


@pytest.mark.parametrize("response", [
    {},
    {"scripts/launch.ps1": [], "scripts/other.ps1": []},
    {"scripts/launch.ps1": {}},
    {"scripts/launch.ps1": [{"text": "# note", "line": True}]},
    {"scripts/launch.ps1": [{"text": "# note", "line": 0}]},
    {"scripts/launch.ps1": [{"text": "", "line": 1}]},
])
def test_comment_driver_rejects_invalid_worker_response(monkeypatch: pytest.MonkeyPatch, response: object) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        [], 0, json.dumps(response).encode(),
    ))

    with pytest.raises(ValueError, match="powershell comment parser"):
        scan({"scripts/launch.ps1": b"# note\n"})


def test_comment_baseline_cannot_grow_or_retain_deleted_comments(repository: Path) -> None:
    source = repository / "myrk/legacy.py"
    unrelated = repository / "desktop/src/links.ts"
    unrelated.parent.mkdir()
    unrelated.write_text('export const url = "https://example.invalid"\n', encoding="utf-8")
    baseline = repository / "scripts/comment_baseline.json"
    initial_baseline = json.loads(baseline.read_text(encoding="utf-8"))
    legacy = "# existing prose\nvalue = True\n"
    source.write_text(legacy, encoding="utf-8")
    baseline.write_text(json.dumps({**initial_baseline, "myrk/legacy.py": ["# existing prose"]}) + "\n", encoding="utf-8")
    assert git(repository, "add", "myrk/legacy.py", "scripts/comment_baseline.json", "desktop/src/links.ts").returncode == 0
    author = ("-c", "user.name=Codex", "-c", "user.email=codex@local.invalid")
    seed = git(repository, *author, "-c", "core.hooksPath=.initial-hooks", "commit", "-m", "Existing baseline")
    assert seed.returncode == 0, seed.stdout + seed.stderr

    source.write_text("value = True  # replacement\n", encoding="utf-8")
    baseline.write_text(json.dumps({**initial_baseline, "myrk/legacy.py": ["# replacement"]}) + "\n", encoding="utf-8")
    assert git(repository, "add", "myrk/legacy.py", "scripts/comment_baseline.json").returncode == 0
    replaced = git(repository, *author, "commit", "-m", "Replace a baseline comment")
    assert replaced.returncode != 0
    assert "cannot add baseline comments for myrk/legacy.py" in replaced.stderr

    source.write_text("value = True\n", encoding="utf-8")
    baseline.write_text(json.dumps({**initial_baseline, "myrk/legacy.py": ["# existing prose"]}) + "\n", encoding="utf-8")
    assert git(repository, "add", "myrk/legacy.py", "scripts/comment_baseline.json").returncode == 0
    stale = git(repository, *author, "commit", "-m", "Remove comment")
    assert stale.returncode != 0
    assert "remove unused baseline comments for myrk/legacy.py" in stale.stderr

    baseline.write_text(json.dumps(initial_baseline) + "\n", encoding="utf-8")
    assert git(repository, "add", "scripts/comment_baseline.json").returncode == 0
    removed = git(repository, *author, "commit", "-m", "Remove comment and baseline entry")
    assert removed.returncode == 0, removed.stdout + removed.stderr


def test_git_preflight_keeps_the_proposed_hook_index(repository: Path) -> None:
    proposed = repository.parent / "proposed-index"
    environment = {**fixture_environment(), "GIT_DIR": str(repository / ".git"),
        "GIT_WORK_TREE": str(repository), "GIT_INDEX_FILE": str(proposed)}
    def proposed_git(*arguments):
        return subprocess.run(["git", "-C", str(repository), *arguments], env=environment,
                              capture_output=True, text=True, encoding="utf-8")
    assert proposed_git("read-tree", "HEAD").returncode == 0
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nWhitespace in proposed index.  \n", encoding="utf-8")
    assert proposed_git("add", "README.md").returncode == 0
    assert git(repository, "diff", "--cached", "--check").returncode == 0
    result = subprocess.run([sys.executable, "-B", str(repository / "scripts/pre_commit.py")],
        capture_output=True, text=True, encoding="utf-8", env=environment)
    assert result.returncode != 0
    assert "trailing whitespace" in result.stdout + result.stderr
    assert "Myrk pre-commit: git diff --cached --check" in result.stdout


def test_actual_git_hook_rejects_an_unstaged_guide_deletion_that_hides_a_staged_target_deletion(repository: Path) -> None:
    guide = repository / "docs/guide.md"
    guide.write_text("# Guide\n\n[implementation](../myrk/obsolete.py)\n", encoding="utf-8")
    obsolete = repository / "myrk/obsolete.py"
    obsolete.write_text("obsolete = True\n", encoding="utf-8")
    assert git(repository, "add", "docs/guide.md", "myrk/obsolete.py").returncode == 0
    baseline = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                   "commit", "-m", "Guide and linked source fixture")
    assert baseline.returncode == 0, baseline.stdout + baseline.stderr
    guide.unlink()
    assert git(repository, "rm", "--", "myrk/obsolete.py").returncode == 0
    head_before = git(repository, "rev-parse", "HEAD").stdout
    staged_before = git(repository, "ls-files", "--stage").stdout

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Delete source while its committed guide still links to it")

    assert result.returncode != 0, result.stdout + result.stderr
    assert git(repository, "rev-parse", "HEAD").stdout == head_before
    assert git(repository, "ls-files", "--stage").stdout == staged_before


@pytest.mark.parametrize("looks_like_rename", [False, True])
def test_actual_git_hook_rejects_a_link_target_with_only_intent_to_add(
    repository: Path, looks_like_rename: bool,
) -> None:
    if looks_like_rename:
        previous = repository / "myrk/previous.py"
        previous.write_text("future = True\n", encoding="utf-8")
        assert git(repository, "add", "myrk/previous.py").returncode == 0
        baseline = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                       "commit", "-m", "Tracked source with matching target contents")
        assert baseline.returncode == 0, baseline.stdout + baseline.stderr
        assert git(repository, "config", "diff.renames", "true").returncode == 0
        previous.unlink()
    future = repository / "myrk/future.py"
    future.write_text("future = True\n", encoding="utf-8")
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n[future](myrk/future.py)\n", encoding="utf-8")
    assert git(repository, "add", "-N", "--", "myrk/future.py").returncode == 0
    assert git(repository, "add", "README.md").returncode == 0
    head_before = git(repository, "rev-parse", "HEAD").stdout
    staged_before = git(repository, "ls-files", "--stage").stdout

    result = git(repository, "-c", "user.name=Codex", "-c", "user.email=codex@local.invalid",
                 "commit", "-m", "Reference a target without committed contents")

    assert result.returncode != 0, result.stdout + result.stderr
    assert git(repository, "rev-parse", "HEAD").stdout == head_before
    assert git(repository, "ls-files", "--stage").stdout == staged_before
