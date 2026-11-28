from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import plan_check_evidence, pre_commit
from tests.test_pre_commit import GIT_LOCAL_ENV, SOURCE, fixture_environment, git, repository
from tests.test_pre_commit_size import stage_exemptions


def plan(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SOURCE / "scripts/plan_check_evidence.py"), *arguments],
        cwd=repository, capture_output=True, text=True, encoding="utf-8", env=fixture_environment(),
    )


def test_estimate_checks_future_destinations_using_the_staged_git_exemptions(repository: Path) -> None:
    exemptions = stage_exemptions(repository)
    with exemptions.open("a", encoding="utf-8") as stream:
        stream.write("\nCase.txt\n")
    assert git(repository, "add", "--", str(exemptions)).returncode == 0
    with exemptions.open("a", encoding="utf-8") as stream:
        stream.write(".scratch/check-runs/launches/*.json\n")
    index = repository / ".git/index"
    before = index.read_bytes()
    result_path = ".scratch/check-runs/results/b-planned.json"
    launch_path = ".scratch/check-runs/launches/planned.json"
    result = plan(repository, "--file", result_path, "29883", "--file", launch_path, "37734",
                  "--file", "Case.txt", "20001",
                  "--file", ".scratch/task/archive-index.json", "?",
                  "--archive", ".scratch/task/check-runs.tar.xz", "82264")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    files = {row["path"]: row for row in report["files"]}
    assert report["basis"] == "estimated_bytes"
    assert report["limit_bytes"] == 20_000
    assert files[result_path]["exempt"] is True and files[result_path]["exceeds_limit"] is False
    assert files[launch_path]["exempt"] is False and files[launch_path]["exceeds_limit"] is True
    assert files["Case.txt"]["exempt"] is True
    result = plan(repository, "--file", "case.txt", "20001")
    assert result.returncode == 0, result.stderr
    lowercase_file, = json.loads(result.stdout)["files"]
    assert lowercase_file["exempt"] is False and lowercase_file["exceeds_limit"] is True
    unknown = files[".scratch/task/archive-index.json"]
    assert unknown["bytes"] is None and unknown["exceeds_limit"] is None
    assert files[".scratch/task/check-runs.tar.xz"]["parts"] == {
        "count": 5, "first": ".scratch/task/check-runs.tar.xz.part-01",
        "last": ".scratch/task/check-runs.tar.xz.part-05", "full_part_bytes": 20_000, "last_part_bytes": 2264,
    }
    assert index.read_bytes() == before
    assert not list((repository / ".scratch").glob("evidence-plan-*"))


