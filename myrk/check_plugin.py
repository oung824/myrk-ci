"""Project-owned pytest resources for direct and wrapper check commands."""

import os
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import cast
import warnings

import pytest

from myrk.project_paths import project_root
from myrk.check_outputs import report_path, retain_reports, validate_pytest_arguments

os.environ["QT_QPA_PLATFORM"] = "offscreen"

BARE_RUN = pytest.StashKey[Path]()
BARE_EXIT = pytest.StashKey[int]()
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT = project_root(SOURCE_ROOT)


def project_test_directory(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT / path
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise pytest.UsageError("Project test directories must not use links.")
    path = path.resolve()
    if not path.is_relative_to(PROJECT / ".scratch") or path == PROJECT / ".scratch":
        raise pytest.UsageError("Test output must be inside the project's .scratch directory; use scripts/run_checks.py.")
    return path


def managed_check_run(base: str | Path | None) -> Path | None:
    """Match a wrapper invocation across the Windows virtualenv launcher."""
    managed = os.environ.get("MYRK_CHECK_RUN_DIRECTORY")
    invocation = os.environ.get("MYRK_CHECK_RUN_ID")
    if managed is None or not invocation or base is None:
        return None
    run = project_test_directory(managed)
    if project_test_directory(base) != run / "fixtures":
        return None
    marker = project_test_directory(run / "owner.json")
    if not marker.is_file():
        return None
    owner = json.loads(marker.read_text(encoding="utf-8"))
    if (isinstance(owner, dict) and owner.get("invocation_id") == invocation
            and owner.get("owner") == f"backend-check:{run.name}"
            and owner.get("project_root") == str(PROJECT)
            and owner.get("source_root") == str(SOURCE_ROOT)
            and owner.get("runtime_directory") == str(run)):
        return run
    return None


def check_early_debug(config: pytest.Config) -> None:
    arguments = getattr(config, "known_args_namespace", None)
    if arguments is None:
        return
    debug = getattr(arguments, "debug", None)
    if not debug:
        return
    run = managed_check_run(arguments.basetemp)
    if run is not None:
        target = Path(debug)
        if not target.is_absolute():
            target = Path(config.invocation_params.dir) / target
        if project_test_directory(target).is_relative_to(run / "reports"):
            return
    raise pytest.UsageError("--debug requires scripts/run_checks.py pytest so its log is owned and retained by the check command.")


def pytest_addoption(pluginmanager: pytest.PytestPluginManager) -> None:
    check_early_debug(cast(pytest.Config, pluginmanager.get_plugin("pytestconfig")))


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config, args: list[str]) -> None:
    try:
        validate_pytest_arguments(args)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    check_early_debug(early_config)


