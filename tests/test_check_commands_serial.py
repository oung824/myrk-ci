import json
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
from myrk.project_paths import project_root

ROOT = project_root(SOURCE_ROOT)


@pytest.mark.parametrize(("check", "arguments"), [
    ("pytest", ["--junitxml=reports/result.xml"]),
    ("pytest", ["--junit-xml", "reports/result.xml"]),
    ("pytest", ["--log-file", "reports/check.log"]),
    ("pytest", ["--html=reports/check.html"]),
    ("pytest", ["--debug"]),
    ("pytest", ["--debug=reports/trace.log"]),
    ("pytest", ["--debug", "reports/trace.log"]),
    ("pytest", ["--cov-report", "xml:reports/coverage.xml"]),
    ("pytest", ["--cov-report=html:reports/coverage"]),
    ("mypy", ["--html-report", "reports/types"]),
    ("mypy", ["--junit-xml=reports/types.xml"]),
    ("mypy", ["--linecount-report", "reports/counts"]),
    ("mypy", ["--junit-x=reports/types.xml"]),
    ("mypy", ["--html-r", "reports/types"]),
    ("mypy", ["--linecount-r", "reports/counts"]),
])
def test_check_reports_are_created_only_in_the_owned_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], check: str, arguments: list[str],
) -> None:
    from scripts.run_checks import main

    class FinishedProcess:
        pid = os.getpid()
        returncode = 0
        stdin = None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: FinishedProcess())
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FinishedProcess())
    monkeypatch.setattr(sys, "argv", ["run_checks.py", check, *arguments])

    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    command = result["commands"][0]["argv"]
    option = arguments[0].partition("=")[0]
    position = next(index for index, value in enumerate(command) if value.partition("=")[0].startswith(option))
    output = command[position].partition("=")[2] if "=" in command[position] else command[position + 1]
    if output.startswith(("xml:", "html:")):
        output = output.partition(":")[2]
    assert Path(output).is_relative_to(Path(result["runtime_directory"]) / "reports")


def test_backend_command_keeps_generated_files_in_its_run_and_removes_them(tmp_path: Path) -> None:
    probe = tmp_path / "test_environment.py"
    observation = tmp_path / "observation.json"
    probe.write_text(
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "def test_environment(tmp_path):\n"
        "    run = Path(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n"
        "    paths = [tmp_path, Path(tempfile.gettempdir())]\n"
        "    paths += [Path(os.environ[key]) for key in "
        "('TEMP', 'TMP', 'TMPDIR', 'MYPY_CACHE_DIR', 'NPM_CONFIG_CACHE', 'MYRK_RUNTIME_ROOT')]\n"
        "    assert all(path.is_relative_to(run) for path in paths)\n"
        "    assert os.environ['PYTHONDONTWRITEBYTECODE'] == '1'\n"
        "    assert (run / 'owner.json').is_file()\n"
        "    Path(os.environ['MYRK_CHECK_PROBE']).write_text(json.dumps({'run': str(run)}))\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q"],
        cwd=SOURCE_ROOT, env=dict(os.environ, MYRK_CHECK_PROBE=str(observation)),
        capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    observed = json.loads(observation.read_text(encoding="utf-8"))
    assert Path(observed["run"]).is_relative_to(ROOT / ".scratch/check-runs/runtime-tmp")
    assert not Path(observed["run"]).exists()
    assert result["exit_code"] == 0
    assert result["runtime_removed"] is True
    assert Path(result["result_path"]).is_file()
    saved = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert saved["commands"][0]["exit_code"] == 0
    assert saved["commands"][0]["started_at"] <= saved["commands"][0]["finished_at"]


