"""Exercise split file dispositions through the closeout CLI and hook."""

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tests.test_closeout import cli, git, manifest, repo, snapshot
from tests.test_pre_commit import SOURCE, fixture_environment, repository


FRAGMENT = ".closeout/files-001.json"
COMMITTED_FRAGMENT = {"path": FRAGMENT, "outcome": "committed"}


def set_file_lists(resource: Path, file_lists: object) -> None:
    document = json.loads(resource.read_text(encoding="utf-8"))
    document["closeout"]["file_lists"] = file_lists
    resource.write_text(json.dumps(document), encoding="utf-8")


def test_fragment_must_match_the_proposed_index_and_target_commit(repo: Path) -> None:
    baseline = snapshot(repo)
    resource = manifest(repo, baseline, [COMMITTED_FRAGMENT])
    set_file_lists(resource, [FRAGMENT])
    fragment = repo / FRAGMENT
    entries = [{"path": "delivery.txt", "outcome": "committed"}]
    fragment.write_text(json.dumps(entries), encoding="utf-8")
    (repo / "delivery.txt").write_text("Delivery\n", encoding="utf-8")
    git(repo, "add", "-f", "--", str(resource), "delivery.txt")

    result, document = cli(repo, "preflight", str(resource),
                           extra_env={"PYTHONSAFEPATH": "1", "PYTHONPATH": ""})
    assert result.returncode != 0, document
    assert any("Committed content is missing" in error and fragment.name in error for error in document["errors"])

    git(repo, "add", "-f", "--", FRAGMENT)
    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode == 0, document
    assert document["checked"] == 4
    git(repo, "commit", "-m", "Deliver split closeout records")
    result, document = cli(repo, "check", str(resource))
    assert result.returncode == 0, document

    fragment.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    for phase, expected in (("preflight", "Index"), ("check", "Committed")):
        result, document = cli(repo, phase, str(resource))
        assert result.returncode != 0, document
        assert any(f"{expected} content differs" in error and fragment.name in error for error in document["errors"])


@pytest.mark.parametrize(("references", "contents", "message"), [
    (None, "[]", "file_lists must be a list"),
    ([FRAGMENT], None, "list is missing"),
    ([FRAGMENT], "not JSON", "Invalid JSON"),
    ([FRAGMENT], "{}", "JSON array"),
    ([FRAGMENT], "[null]", "JSON array"),
    ([FRAGMENT, FRAGMENT], "[]", "Duplicate closeout file list"),
    ([".closeout/../outside.json"], "[]", "Parent traversal"),
    (["outside.json"], "[]", "within the manifest directory"),
])
def test_invalid_file_list_references_fail_preflight(repo: Path, references: object, contents: str | None,
                                                     message: str) -> None:
    resource = manifest(repo, snapshot(repo), [COMMITTED_FRAGMENT])
    set_file_lists(resource, references)
    if contents is not None:
        (repo / FRAGMENT).write_text(contents, encoding="utf-8")

    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert any(message in error for error in document["errors"]), document


def test_metadata_only_fragment_fails_before_parsing(repo: Path) -> None:
    resource = manifest(repo, snapshot(repo, "--metadata-only", FRAGMENT), [COMMITTED_FRAGMENT])
    set_file_lists(resource, [FRAGMENT])
    (repo / FRAGMENT).write_bytes(b"\xff")

    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert any("metadata-only closeout file list" in error for error in document["errors"]), document


def test_root_manifest_cannot_load_a_file_list_from_the_git_database(repo: Path) -> None:
    reference = ".git/files-001.json"
    resource = manifest(repo, snapshot(repo), [])
    root_manifest = repo / "resources.json"
    shutil.copyfile(resource, root_manifest)
    set_file_lists(root_manifest, [reference])

    result, document = cli(repo, "preflight", str(root_manifest))
    assert result.returncode != 0, document
    assert any("Git database" in error for error in document["errors"]), document


def test_linked_fragment_is_rejected_before_parsing(repo: Path) -> None:
    reference = ".closeout/linked/files-001.json"
    resource = manifest(repo, snapshot(repo), [{"path": reference, "outcome": "committed"}])
    set_file_lists(resource, [reference])
    target = repo.parent / "outside"
    target.mkdir()
    (target / "files-001.json").write_text("[]", encoding="utf-8")
    link = resource.parent / "linked"
    if sys.platform == "win32":
        created = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                 capture_output=True, timeout=20)
        if created.returncode:
            pytest.skip("Windows junctions are unavailable in this test environment")
    else:
        link.symlink_to(target, target_is_directory=True)

    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert any("Linked file or ancestor" in error for error in document["errors"]), document


@pytest.mark.parametrize("outcome", [None, "removed", "external", "committed-deletion"])
def test_every_fragment_needs_a_committed_entry(repo: Path, outcome: str | None) -> None:
    baseline = snapshot(repo)
    records = [] if outcome is None else [{**COMMITTED_FRAGMENT, "outcome": outcome}]
    if outcome == "committed-deletion":
        records = [{**COMMITTED_FRAGMENT, "deletion": True}]
    resource = manifest(repo, baseline, records)
    set_file_lists(resource, [FRAGMENT])
    (repo / FRAGMENT).write_text("[]", encoding="utf-8")
    git(repo, "add", "-f", "--", str(resource), FRAGMENT)

    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    expected = "Unclassified file:" if outcome is None else "Required closeout file must be committed"
    assert any(expected in error and "files-001.json" in error for error in document["errors"]), document


def test_duplicate_candidate_between_inline_and_fragment_is_rejected(repo: Path) -> None:
    record = {"path": "source.txt", "outcome": "committed"}
    resource = manifest(repo, snapshot(repo), [COMMITTED_FRAGMENT, record])
    set_file_lists(resource, [FRAGMENT])
    (repo / FRAGMENT).write_text(json.dumps([record]), encoding="utf-8")

    result, document = cli(repo, "preflight", str(resource))
    assert result.returncode != 0, document
    assert "Duplicate file dispositions are not accepted." in document["errors"]


@pytest.mark.parametrize("change", ["untracked", "deleted"])
def test_hook_requires_the_file_list_parser_in_the_proposed_index(repository: Path, change: str) -> None:
    name = "scripts/closeout_file_lists.py"
    shutil.copyfile(SOURCE / name, repository / name)
    if change == "deleted":
        git(repository, "add", "--", name)
        git(repository, "-c", "core.hooksPath=.initial-hooks", "-c", "user.name=Closeout test",
            "-c", "user.email=closeout@example.invalid", "commit", "-m", "Save file list parser")
        git(repository, "rm", "--", name)
    result = subprocess.run([sys.executable, "-B", str(repository / "scripts/pre_commit.py")],
                            env={**fixture_environment(), "MYRK_CLOSEOUT_MANIFEST": ".closeout/resources.json"},
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert name in output and "未暂存" in output, output
    assert "Myrk pre-commit:" not in output, output
