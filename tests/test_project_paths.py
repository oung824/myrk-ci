"""Project-owned resources follow the repository's common Git directory."""

from pathlib import Path
import os
import subprocess

import pytest

from myrk.project_paths import project_root


def test_real_checkout_uses_the_common_git_directory() -> None:
    source = Path(__file__).resolve().parents[1]
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=source, check=True, capture_output=True, text=True, encoding="utf-8",
    )

    assert project_root(source) == Path(common.stdout.strip()).parent


def test_source_inside_an_ordinary_repository_uses_its_root(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    source = tmp_path / "nested" / "source"
    source.mkdir(parents=True)

    assert project_root(source) == tmp_path


@pytest.mark.parametrize("relative_registration", [False, True])
def test_linked_worktree_uses_its_common_repository(tmp_path: Path, relative_registration: bool) -> None:
    primary = tmp_path / "primary"
    registration = primary / ".git" / "worktrees" / "implementation"
    registration.mkdir(parents=True)
    (registration / "commondir").write_text("../..\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    pointer = os.path.relpath(registration, checkout) if relative_registration else str(registration)
    (checkout / ".git").write_text(f"gitdir: {pointer}\n", encoding="utf-8")

    assert project_root(checkout) == primary


def test_source_without_git_registration_remains_its_own_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path.name == ".git" else exists(path))

    assert project_root(tmp_path) == tmp_path


def test_incomplete_worktree_registration_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("incomplete registration\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Git"):
        project_root(tmp_path)