def test_failed_backend_command_keeps_only_diagnostic_logs(tmp_path: Path) -> None:
    probe = tmp_path / "test_failure.py"
    probe.write_text(
        "def test_failure(tmp_path):\n"
        "    (tmp_path / 'rebuildable.txt').write_text('temporary fixture')\n"
        "    assert False, 'original failure evidence'\n", encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q"],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 1
    result = json.loads(process.stdout)
    assert result["runtime_removed"] is True
    assert not Path(result["runtime_directory"]).exists()
    assert result["retained"]["owner"]
    assert result["retained"]["reason"]
    assert result["retained"]["delete_when"]
    logs = [Path(path) for path in result["failure_logs"]]
    assert len(logs) == 2
    assert all(path.is_relative_to(ROOT / ".scratch/check-runs/results") for path in logs)
    assert "original failure evidence" in logs[0].read_text(encoding="utf-8")


def test_successful_check_preserves_the_explicit_report_after_runtime_removal(tmp_path: Path) -> None:
    probe = tmp_path / "test_report.py"
    probe.write_text("def test_report():\n    assert True\n", encoding="utf-8")
    requested = tmp_path / "requested.xml"
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q", f"--junitxml={requested}"],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["runtime_removed"] is True
    assert not requested.exists()
    retained = result["retained_reports"]
    assert retained["owner"] and retained["reason"] and retained["delete_when"]
    directory = Path(retained["paths"][0])
    reports = list(directory.rglob("requested.xml"))
    assert len(reports) == 1
    assert "testsuite" in reports[0].read_text(encoding="utf-8")
    assert directory.is_relative_to(ROOT / ".scratch/check-runs/results")


def test_mypy_configuration_reports_also_stay_in_the_owned_run(tmp_path: Path) -> None:
    source = tmp_path / "checked.py"
    source.write_text("value: int = 1\n", encoding="utf-8")
    requested = tmp_path / "configured-types.xml"
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\njunit_xml = " + str(requested) + "\n", encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "mypy", "--config-file", str(config), str(source)],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert not requested.exists()
    reports = Path(result["retained_reports"]["paths"][0])
    assert len(list(reports.rglob("configured-types.xml"))) == 1
    assert result["runtime_removed"] is True


@pytest.mark.parametrize("inside_source", [False, True])
def test_default_pytest_debug_log_is_preserved_inside_results(tmp_path: Path, inside_source: bool) -> None:
    probe = SOURCE_ROOT / ".scratch" / ("test_managed_debug_" + uuid4().hex + ".py") if inside_source else tmp_path / "test_debug.py"
    probe.write_text("def test_debug():\n    assert True\n", encoding="utf-8")
    unowned = SOURCE_ROOT / "pytestdebug.log"
    assert not unowned.exists()
    try:
        process = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q", "--debug"],
            cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        assert process.returncode == 0, process.stdout + process.stderr
        result = json.loads(process.stdout)
        assert not unowned.exists()
        assert result["runtime_removed"] is True
        reports = Path(result["retained_reports"]["paths"][0])
        debug = list(reports.rglob("pytestdebug.log"))
        assert len(debug) == 1
        assert "pytest" in debug[0].read_text(encoding="utf-8")
    finally:
        unowned.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def test_external_pytest_debug_log_is_rejected_before_tool_start(tmp_path: Path) -> None:
    external = ROOT.parent / ("uncreated-debug-" + uuid4().hex) / "debug.log"
    assert not external.parent.exists()
    probe = tmp_path / "test_never_debug.py"
    probe.write_text("def test_never_debug():\n    assert False, 'must not execute'\n", encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q", "--debug=" + str(external)],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode != 0
    result = json.loads(process.stdout)
    assert "Output path must be inside the project" in result["commands"][0]["startup_error"]
    assert result["runtime_removed"] is True
    assert not external.parent.exists()


@pytest.mark.parametrize("report", ["junitxml", "debug"])
def test_external_probe_rejects_environment_report_before_any_write(tmp_path: Path, report: str) -> None:
    probe = tmp_path / "test_unowned_environment.py"
    probe.write_text("raise AssertionError('Unsafe report must not collect this file')\n", encoding="utf-8")
    outside = ROOT.parent / ("unsafe-environment-report-" + uuid4().hex + ".xml")
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q", "-p", "tests.support.check_output_audit"],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTEST_ADDOPTS=f"--{report}={outside.as_posix()}", MYRK_CHECK_BLOCKED_OUTPUT=str(outside)),
    )

    assert process.returncode != 0
    result = json.loads(process.stdout)
    errors = Path(result["failure_logs"][1]).read_text(encoding="utf-8")
    expected = "Output path must be inside the project" if report == "junitxml" else "Test output must be inside the project's .scratch"
    assert expected in errors
    assert "Test guard prevented" not in errors
    assert "Unsafe report must not collect" not in errors
    assert not outside.exists()


@pytest.mark.parametrize("arguments", [["--noconftest"], ["-p", "no:myrk.check_plugin"], ["-pno:myrk.check_plugin"], ["@hidden-arguments.txt"]])
@pytest.mark.parametrize("via_environment", [False, True])
def test_wrapper_rejects_disabling_its_pytest_protection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str], via_environment: bool,
) -> None:
    from scripts.run_checks import main

    def must_not_start(*args: object, **kwargs: object) -> None:
        raise AssertionError("Protection overrides must be rejected before process creation")

    monkeypatch.setattr(subprocess, "Popen", must_not_start)
    monkeypatch.setattr(sys, "argv", ["run_checks.py", "pytest", *([] if via_environment else arguments)])
    monkeypatch.setenv("PYTEST_ADDOPTS", " ".join(arguments) if via_environment else "")
    assert main() != 0
    result = json.loads(capsys.readouterr().out)
    error = result["commands"][0]["startup_error"]
    assert "protection cannot be disabled" in error or "expand their arguments" in error
    assert result["runtime_removed"] is True


