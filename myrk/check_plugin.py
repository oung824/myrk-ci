"""Project-owned pytest resources for direct and wrapper check commands."""

from collections.abc import Iterator
import os
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, cast
from uuid import uuid4
import warnings

import pytest

from myrk.project_paths import project_root
from myrk.check_outputs import (
    CheckLaunch, configuration_input, publish_json, report_path, retain_reports, source_version,
    successful_pytest_status, validate_pytest_arguments,
)

os.environ["QT_QPA_PLATFORM"] = "offscreen"

BARE_RUN = pytest.StashKey[Path]()
BARE_RESULT = pytest.StashKey[dict[str, object]]()
BARE_LAUNCH = pytest.StashKey[CheckLaunch]()
CACHE_CONFIGURATION = pytest.StashKey[tuple[Path, tuple[str, ...]]]()
MANAGED_RUN = pytest.StashKey[Path]()
PYTEST_COVERAGE_PATH = pytest.StashKey[Path]()
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT = project_root(SOURCE_ROOT)


def selected_file_digest(path: Path) -> str | None:
    try:
        return configuration_input(path, PROJECT)["sha256"]
    except (OSError, ValueError):
        return None


class PytestObservation:
    def __init__(self) -> None:
        self.files: dict[Path, str | None] = {}
        self.passed: set[str] = set()
        self.selected: dict[Path, str | None] = {}
        self.nodeids: list[str] = []
        self.coverage: dict[str, Any] | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_collectstart(self, collector: pytest.Collector) -> None:
        if isinstance(collector, pytest.File) and collector.path not in self.files:
            self.files[collector.path] = selected_file_digest(collector.path)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" and report.passed and not getattr(report, "wasxfail", False):
            self.passed.add(report.nodeid)


OBSERVATION = pytest.StashKey[PytestObservation]()


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


def validate_cache_directory(config: pytest.Config) -> None:
    directories = {str(config.getini("cache_dir"))}
    directories.update(entry.removeprefix("cache_dir=") for entry in config.getoption("override_ini") or ()
                       if entry.startswith("cache_dir="))
    for directory in directories:
        project_test_directory(config.rootpath / os.path.expandvars(os.path.expanduser(directory)))


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


def check_early_debug(config: pytest.Config) -> bool:
    arguments = getattr(config, "known_args_namespace", None)
    if arguments is None:
        return False
    debug = getattr(arguments, "debug", None)
    if not debug:
        return False
    run = managed_check_run(arguments.basetemp)
    if run is not None:
        target = Path(debug)
        if not target.is_absolute():
            target = Path(config.invocation_params.dir) / target
        if project_test_directory(target).is_relative_to(run / "reports"):
            return True
    raise pytest.UsageError("--debug requires scripts/run_checks.py pytest so its log is owned and retained by the check command.")


def pytest_addoption(parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager) -> None:
    parser.addini("cache_dir", "Cache directory path", default=str(PROJECT / ".scratch/check-runs/cache/pytest"))
    check_early_debug(cast(pytest.Config, pluginmanager.get_plugin("pytestconfig")))


def append_pytest_options(arguments: list[str], *options: str) -> None:
    end = arguments.index("--") if "--" in arguments else len(arguments)
    arguments[end:end] = options


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config, args: list[str]) -> Iterator[None]:
    """Validate paths early and defer writing a managed debug log to the effective invocation."""
    try:
        validate_pytest_arguments(args)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    owned_debug = check_early_debug(early_config)
    yield
    if owned_debug and CACHE_CONFIGURATION not in early_config.stash:
        append_pytest_options(args, "--debug=")


