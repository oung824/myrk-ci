"""Exercise check isolation and comment rules through the installed Git hook."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.check_comments import scan
from tests.test_pre_commit import GIT_LOCAL_ENV, fixture_environment, git, repository


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