@pytest.mark.parametrize("explicit_configuration", [False, True])
def test_wrapper_keeps_configuration_and_imports_in_its_source_worktree(tmp_path: Path, explicit_configuration: bool) -> None:
    probe = tmp_path / "test_configuration.py"
    probe.write_text(
        "from pathlib import Path\nimport myrk.check_plugin\n"
        "def test_configuration(request):\n"
        "    source = Path(myrk.check_plugin.__file__).resolve().parents[1]\n"
        "    assert source == Path(" + repr(str(SOURCE_ROOT)) + ")\n"
        "    assert request.config.getini('pythonpath') == [source]\n"
        "    assert Path(request.config.inipath) == Path(" + repr(str(tmp_path / "explicit.ini" if explicit_configuration else SOURCE_ROOT / "pyproject.toml")) + ")\n",
        encoding="utf-8",
    )
    arguments = []
    if explicit_configuration:
        configuration = tmp_path / "explicit.ini"
        configuration.write_text("[pytest]\n", encoding="utf-8")
        arguments = ["-c", str(configuration)]
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/run_checks.py"), "pytest", str(probe), "-q", *arguments],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout)["runtime_removed"] is True


@pytest.mark.parametrize(("check", "flag", "style"), [
    ("pytest", "--junitxml", "outside"), ("pytest", "--junit-xml", "outside"),
    ("pytest", "--log-file", "parent"), ("pytest", "--html", "junction"),
    ("pytest", "--cov-report", "outside"), ("pytest", "--override-ini", "outside"),
    ("mypy", "--html-report", "outside"), ("mypy", "--xml-report", "parent"),
    ("mypy", "--lineprecision-report", "junction"),
])
def test_check_rejects_unsafe_reports_before_starting_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], check: str, flag: str, style: str,
) -> None:
    from scripts.run_checks import main

    requested = str(ROOT.parent / "unowned-report.xml") if style == "outside" else "../unowned-report.xml"
    if style == "junction":
        link = tmp_path / "report-link"
        target = tmp_path / "ordinary"
        target.mkdir()
        linked = subprocess.run(
            ["C:/Program Files/PowerShell/7/pwsh.exe", "-NoProfile", "-Command",
             "New-Item -ItemType Junction -Path $env:MYRK_LINK_SOURCE -Target $env:MYRK_LINK_TARGET | Out-Null"],
            env=dict(os.environ, MYRK_LINK_SOURCE=str(link), MYRK_LINK_TARGET=str(target)),
            capture_output=True, text=True, encoding="utf-8",
        )
        assert linked.returncode == 0, linked.stdout + linked.stderr
        requested = str(link / "report.xml")
    value = "xml:" + requested if flag == "--cov-report" else requested
    value = "log_file=" + requested if flag == "--override-ini" else value

    def must_not_start(*args: object, **kwargs: object) -> None:
        raise AssertionError("An invalid output path must be rejected before process creation")

    monkeypatch.setattr(subprocess, "Popen", must_not_start)
    monkeypatch.setattr(sys, "argv", ["run_checks.py", check, flag, value])
    assert main() != 0
    result = json.loads(capsys.readouterr().out)
    assert "Output path" in result["commands"][0]["startup_error"]
    assert result["runtime_removed"] is True


