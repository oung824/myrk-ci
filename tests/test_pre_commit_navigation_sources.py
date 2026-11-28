from pathlib import Path

from tests.test_pre_commit import git, repository


def test_unstaged_guidance_deletion_cannot_hide_a_staged_link_target_deletion(repository: Path) -> None:
    guide_path = "tests/support/agent_acceptance/README.md"
    target_path = "tests/support/agent_acceptance/target.md"
    guide = repository / guide_path
    target = repository / target_path
    guide.parent.mkdir(parents=True)
    guide.write_text("[Reference](target.md)\n", encoding="utf-8")
    target.write_text("# Reference\n", encoding="utf-8")
    author = ("-c", "user.name=Codex", "-c", "user.email=codex@local.invalid")
    assert git(repository, "add", "--", guide_path, target_path).returncode == 0
    initial = git(repository, *author, "commit", "-m", "Add valid acceptance guidance")
    assert initial.returncode == 0, initial.stdout + initial.stderr

    guide.unlink()
    assert git(repository, "rm", "--", target_path).returncode == 0
    rejected = git(repository, *author, "commit", "-m", "Remove target still linked in proposed commit")
    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert guide_path in rejected.stdout + rejected.stderr

    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text("# Updated acceptance guidance\n", encoding="utf-8")
    assert git(repository, "add", "--", guide_path).returncode == 0
    accepted = git(repository, *author, "commit", "-m", "Remove target and update acceptance guidance")
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