def configure_report_paths(config: pytest.Config, run: Path) -> None:
    source = Path(config.invocation_params.dir)

    def output(value: str, kind: str) -> str:
        path = report_path(value, kind=kind, run=run, project=PROJECT, source=source)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    try:
        for option, kind in (("xmlpath", "junit"), ("htmlpath", "html")):
            value = getattr(config.option, option, None)
            if value:
                setattr(config.option, option, output(value, kind))
        log_file = config.option.log_file or config.getini("log_file")
        if log_file and log_file != os.devnull:
            config.option.log_file = output(log_file, "log")
        coverage = getattr(config.option, "cov_report", {})
        defaults = {"html": "htmlcov", "xml": "coverage.xml", "json": "coverage.json", "lcov": "coverage.lcov", "annotate": "coverage-annotate"}
        for kind, value in coverage.items():
            if kind in defaults:
                coverage[kind] = output(value or defaults[kind], f"coverage-{kind}")
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    base = project_test_directory(config.option.basetemp) if config.option.basetemp is not None else None
    project_test_directory(str(config.getini("cache_dir")))
    project_test_directory(PROJECT / ".scratch/check-runs/results")
    if base is not None and base.exists() and (not base.is_dir() or any(base.iterdir())):
        raise pytest.UsageError("An explicit pytest base must be new or empty; existing files will be preserved.")
    managed = managed_check_run(base)
    if managed is not None:
        configure_report_paths(config, managed)
        return
    parent = project_test_directory(PROJECT / ".scratch/check-runs/runtime-tmp")
    parent.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="p-", dir=parent))
    fixtures = base if base is not None else run / "fixtures"
    config.option.basetemp = str(fixtures)
    config._inicache["cache_dir"] = str(run / "cache/pytest")
    temporary = run / "tmp"
    temporary.mkdir()
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(temporary)
    tempfile.tempdir = str(temporary)
    os.environ["MYRK_RUNTIME_ROOT"] = str(run / "runtime")
    os.environ["MYRK_CHECK_RUN_DIRECTORY"] = str(run)
    os.environ["MYPY_CACHE_DIR"] = str(run / "cache/mypy")
    os.environ["COVERAGE_FILE"] = str(run / "cache/coverage")
    os.environ["LOCALAPPDATA"] = str(run / "profile/local")
    os.environ["APPDATA"] = str(run / "profile/roaming")
    os.environ["XDG_CACHE_HOME"] = str(run / "cache/xdg")
    os.environ["XDG_CONFIG_HOME"] = str(run / "profile/config")
    os.environ["npm_config_cache"] = str(run / "cache/npm")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    owner = {
        "owner": f"bare-pytest:{run.name}", "pid": os.getpid(), "project_root": str(PROJECT),
        "source_root": str(SOURCE_ROOT),
        "runtime_directory": str(run), "started_at": datetime.now(timezone.utc).isoformat(),
        "fixture_directory": str(fixtures),
        "reason": "Runtime directory of this directly invoked pytest command.",
        "delete_when": "Pytest has exited and the result and failure details are recorded.",
    }
    (run / "owner.json").write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
    config.stash[BARE_RUN] = run
    configure_report_paths(config, run)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    run = config.stash.get(BARE_RUN, None)
    if run is None:
        return
    results = project_test_directory(PROJECT / ".scratch/check-runs/results")
    results.mkdir(parents=True, exist_ok=True)
    summary = json.loads((run / "owner.json").read_text(encoding="utf-8"))
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["exit_code"] = int(config.stash.get(BARE_EXIT, 1))
    summary["runtime_removed"] = False
    failure = results / f"{run.name}.failure.txt"
    if failure.exists():
        summary["retained"] = {
            "owner": summary["owner"], "paths": [str(failure)],
            "reason": "Original details of a failed pytest command.",
            "delete_when": "The failure is resolved or necessary evidence is recorded with its task.",
        }
    try:
        module = sys.modules.get("myrk.supervisor_runtime")
        runtime = getattr(module, "SUPERVISOR_RUNTIME", None)
        if runtime is not None:
            runtime.close()
        retained_reports = retain_reports(run, results / f"{run.name}.json", owner=summary["owner"])
        if retained_reports is not None:
            summary["retained_reports"] = retained_reports
            print(f"Myrk requested reports: {retained_reports['paths'][0]}")
        fixtures = project_test_directory(summary["fixture_directory"])
        project_test_directory(run)
        if fixtures != run / "fixtures" and fixtures.exists():
            shutil.rmtree(fixtures)
        if run.exists():
            shutil.rmtree(run)
        summary["runtime_removed"] = True
    except (OSError, ValueError, pytest.UsageError) as error:
        summary["cleanup_error"] = str(error)
    if not summary["runtime_removed"]:
        summary["retained_runtime"] = {
            "owner": summary["owner"], "path": str(run), "reason": summary["cleanup_error"],
            "delete_when": "The path is ordinary and no longer locked after the recorded process exits.",
        }
        warnings.warn(pytest.PytestWarning(f"Test runtime retained at {run}: {summary['cleanup_error']}"), stacklevel=1)
    (results / f"{run.name}.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if BARE_RUN not in session.config.stash:
        return
    session.config.stash[BARE_EXIT] = exitstatus
    if exitstatus:
        run = session.config.stash[BARE_RUN]
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            failures = [str(report.longrepr) for outcome in ("failed", "error") for report in reporter.stats.get(outcome, [])]
            results = project_test_directory(PROJECT / ".scratch/check-runs/results")
            results.mkdir(parents=True, exist_ok=True)
            (results / f"{run.name}.failure.txt").write_text("\n\n".join(failures), encoding="utf-8")