def test_backend_command_records_a_process_start_failure_and_still_cleans(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.run_checks import main

    def cannot_start(*args: object, **kwargs: object) -> None:
        raise OSError("Synthetic OS process creation failure")

    monkeypatch.setattr(subprocess, "Popen", cannot_start)
    monkeypatch.setattr(sys, "argv", ["run_checks.py", "pytest"])

    assert main() != 0
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_removed"] is True
    assert not Path(result["runtime_directory"]).exists()
    assert result["commands"][0]["startup_error"] == "Synthetic OS process creation failure"


def test_interrupted_virtualenv_check_confirms_the_real_interpreter_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.run_checks import main

    observation = tmp_path / "actual-interpreter.json"
    probe = tmp_path / "test_interrupt_actual.py"
    probe.write_text(
        "import ctypes, json, os, time\nfrom ctypes import wintypes\nfrom pathlib import Path\n"
        "def test_interrupt_actual():\n"
        "    kernel = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "    kernel.GetCurrentProcess.restype = wintypes.HANDLE\n"
        "    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4\n"
        "    times = [wintypes.FILETIME() for _ in range(4)]\n"
        "    assert kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times))\n"
        "    destination = Path(os.environ['MYRK_INTERRUPT_OBSERVATION'])\n"
        "    pending = destination.with_suffix('.pending')\n"
        "    pending.write_text(json.dumps(\n"
        "        {'pid': os.getpid(), 'created_filetime': str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)}))\n"
        "    pending.replace(destination)\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    launch = subprocess.Popen
    unrelated = launch([sys._base_executable, "-B", "-c", "import time; time.sleep(60)"])
    launched: list[subprocess.Popen[bytes]] = []
    handles: list[int] = []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def interrupt_real_check(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = launch(*args, **kwargs)
        launched.append(process)
        original_wait = process.wait
        interrupted = False

        def wait(timeout: float | None = None) -> int:
            nonlocal interrupted
            if not interrupted and timeout is None:
                deadline = monotonic() + 10
                while not observation.exists() and monotonic() < deadline:
                    sleep(.01)
                assert observation.exists(), "The real pytest interpreter never reached its probe"
                actual = json.loads(observation.read_text(encoding="utf-8"))
                handle = kernel.OpenProcess(0x101001, False, actual["pid"])
                assert handle
                handles.append(int(handle))
                times = [wintypes.FILETIME() for _ in range(4)]
                assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
                assert str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) == actual["created_filetime"]
                assert actual["pid"] != process.pid, "This regression must exercise the Windows virtualenv launcher"
                interrupted = True
                raise KeyboardInterrupt()
            return original_wait(timeout=timeout)

        monkeypatch.setattr(process, "wait", wait)
        return process

    monkeypatch.setattr(subprocess, "Popen", interrupt_real_check)
    monkeypatch.setenv("MYRK_INTERRUPT_OBSERVATION", str(observation))
    monkeypatch.setattr(sys, "argv", ["run_checks.py", "pytest", str(probe), "-q"])
    try:
        assert main() == 130
        result = json.loads(capsys.readouterr().out)
        assert handles and kernel.WaitForSingleObject(handles[0], 500) == 0
        assert result["commands"][0]["job_active_processes"] == 0
        assert result["commands"][0]["interpreter_exit_confirmed"] is True
        assert result["runtime_removed"] is True
        assert not Path(result["runtime_directory"]).exists()
        assert unrelated.poll() is None
    finally:
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
        for process in [*launched, unrelated]:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_resource_command_verifies_removed_and_retained_paths_without_writing(tmp_path: Path) -> None:
    project = tmp_path / "isolated-project"
    (project / "scripts").mkdir(parents=True)
    (project / "myrk").mkdir()
    for relative in ("scripts/check_resources.py", "myrk/project_paths.py"):
        (project / relative).write_bytes((SOURCE_ROOT / relative).read_bytes())
    (project / "myrk/__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", "--template=", str(project)], check=True, capture_output=True)
    retained = project / "retained.txt"
    retained.write_text("needed evidence", encoding="utf-8")
    manifest = project / "resources.json"
    document = {
        "project_root": str(project),
        "paths": [
            {"path": str(project / "removed"), "status": "removed"},
            {"path": str(retained), "status": "retained", "owner": "current-task",
             "reason": "Original failure output needed for the current fix.",
             "delete_when": "The fix is reviewed and its result recorded."},
        ],
        "branches": [{"name": "task/nonexistent-cleanup-probe", "status": "removed"}],
    }
    manifest.write_text(json.dumps(document), encoding="utf-8")
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}
    process = subprocess.run(
        [sys.executable, "-B", str(project / "scripts/check_resources.py"), str(manifest)],
        cwd=project, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout)["ok"] is True
    assert {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()} == before