def test_plan_round_trips_chinese_paths_when_stdout_uses_cp950(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ".scratch/task/证据.json"
    exemptions = stage_exemptions(repository)
    with exemptions.open("a", encoding="utf-8") as stream:
        stream.write(f"\n{target}\n")
    assert git(repository, "add", "--", str(exemptions)).returncode == 0
    monkeypatch.setenv("PYTHONIOENCODING", "cp950")

    result = plan(repository, "--file", target, "20001")

    assert result.returncode == 0, result.stderr
    row, = json.loads(result.stdout)["files"]
    assert row["path"] == target
    assert row["exempt"] is True
    assert row["exceeds_limit"] is False


def test_measurement_updates_archive_parts_using_observed_bytes(repository: Path) -> None:
    stage_exemptions(repository)
    archive = repository / "observed.tar.xz"
    receipt = repository / "observed.json"
    archive.write_bytes(b"x" * 20_000)
    receipt.write_bytes(b"x" * 20_001)
    target = ".scratch/task/check-runs.tar.xz"
    receipt_target = ".scratch/task/copied.json"
    result = plan(repository, "--measure", "--file", receipt_target, receipt.name, "--archive", target, archive.name)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["basis"] == "measured_file_bytes"
    receipt_plan, archive_plan = report["files"]
    assert receipt_plan["bytes"] == 20_001 and receipt_plan["exceeds_limit"] is True
    assert archive_plan["bytes"] == 20_000 and "parts" not in archive_plan

    archive.write_bytes(b"x" * 20_001)
    result = plan(repository, "--measure", "--archive", target, str(archive))
    assert result.returncode == 0, result.stderr
    archive_plan, = json.loads(result.stdout)["files"]
    assert archive_plan["parts"]["count"] == 2
    assert archive_plan["parts"]["last_part_bytes"] == 1


def test_plan_rejects_a_file_that_would_overwrite_an_archive_part(repository: Path) -> None:
    target = ".scratch/task/check-runs.tar.xz"
    result = plan(repository, "--archive", target, "20001", "--file", f"{target}.part-01", "1")
    assert result.returncode == 2
    assert f"{target}.part-01" in result.stderr


def test_plan_uses_the_reported_part_names_to_check_conflicts(repository: Path) -> None:
    target = ".scratch/task/check-runs.tar.xz"
    arguments = ("--archive", target, "1980001")
    result = plan(repository, *arguments, "--file", f"{target}.part-001", "1")
    assert result.returncode == 0, result.stderr
    files = {row["path"]: row for row in json.loads(result.stdout)["files"]}
    parts = files[target]["parts"]
    assert parts["first"] == f"{target}.part-01"
    assert parts["last"] == f"{target}.part-100"

    result = plan(repository, *arguments, "--file", parts["last"], "1")
    assert result.returncode == 2
    assert parts["last"] in result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows destination paths are case-insensitive")
def test_plan_rejects_windows_case_alias_of_an_archive_part(repository: Path) -> None:
    target = ".scratch/task/check-runs.tar.xz"
    alias = ".scratch/task/CHECK-RUNS.TAR.XZ.part-01"
    result = plan(repository, "--archive", target, "20001", "--file", alias, "1")
    assert result.returncode == 2
    assert alias.casefold() in result.stderr.casefold()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows aliases paths with trailing dots or spaces")
@pytest.mark.parametrize(
    "alias",
    [
        ".scratch/task/check-runs.tar.xz.part-01.",
        ".scratch/task/check-runs.tar.xz.part-01 ",
        ".scratch/task./check-runs.tar.xz.part-01",
        ".scratch/task /check-runs.tar.xz.part-01",
    ],
    ids=["file-dot", "file-space", "directory-dot", "directory-space"],
)
def test_plan_rejects_windows_trailing_dot_or_space_alias_of_an_archive_part(repository: Path, alias: str) -> None:
    target = ".scratch/task/check-runs.tar.xz"
    result = plan(repository, "--archive", target, "20001", "--file", alias, "1")
    assert result.returncode == 2
    assert alias.rstrip(" ") in result.stderr


def test_plan_rejects_a_file_that_would_block_the_archive_parts_directory(repository: Path) -> None:
    directory = ".scratch/task/check-runs"
    result = plan(repository, "--file", directory, "1", "--archive", f"{directory}/nested.tar.xz", "31000")
    assert result.returncode == 2
    assert directory in result.stderr


def test_plan_allows_a_directory_at_the_unsplit_archive_path(repository: Path) -> None:
    target = ".scratch/task/check-runs.tar.xz"
    manifest = f"{target}/archive-index.json"
    arguments = ("--archive", target, "20001", "--file", manifest, "1")
    result = plan(repository, *arguments)
    assert result.returncode == 0, result.stderr
    files = {row["path"]: row for row in json.loads(result.stdout)["files"]}
    assert files[manifest]["exceeds_limit"] is False
    assert files[target]["parts"]["first"] == f"{target}.part-01"

    exemptions = stage_exemptions(repository)
    with exemptions.open("a", encoding="utf-8") as stream:
        stream.write(f"\n{target}\n")
    assert git(repository, "add", "--", str(exemptions)).returncode == 0
    result = plan(repository, *arguments)
    assert result.returncode == 2
    assert target in result.stderr and manifest in result.stderr


def test_regular_file_with_dot_stem_and_part_suffix_is_allowed(repository: Path) -> None:
    stage_exemptions(repository)
    result = plan(repository, "--file", ".scratch/task/..part-01", "1")
    assert result.returncode == 0, result.stderr


def test_exemption_lookup_records_ownership_before_creating_the_index(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage_exemptions(repository)
    for key in GIT_LOCAL_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MYRK_TASK_ID", "evidence-planning-test")
    for key in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(key, str(repository))
    monkeypatch.chdir(repository)
    result_path = ".scratch/check-runs/results/b-planned.json"
    monkeypatch.setattr(sys, "argv", ["plan_check_evidence.py", "--file", result_path, "20001"])
    git_output = pre_commit.git_output
    index_directory = None
    index_commands = {"hash-object", "update-index", "ls-files"}
    observed_commands: set[str] = set()

    def inspect_index(root: Path, *args: str, input_data: bytes | None = None,
                      env: dict[str, str] | None = None) -> bytes:
        nonlocal index_directory
        if commands := index_commands.intersection(args):
            observed_commands.update(commands)
            assert env is not None
            directory = Path(env["TEMP"])
            assert directory.parent == repository.resolve() / ".scratch"
            assert all(Path(env[key]) == directory for key in ("TMP", "TMPDIR"))
            if "hash-object" not in args:
                assert Path(env["GIT_INDEX_FILE"]).parent == directory
            if index_directory is not None:
                assert index_directory == directory
            index_directory = directory
            owner = json.loads((index_directory / "owner.json").read_text(encoding="utf-8"))
            assert owner["owner"] == "check-evidence-planner"
            assert owner["task_id"] == "evidence-planning-test"
            assert Path(owner["project_root"]) == repository.resolve()
            assert Path(owner["path"]) == index_directory
            assert owner["purpose"]
            assert datetime.fromisoformat(owner["created_at"]).utcoffset() is not None
        return git_output(root, *args, input_data=input_data, env=env)

    monkeypatch.setattr(plan_check_evidence, "git_output", inspect_index)
    monkeypatch.setattr(pre_commit, "git_output", inspect_index)
    assert plan_check_evidence.main() == 0
    assert observed_commands == index_commands
    assert index_directory is not None and not index_directory.exists()