class CacheConfiguration:
    def __init__(self, run: Path, arguments: tuple[str, ...], *, managed: bool) -> None:
        self.run = run
        self.arguments = arguments
        self.managed = managed

    def pytest_load_initial_conftests(self, early_config: pytest.Config) -> None:
        early_config.stash[CACHE_CONFIGURATION] = (self.run, self.arguments)
        if self.managed:
            early_config.stash[MANAGED_RUN] = self.run


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> int | None:
    """Supply the owned cache directory before plugins configure the test session."""
    if CACHE_CONFIGURATION in config.stash:
        run, _ = config.stash[CACHE_CONFIGURATION]
        if Path(config.getini("cache_dir")) != run / "cache/pytest":
            raise pytest.UsageError("Pytest did not apply the owned cache directory.")
        return None

    validate_cache_directory(config)
    managed = managed_check_run(config.option.basetemp)
    run = managed or project_test_directory(PROJECT / ".scratch/check-runs/runtime-tmp" / f"p-{uuid4().hex[:8]}")
    params = config.invocation_params
    arguments = list(params.args)
    append_pytest_options(arguments, "-o", f"cache_dir={run / 'cache/pytest'}")
    cache_configuration = CacheConfiguration(run, params.args, managed=managed is not None)
    return pytest.main(arguments, plugins=[*(params.plugins or ()), cache_configuration])


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
    observation = PytestObservation()
    config.stash[OBSERVATION] = observation
    config.pluginmanager.register(observation)
    base = project_test_directory(config.option.basetemp) if config.option.basetemp is not None else None
    results = project_test_directory(PROJECT / ".scratch/check-runs/results")
    if base is not None and base.exists() and (not base.is_dir() or any(base.iterdir())):
        raise pytest.UsageError("An explicit pytest base must be new or empty; existing files will be preserved.")
    managed = config.stash.get(MANAGED_RUN, None)
    if managed is not None:
        config.stash[PYTEST_COVERAGE_PATH] = managed / "pytest-coverage.json"
        configure_report_paths(config, managed)
        return
    try:
        pytest_config = configuration_input(config.inipath, PROJECT) if config.inipath is not None else None
    except (OSError, ValueError) as error:
        raise pytest.UsageError(str(error)) from error
    run, arguments = config.stash[CACHE_CONFIGURATION]
    run.mkdir(parents=True)
    invocation_id = uuid4().hex
    launch = CheckLaunch(PROJECT, SOURCE_ROOT, results / f"{run.name}.json", invocation_id, "bare-pytest",
                         list(arguments), version={**source_version(SOURCE_ROOT), "pytest_config": pytest_config})
    fixtures = base if base is not None else run / "fixtures"
    config.option.basetemp = str(fixtures)
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
        "source_root": str(SOURCE_ROOT), "invocation_id": invocation_id,
        "runtime_directory": str(run), "started_at": datetime.now(timezone.utc).isoformat(),
        "fixture_directory": str(fixtures),
        "reason": "Runtime directory of this directly invoked pytest command.",
        "delete_when": "Pytest has exited and the result and failure details are recorded.",
    }
    if task_id := os.environ.get("MYRK_TASK_ID"):
        owner["task_id"] = task_id
    (run / "owner.json").write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
    config.stash[BARE_RUN] = run
    config.stash[BARE_LAUNCH] = launch
    configure_report_paths(config, run)


@pytest.hookimpl(trylast=True)
def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    observation = config.stash[OBSERVATION]
    observation.nodeids = sorted(item.nodeid for item in session.items)
    observation.selected = {item.path: observation.files.get(item.path) for item in session.items}
    observation.coverage = {
        "paths": config.args,
        "keyword": config.option.keyword,
        "markers": config.option.markexpr,
        "collect_only": config.option.collectonly,
        "setup_or_fixtures_only": any(getattr(config.option, option, False) for option in (
            "setuponly", "setupplan", "showfixtures", "show_fixtures_per_test",
        )),
        "node_count": len(observation.nodeids),
        "selection_sha256": hashlib.sha256(json.dumps(observation.nodeids).encode("utf-8")).hexdigest(),
        "selected_files": {path.relative_to(PROJECT).as_posix(): digest for path, digest in sorted(observation.selected.items())
                           if path.is_relative_to(PROJECT)},
    }
    record_coverage(config, observation.coverage)


def record_coverage(config: pytest.Config, coverage: dict[str, Any]) -> None:
    if BARE_LAUNCH in config.stash:
        config.stash[BARE_LAUNCH].coverage["pytest"] = coverage
    elif PYTEST_COVERAGE_PATH in config.stash:
        publish_json(config.stash[PYTEST_COVERAGE_PATH], coverage)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    run = config.stash.get(BARE_RUN, None)
    if run is None:
        return
    results = project_test_directory(PROJECT / ".scratch/check-runs/results")
    results.mkdir(parents=True, exist_ok=True)
    summary = json.loads((run / "owner.json").read_text(encoding="utf-8"))
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary.update(config.stash.get(BARE_RESULT, {"exit_code": 1}))
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
    launch = config.stash[BARE_LAUNCH]
    if BARE_RESULT not in config.stash:
        status = "startup_failed"
    elif summary["exit_code"] == pytest.ExitCode.INTERRUPTED:
        status = "interrupted"
    else:
        status = "failed" if summary["exit_code"] else successful_pytest_status(launch.coverage)
    launch.finish(summary, status)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    observation = session.config.stash[OBSERVATION]
    if observation.coverage is not None:
        observation.coverage.update(
            passed_calls=len(observation.passed.intersection(observation.nodeids)),
            selected_inputs_unchanged=(
                observation.nodeids == sorted(item.nodeid for item in session.items)
                and observation.selected.keys() == {item.path for item in session.items}
                and all(digest is not None and selected_file_digest(path) == digest
                        for path, digest in observation.selected.items())
            ),
        )
        record_coverage(session.config, observation.coverage)
    if BARE_RUN not in session.config.stash:
        return
    result: dict[str, object] = {"exit_code": int(exitstatus)}
    session.config.stash[BARE_LAUNCH].verify_pytest_config(result)
    session.config.stash[BARE_RESULT] = result
    if not exitstatus and result["exit_code"]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if exitstatus:
        run = session.config.stash[BARE_RUN]
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            failures = [str(report.longrepr) for outcome in ("failed", "error") for report in reporter.stats.get(outcome, [])]
            results = project_test_directory(PROJECT / ".scratch/check-runs/results")
            results.mkdir(parents=True, exist_ok=True)
            (results / f"{run.name}.failure.txt").write_text("\n\n".join(failures), encoding="utf-8")
