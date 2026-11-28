"""Direct pytest commands isolate tests collected outside the tests directory."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from collections.abc import Iterator
from uuid import uuid4

import pytest

from myrk.project_paths import project_root


SOURCE = Path(__file__).resolve().parents[1]
PROJECT = project_root(SOURCE)


def remove_probe_receipt(result: Path, summary: dict) -> None:
    launch = result.parent.parent / "launches" / f"{summary['invocation_id']}.json"
    assert json.loads(launch.read_text(encoding="utf-8"))["result_path"] == str(result)
    launch.unlink()
    result.unlink()


@pytest.fixture
def scratch_probe() -> Iterator[Path]:
    probe_directory = SOURCE / ".scratch" / f"bare-check-probe-{uuid4().hex}"
    probe_directory.mkdir(parents=True)
    try:
        yield probe_directory / "test_scratch.py"
    finally:
        shutil.rmtree(probe_directory)


def test_explicit_check_plugin_and_project_discovery_reuse_the_same_plugin(
    tmp_path: Path, scratch_probe: Path,
) -> None:
    observation = tmp_path / "plugin-observation.txt"
    scratch_probe.write_text(
        "import os\nfrom pathlib import Path\n"
        "def test_plugin(request):\n"
        "    assert request.config.pluginmanager.get_plugin('myrk.check_plugin') is not None\n"
        "    Path(os.environ['MYRK_CHECK_PROBE']).write_text(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n",
        encoding="utf-8",
    )
    environment = dict(os.environ, PYTHONPATH=str(SOURCE), MYRK_CHECK_PROBE=str(observation), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("MYRK_CHECK_RUN_DIRECTORY", None)
    process = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-p", "myrk.check_plugin", str(scratch_probe), "-q"],
        cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8",
    )

    assert process.returncode == 0, process.stdout + process.stderr
    run = Path(observation.read_text(encoding="utf-8"))
    assert not run.exists()
    result = PROJECT / ".scratch/check-runs/results" / f"{run.name}.json"
    summary = json.loads(result.read_text(encoding="utf-8"))
    assert summary["runtime_removed"] is True
    remove_probe_receipt(result, summary)


@pytest.mark.parametrize("explicit_base", [False, True])
def test_bare_pytest_isolates_a_scratch_test_without_loading_a_plugin(
    tmp_path: Path, scratch_probe: Path, explicit_base: bool,
) -> None:
    observation = tmp_path / "observation.json"
    configuration = tmp_path / "configured-cache.txt"
    temporary = tmp_path / "fake-system-temp"
    temporary.mkdir()
    (scratch_probe.parent / "conftest.py").write_text(
        "import os, pytest\nfrom pathlib import Path\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_configure(config):\n"
        "    Path(os.environ['MYRK_CHECK_CONFIGURATION']).write_text(config.getini('cache_dir'))\n",
        encoding="utf-8",
    )
    scratch_probe.write_text(
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "def test_scratch(tmp_path, request, cache):\n"
        "    directory = os.environ.get('MYRK_CHECK_RUN_DIRECTORY')\n"
        "    assert directory is not None, 'Bare pytest did not allocate an owned runtime'\n"
        "    run = Path(directory)\n"
        "    paths = [Path(tempfile.gettempdir()), Path(request.config.getini('cache_dir'))]\n"
        "    paths += [Path(os.environ[key]) for key in ('TEMP', 'TMP', 'TMPDIR', 'MYRK_RUNTIME_ROOT')]\n"
        "    assert all(path.is_relative_to(run) for path in paths)\n"
        "    assert cache.get('scratch_probe/value', None) is None\n"
        "    cache.set('scratch_probe/value', 'checked')\n"
        "    assert cache.mkdir('scratch_probe').is_relative_to(run / 'cache/pytest')\n"
        "    Path(os.environ['MYRK_CHECK_PROBE']).write_text(json.dumps(\n"
        "        {'run': str(run), 'fixtures': str(tmp_path), 'owner': json.loads((run / 'owner.json').read_text())}))\n",
        encoding="utf-8",
    )
    environment = dict(
        os.environ, TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary),
        MYRK_CHECK_PROBE=str(observation), PYTHONPATH=str(SOURCE), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        MYRK_CHECK_CONFIGURATION=str(configuration),
    )
    for name in ("MYRK_CHECK_RUN_DIRECTORY", "MYRK_RUNTIME_ROOT", "MYRK_TEST_RUN_PARENT"):
        environment.pop(name, None)
    command = [sys.executable, "-B", "-m", "pytest", "-q", "-o", f"cache_dir={tmp_path / 'initial-cache'}"]
    if explicit_base:
        command += ["--basetemp", str(tmp_path / "explicit-fixtures")]
    command += ["--", str(scratch_probe)]

    common_directory = subprocess.run(
        ["git", "-C", str(SOURCE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    project = Path(common_directory.stdout.strip()).parent
    runs = set()
    for _ in range(2):
        process = subprocess.run(command, cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8")
        assert process.returncode == 0, process.stdout + process.stderr
        observed = json.loads(observation.read_text(encoding="utf-8"))
        run = Path(observed["run"])
        fixtures = Path(observed["fixtures"])
        assert fixtures.is_relative_to(tmp_path / "explicit-fixtures" if explicit_base else run / "fixtures")
        assert observed["owner"]["project_root"] == str(project)
        assert run.parent == project / ".scratch/check-runs/runtime-tmp"
        assert not run.exists()
        assert not fixtures.exists()
        assert not list(temporary.iterdir())
        result = project / ".scratch/check-runs/results" / f"{run.name}.json"
        summary = json.loads(result.read_text(encoding="utf-8"))
        assert summary["runtime_removed"] is True
        remove_probe_receipt(result, summary)
        assert Path(configuration.read_text(encoding="utf-8")) == run / "cache/pytest"
        assert run not in runs
        runs.add(run)
    assert not (tmp_path / "initial-cache").exists()


@pytest.mark.parametrize("location", ["outside-base", "outside-cache", "relative-cache", "existing-base"])
def test_bare_scratch_checks_reject_unowned_output_before_collection(
    tmp_path: Path, scratch_probe: Path, location: str,
) -> None:
    scratch_probe.write_text("raise AssertionError('Unsafe check must not collect this file')\n", encoding="utf-8")
    important = tmp_path / "important.txt"
    important.write_text("preserve", encoding="utf-8")
    command = [sys.executable, "-B", "-m", "pytest", str(scratch_probe), "-q"]
    outside = PROJECT.parent / f"unsafe-pytest-{uuid4().hex}"
    if location == "outside-base":
        command += ["--basetemp", str(outside)]
    elif location == "outside-cache":
        command += ["-o", f"cache_dir={outside}"]
    elif location == "relative-cache":
        relative_cache = Path(".scratch") / outside.name
        outside = PROJECT.parent / relative_cache
        command += ["--rootdir", str(PROJECT.parent), "-o", f"cache_dir={relative_cache}"]
    else:
        command += ["--basetemp", str(tmp_path)]
    environment = dict(os.environ, PYTHONPATH=str(SOURCE), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    process = subprocess.run(command, cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8")

    assert process.returncode == 4, process.stdout + process.stderr
    assert "Unsafe check must not collect" not in process.stdout + process.stderr
    expected = "An explicit pytest base must be new or empty" if location == "existing-base" else "Test output must be inside the project's .scratch"
    assert expected in process.stderr
    assert not outside.exists()
    assert important.read_text(encoding="utf-8") == "preserve"


@pytest.mark.parametrize("location", [
    "outside", "relative-cache", "configured-outside", "configured-allowed", "configured-default",
    "configured-explicit-default", "allowed",
])
def test_wrapped_pytest_validates_and_isolates_requested_cache(
    tmp_path: Path, scratch_probe: Path, location: str,
) -> None:
    collected = tmp_path / "wrapped-probe-collected"
    scratch_probe.write_text(
        "import os\nfrom pathlib import Path\n"
        f"Path({str(collected)!r}).touch()\n"
        "def test_probe(cache):\n"
        "    run = Path(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n"
        "    assert cache.mkdir('wrapped_probe').is_relative_to(run / 'cache/pytest')\n"
        "    assert cache.get('wrapped_probe/value', None) is None\n"
        "    cache.set('wrapped_probe/value', 'checked')\n",
        encoding="utf-8",
    )
    outside = location not in {"allowed", "configured-allowed", "configured-default"}
    requested = PROJECT.parent / f"unsafe-wrapped-pytest-{uuid4().hex}" if outside else tmp_path / "initial-cache"
    command = [sys.executable, "-B", "scripts/run_checks.py", "pytest", str(scratch_probe), "-q"]
    if location == "relative-cache":
        relative_cache = Path(".scratch") / requested.name
        requested = PROJECT.parent / relative_cache
        command += ["--rootdir", str(PROJECT.parent), "--override-ini", f"cache_dir={relative_cache}"]
    elif location.startswith("configured-"):
        configuration = tmp_path / "pytest.ini"
        cache_setting = f"cache_dir = {requested.as_posix()}\n"
        if location in {"configured-default", "configured-explicit-default"}:
            command += ["--rootdir", str(PROJECT)]
            requested = PROJECT / ".pytest_cache"
            cache_setting = "cache_dir = .pytest_cache\n" if outside else ""
        configuration.write_text(f"[pytest]\n{cache_setting}", encoding="utf-8")
        command += ["-c", str(configuration)]
        if outside:
            command += ["--debug"]
    else:
        command += ["--override-ini", f"cache_dir={requested}"]
    environment = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    requested_existed = requested.exists()
    runs = set()
    for _ in range(1 if outside else 2):
        process = subprocess.run(command, cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8")
        assert process.stdout.strip(), process.stderr
        summary = json.loads(process.stdout)
        run = Path(summary["runtime_directory"])
        assert summary["runtime_removed"] is True
        assert not run.exists()
        assert run not in runs
        runs.add(run)
        result = Path(summary["result_path"])
        output = summary["commands"][0]["output_tail"]
        for name in summary["failure_logs"]:
            log = Path(name)
            assert log.parent == result.parent and log.name.startswith(result.stem + ".")
            output += log.read_text(encoding="utf-8")
            log.unlink()
        remove_probe_receipt(result, summary)

        assert process.returncode == (4 if outside else 0), output + process.stderr
        if outside:
            assert "Test output must be inside the project's .scratch" in output
            assert "retained_reports" not in summary
            assert "writing pytest debug information" not in output
        else:
            assert summary["pytest_coverage"]["passed_calls"] == 1
        assert collected.exists() is not outside
        assert requested.exists() is requested_existed


@pytest.mark.parametrize("explicit", [False, True])
def test_bare_debug_is_rejected_before_pytest_opens_its_log(
    tmp_path: Path, scratch_probe: Path, explicit: bool,
) -> None:
    scratch_probe.write_text("raise AssertionError('Debug rejection must happen before collection')\n", encoding="utf-8")
    requested = PROJECT.parent / f"unsafe-pytest-debug-{uuid4().hex}.log" if explicit else SOURCE / "pytestdebug.log"
    bootstrap = tmp_path / "debug-bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import os, sys\n"
        "blocked = os.environ['MYRK_CHECK_BLOCKED_OUTPUT'].casefold()\n"
        "def prevent_output(event, arguments):\n"
        "    if event == 'open' and isinstance(arguments[0], (str, bytes, os.PathLike)):\n"
        "        if os.path.abspath(os.fsdecode(arguments[0])).casefold() == blocked:\n"
        "            raise RuntimeError('Test guard prevented a premature debug write')\n"
        "sys.addaudithook(prevent_output)\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-B", "-m", "pytest", str(scratch_probe), "-q", f"--debug={requested}" if explicit else "--debug"]
    environment = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(bootstrap), str(SOURCE)]),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", MYRK_CHECK_BLOCKED_OUTPUT=str(requested),
    )
    environment.pop("MYRK_CHECK_RUN_DIRECTORY", None)
    process = subprocess.run(command, cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8")

    assert process.returncode == 4, process.stdout + process.stderr
    assert "--debug requires scripts/run_checks.py pytest" in process.stderr
    assert "Test guard prevented" not in process.stdout + process.stderr
    assert "Debug rejection must happen" not in process.stdout + process.stderr


@pytest.mark.parametrize("report", ["junit", "log-option", "log-ini"])
def test_bare_pytest_preserves_requested_reports_after_removing_runtime(
    tmp_path: Path, scratch_probe: Path, report: str,
) -> None:
    observation = tmp_path / "report-observation.json"
    requested = tmp_path / ("requested.xml" if report == "junit" else "requested.log")
    scratch_probe.write_text(
        "import logging, os\nfrom pathlib import Path\n"
        "def test_requested_report():\n"
        "    logging.warning('requested check report evidence')\n"
        "    Path(os.environ['MYRK_CHECK_PROBE']).write_text(os.environ['MYRK_CHECK_RUN_DIRECTORY'])\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-B", "-m", "pytest", str(scratch_probe), "-q"]
    command += ["--junitxml", str(requested)] if report == "junit" else (
        ["--log-file", str(requested)] if report == "log-option" else ["-o", f"log_file={requested}"]
    )
    environment = dict(os.environ, PYTHONPATH=str(SOURCE), MYRK_CHECK_PROBE=str(observation), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("MYRK_CHECK_RUN_DIRECTORY", None)
    process = subprocess.run(command, cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8")

    assert process.returncode == 0, process.stdout + process.stderr
    run = Path(observation.read_text(encoding="utf-8"))
    result = PROJECT / ".scratch/check-runs/results" / f"{run.name}.json"
    summary = json.loads(result.read_text(encoding="utf-8"))
    assert summary["runtime_removed"] is True
    retained = summary["retained_reports"]
    assert retained["owner"] and retained["reason"] and retained["delete_when"]
    reports = Path(retained["paths"][0])
    assert reports.parent == result.parent
    assert retained["bytes"] > 0
    assert not requested.exists()
    assert not run.exists()
    files = [path for path in reports.rglob("*") if path.is_file()]
    launch = result.parent.parent / "launches" / f"{summary['invocation_id']}.json"
    original = json.loads(launch.read_text(encoding="utf-8"))["terminal"]["required_artifacts"]
    assert original == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    content = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert ("test_requested_report" if report == "junit" else "requested check report evidence") in content
    shutil.rmtree(reports)
    remove_probe_receipt(result, summary)


@pytest.mark.parametrize("report", ["junit", "log-option", "log-ini"])
def test_bare_pytest_rejects_external_reports_before_any_output_write(
    tmp_path: Path, scratch_probe: Path, report: str,
) -> None:
    scratch_probe.write_text("raise AssertionError('Unsafe report must not collect this file')\n", encoding="utf-8")
    outside = PROJECT.parent / f"unsafe-pytest-report-{uuid4().hex}"
    requested = outside / "report.txt"
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import os, sys\n"
        "blocked = os.environ['MYRK_CHECK_BLOCKED_OUTPUT'].casefold()\n"
        "def prevent_output(event, arguments):\n"
        "    if event in ('os.mkdir', 'open') and isinstance(arguments[0], (str, bytes, os.PathLike)):\n"
        "        path = os.path.abspath(os.fsdecode(arguments[0])).casefold()\n"
        "        if path == blocked or path.startswith(blocked + os.sep):\n"
        "            raise RuntimeError('Test guard prevented an external output write')\n"
        "sys.addaudithook(prevent_output)\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-B", "-m", "pytest", str(scratch_probe), "-q"]
    command += ["--junitxml", str(requested)] if report == "junit" else (
        ["--log-file", str(requested)] if report == "log-option" else ["-o", f"log_file={requested}"]
    )
    environment = dict(
        os.environ, PYTHONPATH=os.pathsep.join([str(bootstrap), str(SOURCE)]),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", MYRK_CHECK_BLOCKED_OUTPUT=str(outside),
    )
    environment.pop("MYRK_CHECK_RUN_DIRECTORY", None)
    process = subprocess.run(command, cwd=SOURCE, env=environment, capture_output=True, text=True, encoding="utf-8")

    assert process.returncode == 4, process.stdout + process.stderr
    assert "Output path must be inside the project" in process.stderr
    assert "Test guard prevented" not in process.stdout + process.stderr
    assert "Unsafe report must not collect" not in process.stdout + process.stderr
    assert not outside.exists()
