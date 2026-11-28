"""Check borrowed acceptance evidence through both file closeout commands."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.test_closeout import cli, git, manifest, repo, snapshot
from tests.test_closeout_runs import prepare_runner, run_bare_pytest, run_check


def pinned(path: Path) -> dict:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def declare_borrowing(repository: Path, baseline: Path, reference: dict) -> Path:
    candidates = json.loads(baseline.read_text(encoding="utf-8"))["files"]
    resources = manifest(repository, baseline, [
        {"outcome": "external", "owner": "producer", "reason": "Original check evidence", **pinned(Path(item["path"]))}
        for item in candidates
    ])
    update_borrowing(repository, resources, reference)
    return resources


def update_borrowing(repository: Path, resources: Path, reference: dict) -> None:
    document = json.loads(resources.read_text(encoding="utf-8"))
    document["closeout"]["borrowed_checks"] = [reference]
    resources.write_text(json.dumps(document), encoding="utf-8")
    git(repository, "add", "-f", "--", str(resources))


def test_borrowed_result_rejects_claimed_coverage_that_the_origin_did_not_run(repo: Path) -> None:
    prepare_runner(repo)
    arguments = ["tests/test_first.py", "-q"]
    completed = run_check(repo, "producer", "pytest", *arguments)
    assert completed["exit_code"] == 0, completed
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    baseline = snapshot(repo, "--verify-runs")
    reference = {
        "requirement": "second test passes", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": "pytest", "source_root": str(repo),
        "source_version": launch["source_version"],
        "coverage": {**launch["coverage"], "arguments": ["tests/test_second.py", "-q"]},
        "receipt": pinned(Path(completed["result_path"])), "origin": pinned(origin), "artifacts": [],
    }
    resources = declare_borrowing(repo, baseline, reference)
    git(repo, "commit", "-m", "Record the proposed borrowing")

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("second test passes" in error and "coverage" in error and str(origin) in error for error in report["errors"])


@pytest.mark.parametrize("mode", ["pytest", "bare-pytest"])
def test_collection_only_cannot_satisfy_a_passing_requirement(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    prepare_runner(repo)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    arguments = ("tests/test_first.py", "-q")
    completed = run_bare_pytest(repo, "producer", *arguments) if mode == "bare-pytest" else run_check(repo, "producer", mode, *arguments)
    assert completed["exit_code"] == 0, completed
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    reference = {
        "requirement": "first test executes and passes", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": mode, "source_root": str(repo),
        "source_version": launch["source_version"], "coverage": launch["coverage"],
        "receipt": pinned(Path(completed["result_path"])), "origin": pinned(origin),
    }
    resources = declare_borrowing(repo, snapshot(repo, "--verify-runs"), reference)
    git(repo, "commit", "-m", "Reference collection as a passing run")

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("actual status is collected" in error for error in report["errors"])


@pytest.mark.parametrize("mode", ["pytest", "bare-pytest"])
def test_pytest_coverage_reflects_effective_options_and_explicit_configuration(
    repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    revision = prepare_runner(repo)
    config = repo / "checks.ini"
    configuration = "[pytest]\ncache_dir = .scratch/cache\naddopts = -k second\n"
    config.write_text(configuration, encoding="utf-8")
    arguments = ("-c", config.name, "tests", "-q")
    observations = []
    for selector in ("first", "second"):
        if selector == "first":
            monkeypatch.setenv("PYTEST_ADDOPTS", "-k first")
        else:
            monkeypatch.delenv("PYTEST_ADDOPTS")
        result = run_bare_pytest(repo, "producer", *arguments) if mode == "bare-pytest" else run_check(repo, "producer", mode, *arguments)
        assert result["exit_code"] == 0, result
        origin = repo / ".scratch/check-runs/launches" / f"{result['invocation_id']}.json"
        launch = json.loads(origin.read_text(encoding="utf-8"))
        observations.append(launch["coverage"]["pytest"]["selection_sha256"])
        assert launch["coverage"]["pytest"]["keyword"] == selector
        assert launch["coverage"]["pytest"]["node_count"] == 1
        assert launch["source_version"] == {"commit": revision, "working_tree": "clean", "pytest_config": pinned(config)}
        if selector == "first":
            config.write_text(configuration + "# Changed bytes\n", encoding="utf-8")
    assert observations[0] != observations[1]


def test_changed_pytest_configuration_cannot_produce_a_passing_receipt(repo: Path) -> None:
    prepare_runner(repo)
    config = repo / "checks.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    probe = repo / "tests/test_config_change.py"
    probe.write_text(
        "from pathlib import Path\n"
        "def test_config_changes():\n"
        "    Path('checks.ini').write_text('[pytest]\\naddopts = -k another\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    completed = run_check(repo, "producer", "pytest", "-c", config.name, str(probe), "-q")
    assert completed["exit_code"] != 0, completed
    assert completed["pytest_config_unchanged"] is False


@pytest.mark.parametrize("mode", ["pytest", "bare-pytest"])
def test_borrowed_pytest_binds_ignored_selected_files_and_checks_for_drift(repo: Path, mode: str) -> None:
    prepare_runner(repo)
    probe = repo / ".scratch/test_probe.py"
    probe.parent.mkdir()

    def launch_probe() -> tuple[dict, Path, dict]:
        arguments = (str(probe), "-q")
        result = (run_bare_pytest(repo, "producer", *arguments) if mode == "bare-pytest"
                  else run_check(repo, "producer", mode, *arguments))
        assert result["exit_code"] == 0, result
        origin = repo / ".scratch/check-runs/launches" / f"{result['invocation_id']}.json"
        return result, origin, json.loads(origin.read_text(encoding="utf-8"))

    probe.write_text("def test_probe():\n    assert 1 == 1\n", encoding="utf-8")
    _, _, first = launch_probe()
    probe.write_text("def test_probe():\n    assert 2 == 2\n", encoding="utf-8")
    second_digest = pinned(probe)["sha256"]
    completed, origin, second = launch_probe()
    assert first["source_version"] == second["source_version"]
    reference = {
        "requirement": "the original ignored test passes", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": mode, "source_root": str(repo),
        "source_version": first["source_version"], "coverage": first["coverage"],
        "receipt": pinned(Path(completed["result_path"])), "origin": pinned(origin),
    }
    resources = declare_borrowing(repo, snapshot(repo, "--verify-runs"), reference)
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 1, report
    assert any("expected coverage differs" in error for error in report["errors"])
    assert second["coverage"]["pytest"]["selected_files"] == {".scratch/test_probe.py": second_digest}

    probe.write_text(
        "from pathlib import Path\n"
        "def test_probe():\n"
        "    Path(__file__).write_text('def test_probe(): pass\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _, _, changed = launch_probe()
    assert changed["terminal"]["status"] == "inputs_unverified"


@pytest.mark.parametrize("change", ["edited", "removed"])
def test_borrowing_rejects_a_report_changed_before_first_reference(repo: Path, change: str) -> None:
    prepare_runner(repo)
    completed = run_check(repo, "producer", "pytest", "tests/test_first.py", "--junitxml=first.xml", "-q")
    assert completed["exit_code"] == 0, completed
    receipt = Path(completed["result_path"])
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    artifact = Path(completed["retained_reports"]["paths"][0]) / "first.xml"
    process, audit = cli(repo, "audit")
    assert process.returncode == 0, audit
    ownership = next(item for item in audit["records"] if item["path"] == str(artifact))
    assert ownership["task_id"] == "producer" and ownership["settled_fingerprint_matches"] is True
    if change == "edited":
        artifact.write_bytes(artifact.read_bytes() + b"\n")
    else:
        artifact.unlink()
    reference = {
        "requirement": "first test passes with original report", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": "pytest", "source_root": str(repo),
        "source_version": launch["source_version"], "coverage": launch["coverage"],
        "receipt": pinned(receipt), "origin": pinned(origin), "artifacts": [pinned(artifact)] if artifact.exists() else [],
    }
    resources = declare_borrowing(repo, snapshot(repo, "--verify-runs"), reference)
    git(repo, "commit", "-m", "Borrow the original report")

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any(str(artifact) in error and ("fingerprint" in error or "missing" in error) for error in report["errors"])


@pytest.mark.parametrize("problem", ["receipt", "changed_artifact", "missing_artifact", "version", "pending"])
def test_borrowed_check_requires_unchanged_final_originals(repo: Path, problem: str) -> None:
    prepare_runner(repo)
    completed = run_check(repo, "producer", "pytest", "tests/test_first.py", "--junitxml=test-report.xml", "-q")
    assert completed["exit_code"] == 0, completed
    receipt = Path(completed["result_path"])
    artifact = next(Path(completed["retained_reports"]["paths"][0]).glob("*.xml"))
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    reference = {
        "requirement": "first test passes", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": "pytest", "source_root": str(repo),
        "source_version": launch["source_version"].copy(), "coverage": launch["coverage"],
        "receipt": pinned(receipt), "origin": pinned(origin), "artifacts": [pinned(artifact)],
    }
    baseline = snapshot(repo, "--verify-runs")
    resources = declare_borrowing(repo, baseline, reference)
    git(repo, "commit", "-m", "Borrow the original check evidence")

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 0, report
        assert report["check_runs"] == []
        borrowed, = report["borrowed_checks"]
        assert borrowed["status"] == "passed"
        assert borrowed["original_task_id"] == "producer"
        assert borrowed["receipt"]["path"] == str(receipt)
        assert set(report["borrowed_paths"]) == {str(receipt), str(origin), str(artifact)}

    if problem == "receipt":
        receipt.write_bytes(receipt.read_bytes() + b"\n")
        path, reason = receipt, "differs from the settled launch"
    elif problem == "changed_artifact":
        artifact.write_bytes(artifact.read_bytes() + b"\n")
        path, reason = artifact, "fingerprint changed"
    elif problem == "missing_artifact":
        artifact.unlink()
        path, reason = artifact, "missing"
    elif problem == "version":
        reference["source_version"]["commit"] = "f" * 40
        update_borrowing(repo, resources, reference)
        git(repo, "commit", "-m", "Point at an untested version")
        path, reason = origin, "source version"
    else:
        del launch["terminal"]
        origin.write_text(json.dumps(launch), encoding="utf-8")
        path, reason = receipt, "no settled final receipt"
    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("first test passes" in error and reason in error and str(path) in error for error in report["errors"])


def test_borrowed_legacy_receipt_uses_the_original_archived_run_record(repo: Path) -> None:
    checked_commit = prepare_runner(repo)
    receipt = repo / ".scratch/check-runs/results/b-original.json"
    receipt.parent.mkdir(parents=True)
    prior_run = {
        "owner": "backend-check:b-original", "pid": 10240, "project_root": str(repo),
        "runtime_directory": str(repo / ".scratch/check-runs/runtime-tmp/b-original"),
        "started_at": "2026-11-09T16:40:00+00:00", "finished_at": "2026-11-09T16:40:25+00:00",
        "result_path": str(receipt), "exit_code": 0, "runtime_removed": True, "failure_logs": [],
    }
    receipt.write_text(json.dumps(prior_run), encoding="utf-8")
    origin = repo / ".scratch/producer/original-run.json"
    origin.parent.mkdir()
    version = {"commit": checked_commit, "working_tree": "clean"}
    coverage = {"arguments": ["tests/test_first.py", "-q"]}
    captured = {
        "version": 1, "kind": "historical_check", "invocation_id": "b-original", "mode": "pytest",
        "project_root": str(repo), "source_root": str(repo), "source_version": version, "coverage": coverage,
        "result_path": str(receipt), "started_at": prior_run["started_at"],
        "terminal": {"status": "passed", "result_sha256": pinned(receipt)["sha256"]},
    }
    origin.write_text(json.dumps(captured), encoding="utf-8")
    git(repo, "add", "-f", "--", str(origin))
    git(repo, "commit", "-m", "Producer captures its completed run")
    archive = git(repo, "rev-parse", "HEAD")
    baseline = snapshot(repo, "--verify-runs")
    reference = {
        "requirement": "first test passes", "invocation_id": "b-original", "mode": "pytest",
        "source_root": str(repo), "source_version": version, "coverage": coverage,
        "receipt": pinned(receipt), "origin": {**pinned(origin), "commit": archive},
    }
    resources = declare_borrowing(repo, baseline, reference)
    git(repo, "commit", "-m", "Borrow the unnumbered original from its producer")

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 0, report
        borrowed, = report["borrowed_checks"]
        assert borrowed["status"] == "passed"
        assert borrowed["original_task_id"] is None
        assert borrowed["origin"]["path"] == str(origin)
        assert borrowed["origin"]["commit"] == archive

    captured["coverage"] = {"arguments": ["tests/test_second.py"]}
    origin.write_text(json.dumps(captured), encoding="utf-8")
    reference["coverage"] = captured["coverage"]
    reference["origin"].update(pinned(origin))
    update_borrowing(repo, resources, reference)

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("first test passes" in error and "archived" in error and str(origin) in error for error in report["errors"])


def test_borrowed_parallel_result_requires_the_original_lane_receipts(repo: Path) -> None:
    prepare_runner(repo)
    completed = run_check(repo, "producer", "pytest-parallel", "tests", "-q")
    assert completed["exit_code"] == 0, completed
    receipt = Path(completed["result_path"])
    lane = Path(completed["lanes"]["lane2"]["receipt"]["result_path"])
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    evidence = [file for name in ("launches", "results")
                for file in (repo / ".scratch/check-runs" / name).rglob("*") if file.is_file()]
    reference = {
        "requirement": "both tests pass", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": "pytest-parallel", "source_root": str(repo),
        "source_version": launch["source_version"], "coverage": launch["coverage"],
        "receipt": pinned(receipt), "origin": pinned(origin),
        "artifacts": [pinned(path) for path in evidence if path not in {receipt, origin, lane}],
    }
    baseline = snapshot(repo, "--verify-runs")
    resources = declare_borrowing(repo, baseline, reference)
    git(repo, "commit", "-m", "Omit the original second lane from the borrowing")

    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("both tests pass" in error and "fingerprint" in error and str(lane) in error for error in report["errors"])

    reference["artifacts"].append(pinned(lane))
    update_borrowing(repo, resources, reference)
    git(repo, "commit", "-m", "Bind the original second lane")
    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 0, report
        assert report["borrowed_checks"][0]["status"] == "passed"
    lane.unlink()
    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("both tests pass" in error and "lane2" in error and str(lane) in error for error in report["errors"])


def test_borrowed_failed_check_cannot_satisfy_a_passing_requirement(repo: Path) -> None:
    prepare_runner(repo)
    completed = run_check(repo, "producer", "pytest", "tests/test_missing.py", "-q")
    assert completed["exit_code"] == 4, completed
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    receipt = Path(completed["result_path"])
    reference = {
        "requirement": "selected test passes", "invocation_id": completed["invocation_id"],
        "original_task_id": "producer", "mode": "pytest", "source_root": str(repo),
        "source_version": launch["source_version"], "coverage": launch["coverage"],
        "receipt": pinned(receipt), "origin": pinned(origin),
        "artifacts": [pinned(Path(log)) for log in completed["failure_logs"]],
    }
    resources = declare_borrowing(repo, snapshot(repo, "--verify-runs"), reference)
    git(repo, "commit", "-m", "Propose the check as passing evidence")
    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 1, report
        assert any("selected test passes" in error and "actual status is failed" in error and str(receipt) in error
                   for error in report["errors"])

    reference.update(requirement="the selected test failed", require_pass=False)
    update_borrowing(repo, resources, reference)
    git(repo, "commit", "-m", "Borrow the recorded failure")
    for command in ("preflight", "check"):
        process, report = cli(repo, command, str(resources))
        assert process.returncode == 0, report
        assert report["borrowed_checks"][0]["status"] == "failed"
