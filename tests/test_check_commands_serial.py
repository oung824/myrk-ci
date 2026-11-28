from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


SOURCE_ROOT = Path(__file__).resolve().parents[1]
from myrk.project_paths import project_root

ROOT = project_root(SOURCE_ROOT)


def _patch_check_popen(monkeypatch: pytest.MonkeyPatch, popen: Callable[..., object]) -> None:
    from scripts import run_checks

    check_subprocess = SimpleNamespace(**{**vars(subprocess), "Popen": popen})
    monkeypatch.setattr(run_checks, "subprocess", check_subprocess)


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

    _patch_check_popen(monkeypatch, lambda *args, **kwargs: FinishedProcess())
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

    _patch_check_popen(monkeypatch, must_not_start)
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

    _patch_check_popen(monkeypatch, must_not_start)
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

    _patch_check_popen(monkeypatch, cannot_start)
    monkeypatch.setattr(sys, "argv", ["run_checks.py", "pytest"])

    assert main() != 0
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_removed"] is True
    assert not Path(result["runtime_directory"]).exists()
    assert result["commands"][0]["startup_error"] == "Synthetic OS process creation failure"
