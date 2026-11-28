"""Exercise navigation and its proposed inputs through the installed Git hook."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_pre_commit import POWERSHELL, fixture_environment, git, repository


@pytest.mark.parametrize("path", [
    ".githooks/pre-commit", "scripts/pre_commit.ps1", "scripts/pre_commit.py", "scripts/check_navigation.py",
    "scripts/check_closeout.py", "scripts/closeout_inventory.py", "scripts/closeout_runs.py", "scripts/closeout_borrowed.py",
    "myrk/conversation.py", "scripts/check_comments.py",
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
    if path in {"scripts/check_closeout.py", "scripts/closeout_inventory.py", "scripts/closeout_runs.py", "scripts/closeout_borrowed.py"}:
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