def test_resource_command_reports_runtime_cleanup_failure_receipts_not_in_the_manifest(tmp_path: Path) -> None:
    receipt_root = ROOT / ".scratch/runtime/cleanup-failures"
    assert not any(path.is_symlink() or path.is_junction() for path in (receipt_root, *receipt_root.parents))
    receipt_root.mkdir(parents=True, exist_ok=True)
    receipt = receipt_root / f"cleanup-command-probe-{uuid4().hex}.json"
    original = json.dumps({"runtime_directory": str(tmp_path / "runtime"), "error_type": "SyntheticCleanupFailure"})
    manifest = tmp_path / "resources.json"
    manifest.write_text(json.dumps({"project_root": str(ROOT), "paths": []}), encoding="utf-8")
    try:
        with receipt.open("x", encoding="utf-8") as destination:
            destination.write(original)
        process = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "scripts/check_resources.py"), str(manifest)],
            cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
        )

        assert process.returncode != 0
        result = json.loads(process.stdout)
        failure = next(item for item in result["cleanup_failures"] if item["path"] == str(receipt))
        assert "owning process has exited" in failure["delete_when"]
        assert str(receipt) in "\n".join(result["errors"])
        assert receipt.read_text(encoding="utf-8") == original
    finally:
        receipt.unlink(missing_ok=True)


def test_resource_command_finds_unlisted_exited_owners_but_preserves_active_tasks(tmp_path: Path) -> None:
    parent = ROOT / ".scratch/check-runs/runtime-tmp"
    parent.mkdir(parents=True, exist_ok=True)
    directories = [parent / ("b-audit-" + uuid4().hex) for _ in range(2)]
    child = subprocess.Popen([sys.executable, "-B", "-c", "pass"])
    child.wait(timeout=5)
    manifest = tmp_path / "empty-resources.json"
    manifest.write_text(json.dumps({"project_root": str(ROOT), "paths": []}), encoding="utf-8")
    before = {}
    try:
        for directory, pid in zip(directories, (child.pid, os.getpid())):
            directory.mkdir()
            marker = directory / "owner.json"
            marker.write_text(json.dumps({
                "owner": "resource-audit-test", "pid": pid, "project_root": str(ROOT),
                "runtime_directory": str(directory), "started_at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
            before[marker] = marker.read_bytes()
        simulated = directories[1] / "fixtures" / "simulated-runtime"
        simulated.mkdir(parents=True)
        simulated_marker = simulated / "owner.json"
        simulated_marker.write_text(json.dumps({
            "owner": "synthetic-fixture", "pid": child.pid, "project_root": str(ROOT),
            "runtime_directory": str(simulated), "started_at": datetime.now(timezone.utc).isoformat(),
        }), encoding="utf-8")
        before[simulated_marker] = simulated_marker.read_bytes()
        process = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "scripts/check_resources.py"), str(manifest)],
            cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
        )

        assert process.returncode != 0
        result = json.loads(process.stdout)
        discovered = {item["path"]: item for item in result["discovered_resources"]}
        assert discovered[str(directories[0])]["state"] == "exited"
        assert discovered[str(directories[1])]["state"] == "active"
        assert str(simulated) not in discovered
        assert str(directories[0]) in "\n".join(result["errors"])
        assert str(directories[1]) not in "\n".join(result["errors"])
        assert {marker: marker.read_bytes() for marker in before} == before
    finally:
        simulated_marker = directories[1] / "fixtures" / "simulated-runtime" / "owner.json"
        if simulated_marker.exists():
            simulated_marker.unlink()
            simulated_marker.parent.rmdir()
            simulated_marker.parent.parent.rmdir()
        for directory in directories:
            (directory / "owner.json").unlink(missing_ok=True)
            directory.rmdir()


@pytest.mark.parametrize("invalid", ["leftover", "missing-reason", "outside", "widened-root", "junction"])
def test_resource_command_rejects_unfinished_or_unsafe_cleanup(tmp_path: Path, invalid: str) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    resource = {"path": str(ordinary), "status": "retained", "owner": "current-task",
                "reason": "Failure evidence pending review.", "delete_when": "Review is complete."}
    document = {"project_root": str(ROOT), "paths": [resource]}
    if invalid == "leftover":
        resource["status"] = "removed"
    elif invalid == "missing-reason":
        resource.pop("reason")
    elif invalid == "outside":
        resource["path"] = str(ROOT.parent / "external-check-resource")
        resource["status"] = "removed"
    elif invalid == "widened-root":
        document["project_root"] = str(ROOT.parent)
    else:
        link = tmp_path / "linked"
        subprocess.run(
            ["C:/Program Files/PowerShell/7/pwsh.exe", "-NoProfile", "-Command",
             "New-Item -ItemType Junction -Path $env:MYRK_LINK_SOURCE -Target $env:MYRK_LINK_TARGET | Out-Null"],
            env=dict(os.environ, MYRK_LINK_SOURCE=str(link), MYRK_LINK_TARGET=str(ordinary)),
            check=True, capture_output=True,
        )
        resource["path"] = str(link / "missing-child")
        resource["status"] = "removed"
    manifest = tmp_path / "resources.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(SOURCE_ROOT / "scripts/check_resources.py"), str(manifest)],
        cwd=SOURCE_ROOT, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode != 0
    assert json.loads(process.stdout)["errors"]
    assert ordinary.is_dir()


