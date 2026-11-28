"""Verify durable check evidence through the runner and file closeout commands."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from textwrap import dedent

import pytest

from tests.test_closeout import ROOT, cli, git, manifest, repo, snapshot


def prepare_runner(repository: Path) -> str:
    for name in (
        "scripts/run_checks.py", "scripts/parallel_checks.py", "scripts/pytest_shards.py",
        "myrk/project_paths.py", "myrk/check_outputs.py", "myrk/check_plugin.py", "myrk/windows_jobs.py", "conftest.py",
    ):
        destination = repository / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    (repository / "myrk/__init__.py").touch()
    (repository / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\ncache_dir = ".scratch/check-runs/cache/pytest"\n',
        encoding="utf-8",
    )
    (repository / "tests").mkdir()
    for module in ("first", "second"):
        (repository / f"tests/test_{module}.py").write_text(
            f"def test_{module}():\n    assert True\n", encoding="utf-8",
        )
    git(repository, "add", ".")
    git(repository, "-c", "core.hooksPath=.initial-hooks", "commit", "-m", "Install check commands")
    return git(repository, "rev-parse", "HEAD")


def run_check(repository: Path, task: str | None, *arguments: str, outcome: str = "pass") -> dict:
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", MYRK_TEST_SCHEDULING="duration")
    environment["MYRK_SYNTHETIC_OUTCOME"] = outcome
    for variable in ("MYRK_TASK_ID", "GIT_INDEX_FILE", "PYTHONPATH"):
        environment.pop(variable, None)
    if task is not None:
        environment["MYRK_TASK_ID"] = task
    process = subprocess.run(
        [sys.executable, "-B", "scripts/run_checks.py", *arguments], cwd=repository,
        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert process.stdout, process.stderr
    result = json.loads(process.stdout)
    assert result["exit_code"] == process.returncode
    return result


def run_bare_pytest(repository: Path, task: str, *arguments: str) -> dict:
    results = repository / ".scratch/check-runs/results"
    previous = set(results.glob("p-*.json"))
    process = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *arguments], cwd=repository,
        env=dict(os.environ, MYRK_TASK_ID=task, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(repository)),
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    paths = set(results.glob("p-*.json")) - previous
    assert len(paths) == 1, process.stdout + process.stderr
    receipt = json.loads(paths.pop().read_text(encoding="utf-8"))
    assert receipt["exit_code"] == process.returncode
    return receipt


def save_evidence(repository: Path, baseline: Path) -> Path:
    directory = repository / ".scratch/check-runs"
    evidence = [file for name in ("results", "launches") for file in (directory / name).rglob("*") if file.is_file()]
    files = [{"path": str(file), "outcome": "committed"} for file in evidence]
    resources = manifest(repository, baseline, files)
    git(repository, "add", "-f", "--", *(str(path) for path in evidence))
    return resources


def test_parallel_launch_identifies_its_missing_lane_before_and_after_commit(repo: Path) -> None:
    checked_commit = prepare_runner(repo)
    baseline = snapshot(repo, "--verify-runs")
    completed = run_check(repo, "task-1", "pytest-parallel", "tests", "-q")
    assert completed["exit_code"] == 0, completed
    lane = Path(completed["lanes"]["lane2"]["receipt"]["result_path"])
    resources = save_evidence(repo, baseline)

    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 0, report
    runs = report["check_runs"]
    parent = next(run for run in runs if run["mode"] == "pytest-parallel")
    assert parent["source_version"]["commit"] == checked_commit
    assert parent["status"] == "passed"
    assert parent["coverage"]["node_count"] == 2
    assert any(item["path"] == str(lane) and item["outcome"] == "committed" for run in runs for item in run["evidence"])

    raw = lane.read_bytes()
    lane.unlink()
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 1, report
    assert any("lane2" in error and "missing" in error.lower() and str(lane) in error for error in report["errors"])
    lane.write_bytes(raw)
    git(repo, "commit", "-m", "Save check evidence")

    process, report = cli(repo, "check", str(resources))
    assert process.returncode == 0, report
    lane.unlink()
    process, report = cli(repo, "check", str(resources))
    assert process.returncode == 1, report
    assert any("lane2" in error and "missing" in error.lower() and str(lane) in error for error in report["errors"])
    lane.write_bytes(raw)
    process, report = cli(repo, "check", str(resources))
    assert process.returncode == 0, report


def test_passing_parallel_run_requires_its_sealed_collection_manifest(repo: Path) -> None:
    prepare_runner(repo)
    baseline = snapshot(repo, "--verify-runs")
    completed = run_check(repo, "task-1", "pytest-parallel", "tests", "-q")
    assert completed["exit_code"] == 0, completed
    manifest_path = completed["manifest_path"]
    origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
    launch = json.loads(origin.read_text(encoding="utf-8"))
    del launch["terminal"]["required_artifacts"][manifest_path]
    origin.write_text(json.dumps(launch), encoding="utf-8")
    Path(manifest_path).unlink()
    resources = save_evidence(repo, baseline)

    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 1, report
    assert any("sealed collection manifest" in error and manifest_path in error for error in report["errors"])


def require_runs(repository: Path, resources: Path, *runs: dict, require_pass: bool = False) -> None:
    document = json.loads(resources.read_text(encoding="utf-8"))
    document["closeout"]["check_runs"] = [
        {"invocation_id": run["invocation_id"], "require_pass": require_pass} for run in runs
    ]
    resources.write_text(json.dumps(document), encoding="utf-8")
    git(repository, "add", "-f", "--", str(resources))


@pytest.mark.parametrize("scenario, status", [
    ("legacy-collection", "passed"), ("all-skipped", "no_tests_passed"), ("checkout-drift", "inputs_unverified"),
])
def test_passing_requirement_needs_an_observed_pytest_pass(repo: Path, scenario: str, status: str) -> None:
    prepare_runner(repo)
    if scenario != "legacy-collection":
        probe = ("import pytest\n\ndef test_first():\n    pytest.skip('unavailable')\n" if scenario == "all-skipped" else
                 "from pathlib import Path\n\ndef test_first():\n    Path('myrk/__init__.py').write_text('changed = True\\n')\n")
        (repo / "tests/test_first.py").write_text(probe, encoding="utf-8")
        git(repo, "add", "tests/test_first.py")
        git(repo, "commit", "-m", "Record pytest proof fixture")
    baseline = snapshot(repo, "--verify-runs")
    options = ("--collect-only",) if scenario == "legacy-collection" else ()
    completed = run_check(repo, "task-1", "pytest", "tests/test_first.py", "-q", *options)
    assert completed["exit_code"] == 0, completed
    if scenario == "legacy-collection":
        origin = repo / ".scratch/check-runs/launches" / f"{completed['invocation_id']}.json"
        launch = json.loads(origin.read_text(encoding="utf-8"))
        receipt = Path(completed["result_path"])
        completed.pop("pytest_coverage")
        completed.pop("artifact_inventory_complete")
        raw = json.dumps(completed).encode("utf-8")
        receipt.write_bytes(raw)
        launch["coverage"].pop("pytest")
        launch["terminal"] = {"status": "passed", "result_sha256": hashlib.sha256(raw).hexdigest()}
        origin.write_text(json.dumps(launch), encoding="utf-8")
    if scenario == "checkout-drift":
        assert completed["pytest_coverage"]["checkout_unchanged"] is False
        (repo / "myrk/__init__.py").write_bytes(b"")

    resources = save_evidence(repo, baseline)
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 0, report
    run, = report["check_runs"]
    assert run["status"] == status

    require_runs(repo, resources, completed, require_pass=True)
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 1, report
    assert any("passing result" in error or "verified pytest execution" in error for error in report["errors"])


def test_failure_and_interruption_remain_visible_after_a_passing_retry(repo: Path) -> None:
    prepare_runner(repo)
    probe = repo / "tests/test_outcome.py"
    probe.write_text(
        "import os\n\ndef test_configured_outcome():\n"
        "    outcome = os.environ['MYRK_SYNTHETIC_OUTCOME']\n"
        "    if outcome == 'interrupt':\n        raise KeyboardInterrupt()\n"
        "    assert outcome == 'pass'\n", encoding="utf-8",
    )
    git(repo, "add", "--", str(probe))
    git(repo, "commit", "-m", "Save outcome probe")
    baseline = snapshot(repo, "--verify-runs")
    arguments = ("pytest", "tests/test_outcome.py", "-q")
    failed = run_check(repo, "task-1", *arguments, outcome="fail")
    interrupted = run_check(repo, "task-1", *arguments, outcome="interrupt")
    passed = run_check(repo, "task-1", *arguments)
    assert (failed["exit_code"], interrupted["exit_code"], passed["exit_code"]) == (1, 2, 0)
    resources = save_evidence(repo, baseline)
    require_runs(repo, resources, passed, require_pass=True)

    def check_statuses(phase: str) -> None:
        process, report = cli(repo, phase, str(resources))
        assert process.returncode == 0, report
        runs = {run["invocation_id"]: run for run in report["check_runs"]}
        for check, status in ((failed, "failed"), (interrupted, "interrupted"), (passed, "passed")):
            run = runs[check["invocation_id"]]
            assert run["status"] == status
            assert run["coverage"]["arguments"] == list(arguments[1:])
            evidence = {item["path"]: item["outcome"] for item in run["evidence"]}
            assert evidence[check["result_path"]] == "committed"
            for log in check["failure_logs"]:
                assert evidence[log] == "committed"

    check_statuses("preflight")
    git(repo, "commit", "-m", "Save the executions and passing requirement")
    check_statuses("check")
    require_runs(repo, resources, failed, interrupted, require_pass=True)
    for phase in ("preflight", "check"):
        process, report = cli(repo, phase, str(resources))
        assert process.returncode == 1, report
        for check, status in ((failed, "failed"), (interrupted, "interrupted")):
            assert any(check["invocation_id"] in error and f"actual status is {status}" in error for error in report["errors"])


def test_same_checkout_unnumbered_run_needs_exact_launch_association(repo: Path) -> None:
    prepare_runner(repo)
    baseline = snapshot(repo, "--verify-runs")
    run = run_check(repo, None, "pytest", "tests/test_first.py", "-q")
    assert run["exit_code"] == 0, run
    assert "task_id" not in run
    resources = save_evidence(repo, baseline)
    git(repo, "commit", "-m", "Preserve possible task evidence")

    for phase in ("preflight", "check"):
        process, report = cli(repo, phase, str(resources))
        assert process.returncode == 1, report
        assert any(run["invocation_id"] in error and "ownership unresolved" in error for error in report["errors"])

    require_runs(repo, resources, run)
    launch = repo / ".scratch/check-runs/launches" / f"{run['invocation_id']}.json"
    original = launch.read_bytes()
    launch.unlink()
    for phase in ("preflight", "check"):
        process, report = cli(repo, phase, str(resources))
        assert process.returncode == 1, report
        assert any(run["result_path"] in error and "without a matching launch" in error for error in report["errors"])
    launch.write_bytes(original)

    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 0, report
    git(repo, "commit", "-m", "Bind the verified unnumbered launch to this task")
    process, report = cli(repo, "check", str(resources))
    assert process.returncode == 0, report
    assert [item["invocation_id"] for item in report["check_runs"]] == [run["invocation_id"]]


def test_direct_pytest_uses_the_same_launch_and_closeout_evidence(repo: Path) -> None:
    prepare_runner(repo)
    baseline = snapshot(repo, "--verify-runs")
    result = run_bare_pytest(repo, "task-1", "tests/test_first.py", "-q")
    assert result["exit_code"] == 0, result
    resources = save_evidence(repo, baseline)
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 0, report
    assert [item["mode"] for item in report["check_runs"]] == ["bare-pytest"]
    assert report["check_runs"][0]["status"] == "passed"


def prepare_desktop_runner(repo: Path) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("The desktop check needs Node.js.")
    for name in ("run-tests.mjs", "run-context.mjs", "project-paths.mjs", "test-timings.mjs", "test-receipts.mjs"):
        source = ROOT / "desktop/tests" / name
        if source.is_file():
            destination = repo / "desktop/tests" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    git(repo, "add", "desktop")
    git(repo, "commit", "-m", "Install the desktop check command")
    return node


def test_desktop_command_records_a_startup_failure_for_closeout(repo: Path) -> None:
    node = prepare_desktop_runner(repo)
    baseline = snapshot(repo, "--verify-runs")
    environment = dict(os.environ, MYRK_TASK_ID="task-1", MYRK_TEST_SCHEDULING="invalid")
    environment.pop("MYRK_TEST_RUN_PARENT", None)
    process = subprocess.run(
        [node, "desktop/tests/run-tests.mjs"], cwd=repo, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert process.returncode == 1, process.stdout + process.stderr
    assert "MYRK_TEST_SCHEDULING must be legacy or duration" in process.stderr
    resources = save_evidence(repo, baseline)
    process, report = cli(repo, "preflight", str(resources))
    assert process.returncode == 0, report
    assert [(item["mode"], item["status"]) for item in report["check_runs"]] == [("desktop", "startup_failed")]


def test_desktop_foreground_ctrl_c_settles_an_interrupted_receipt(repo: Path) -> None:
    node = prepare_desktop_runner(repo)
    (repo / "desktop/package-lock.json").write_text("{}", encoding="utf-8")
    vitest = repo / "desktop/node_modules/vitest"
    vitest.mkdir(parents=True)
    (vitest / "package.json").write_text('{"type":"module","exports":{"./node":"./node.mjs"}}', encoding="utf-8")
    (vitest / "node.mjs").write_text("export const parseCLI = () => ({filter: ['wait.test.mjs'], options: {}})\n", encoding="utf-8")
    ready = repo / "desktop/tests/ctrl-c-ready"
    (ready.parent / "wait.test.mjs").write_text(
        "import test from 'node:test'\n"
        "import { writeFile } from 'node:fs/promises'\n"
        "test('foreground contract', async () => {\n"
        "  await writeFile(new URL('./ctrl-c-ready', import.meta.url), '')\n"
        "  await new Promise(resolve => setTimeout(resolve, 12000))\n"
        "})\n",
        encoding="utf-8",
    )
    controller = dedent("""\
        import ctypes, subprocess, sys
        from ctypes import wintypes
        from pathlib import Path
        from time import monotonic, sleep

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        kernel.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
        kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        handler = handler_type(lambda event: event == 0)
        assert kernel.SetConsoleCtrlHandler(handler, True), ctypes.get_last_error()
        child = subprocess.Popen([sys.argv[1], 'desktop/tests/run-tests.mjs', 'wait.test.mjs'])
        try:
            deadline = monotonic() + 15
            while not Path(sys.argv[2]).exists():
                assert child.poll() is None and monotonic() < deadline, 'The desktop check did not dispatch its test'
                sleep(.01)
            assert kernel.GenerateConsoleCtrlEvent(0, 0), ctypes.get_last_error()
            assert child.wait(timeout=15) != 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        """)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    environment = dict(os.environ, MYRK_TASK_ID="task-1", MYRK_TEST_SCHEDULING="duration")
    environment.pop("MYRK_TEST_RUN_PARENT", None)
    process = subprocess.run(
        [sys._base_executable, "-B", "-c", controller, node, str(ready)], cwd=repo, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=40,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "Myrk check result:" in process.stdout, process.stdout + process.stderr
    launch_path, = (repo / ".scratch/check-runs/launches").glob("*.json")
    launch = json.loads(launch_path.read_text(encoding="utf-8"))
    receipt_path = Path(launch["result_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert launch["terminal"]["status"] == "interrupted"
    assert receipt["result"] != 0
    assert receipt["scheduling"]["reason"] == "interrupted"