@pytest.mark.parametrize("explicit_base", [False, True])
def test_direct_pytest_owns_its_directory_and_closes_the_loaded_runtime(tmp_path: Path, explicit_base: bool) -> None:
    probe = SOURCE_ROOT / ".scratch" / ("test_direct_" + uuid4().hex + ".py")
    probe.parent.mkdir(exist_ok=True)
    observation = tmp_path / "direct.json"
    probe.write_text(
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "from myrk.supervisor_runtime import SUPERVISOR_RUNTIME\n"
        "def test_direct(tmp_path, request, cache):\n"
        "    assert SUPERVISOR_RUNTIME.error is None\n"
        "    run = Path(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n"
        "    assert Path(tempfile.gettempdir()).is_relative_to(run)\n"
        "    cache_root = Path(request.config.getini('cache_dir'))\n"
        "    assert cache_root.is_relative_to(run)\n"
        "    cache.set('resource_probe/value', 'checked')\n"
        "    assert (cache_root / 'v/resource_probe/value').is_file()\n"
        "    Path(os.environ['MYRK_CHECK_PROBE']).write_text(json.dumps(\n"
        "        {'run': str(run), 'runtime': str(SUPERVISOR_RUNTIME.directory), 'fixtures': str(tmp_path)}))\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-B", "-m", "pytest", str(probe), "-q"]
    if explicit_base:
        command += ["--basetemp", str(tmp_path / "explicit-fixtures")]
    release = json.loads((SOURCE_ROOT / "myrk/executor-release.json").read_text(encoding="utf-8"))
    binary = ROOT / "myrk/runtime" / release["release"] / release["executable"]
    try:
        process = subprocess.run(
            command, cwd=SOURCE_ROOT,
            env=dict(os.environ, PYTHONPATH=str(SOURCE_ROOT), MYRK_CHECK_PROBE=str(observation), MYRK_EXECUTOR_PATH=str(binary)),
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        probe.unlink(missing_ok=True)

    assert process.returncode == 0, process.stdout + process.stderr
    observed = json.loads(observation.read_text(encoding="utf-8"))
    assert not Path(observed["run"]).exists()
    assert not Path(observed["runtime"]).exists()
    assert not Path(observed["fixtures"]).exists()
    summary = ROOT / ".scratch/check-runs/results" / (Path(observed["run"]).name + ".json")
    assert json.loads(summary.read_text(encoding="utf-8"))["runtime_removed"] is True


@pytest.mark.parametrize("location", ["outside-base", "outside-cache", "existing-base"])
def test_direct_pytest_rejects_unowned_output_locations(tmp_path: Path, location: str) -> None:
    probe = SOURCE_ROOT / ".scratch" / ("test_never_" + uuid4().hex + ".py")
    probe.parent.mkdir(exist_ok=True)
    probe.write_text("def test_never():\n    assert False, 'must not execute'\n", encoding="utf-8")
    command = [sys.executable, "-B", "-m", "pytest", str(probe), "-q"]
    important = tmp_path / "important.txt"
    important.write_text("preserve", encoding="utf-8")
    if location == "outside-base":
        command += ["--basetemp", str(ROOT.parent / "unsafe-pytest-base")]
    elif location == "outside-cache":
        command += ["-o", f"cache_dir={ROOT.parent / 'unsafe-pytest-cache'}"]
    else:
        command += ["--basetemp", str(tmp_path)]
    try:
        process = subprocess.run(
            command, cwd=SOURCE_ROOT, env=dict(os.environ, PYTHONPATH=str(SOURCE_ROOT)),
            capture_output=True, text=True, encoding="utf-8",
        )
    finally:
        probe.unlink(missing_ok=True)

    assert process.returncode != 0
    assert "must not execute" not in process.stdout
    expected = "An explicit pytest base must be new or empty" if location == "existing-base" else "Test output must be inside the project's .scratch"
    assert expected in process.stderr
    assert important.read_text(encoding="utf-8") == "preserve"
