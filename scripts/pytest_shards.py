"""Collect and verify disjoint pytest lanes inside managed check runs."""

from collections.abc import Iterator
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
from typing import NoReturn, TypedDict, cast

import pytest


ShardMode = str
FORMAL_NODE_PREFIX = (
    "tests/acceptance/test_formal_scenarios.py::"
    "test_selected_formal_scenario_traverses_real_main_service_and_sdk["
)
FORMAL_PARAMETER_FUNCTIONS = {
    "tests/acceptance/test_formal_scenarios.py::"
    "test_selected_formal_scenario_traverses_real_main_service_and_sdk",
}
SOURCE_SUFFIXES = {".py", ".json", ".mjs", ".ts", ".mts", ".tsx", ".ps1", ".css", ".html"}
IGNORED_DIRECTORIES = {
    ".git", ".scratch", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "dist", "build",
    "coverage", "htmlcov", ".vite", ".electron-vite",
}


ShardLanes = dict[str, list[str]]


class ShardManifest(TypedDict):
    version: int
    source_root: str
    nodeids: list[str]
    lanes: ShardLanes
    source_inputs: dict[str, str]
    timing_update_eligible: bool


def _plain_path(path: Path) -> Path:
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise ValueError(f"Pytest shard inputs and reports must not use links: {path}")
    return path.resolve()


def get_source_inputs(source: Path) -> dict[str, str]:
    """Fingerprint existing source and check configuration without reading credentials."""
    source = _plain_path(source)
    inputs: list[Path] = []
    directories = [source / name for name in ("myrk", "tests", "scripts", "desktop/src", "desktop/tests")]
    while directories:
        directory = directories.pop()
        _plain_path(directory)
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError(f"Pytest shard source directory is not a directory: {directory}")
        for path in directory.iterdir():
            if path.name.lower() in IGNORED_DIRECTORIES:
                continue
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"Pytest shard source inputs must not use links: {path}")
            if path.is_dir():
                directories.append(path)
            elif path.suffix.lower() in SOURCE_SUFFIXES:
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"Pytest shard source input is not a regular file: {path}")
                inputs.append(path)

    for name in ("tests/acceptance/materials", "tests/acceptance/plans"):
        directory = _plain_path(source / name)
        if directory.exists():
            for path in directory.rglob("*"):
                _plain_path(path)
                if path.is_file() and path not in inputs:
                    inputs.append(path)

    root_names = {
        "conftest.py", "pyproject.toml", "pytest.ini", ".pytest.ini", "setup.cfg",
        "tox.ini", "mypy.ini", ".mypy.ini",
    }
    for path in source.iterdir():
        if path.name in root_names or (path.name.startswith("requirements") and path.suffix in {".txt", ".in", ".lock"}):
            _plain_path(path)
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError(f"Pytest shard source input is not a regular file: {path}")
            inputs.append(path)
    desktop = source / "desktop"
    _plain_path(desktop)
    if desktop.exists():
        desktop_names = {
            "package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
            "yarn.lock", "bun.lock", "bun.lockb",
        }
        for path in desktop.iterdir():
            if (path.name in desktop_names
                    or ("vite" in path.name and ".config." in path.name and path.suffix in {".ts", ".js", ".mjs", ".cjs"})
                    or (path.name.startswith("tsconfig") and path.suffix == ".json")):
                _plain_path(path)
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"Pytest shard source input is not a regular file: {path}")
                inputs.append(path)
    return {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(inputs)
    }


def _load_manifest(path: Path, project: Path, source: Path) -> ShardManifest:
    path = _plain_path(path)
    results = _plain_path(project / ".scratch/check-runs/results")
    if not path.is_relative_to(results) or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Pytest shard manifest must be a regular file inside the project's check results.")
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 2:
        raise ValueError("Pytest shard manifest must have version 2.")
    if value.get("source_root") != str(source):
        raise ValueError("Pytest shard manifest source_root does not match this source checkout.")
    nodeids = value.get("nodeids")
    lanes = value.get("lanes")
    source_inputs = value.get("source_inputs")
    if (not isinstance(nodeids, list) or not all(isinstance(nodeid, str) and nodeid for nodeid in nodeids)
            or len(nodeids) != len(set(nodeids))):
        raise ValueError("Pytest shard manifest nodeids must be a list of unique node IDs.")
    if (not isinstance(lanes, dict) or not lanes
            or any(not isinstance(name, str) or not name or name == "collect" for name in lanes)):
        raise ValueError("Pytest shard manifest lanes must have nonempty worker names.")
    for lane in lanes.values():
        if (not isinstance(lane, list) or not all(isinstance(nodeid, str) and nodeid for nodeid in lane)
                or len(lane) != len(set(lane))):
            raise ValueError("Each pytest shard lane must contain unique node IDs.")
    assigned = [node for lane in lanes.values() for node in lane]
    if len(assigned) != len(set(assigned)) or set(assigned) != set(nodeids):
        raise ValueError("Pytest shard lanes must be disjoint and cover every collected node ID.")
    if (not isinstance(source_inputs, dict)
            or not all(
                isinstance(name, str) and name and "\\" not in name
                and not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts
                and isinstance(digest, str) and len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
                for name, digest in source_inputs.items()
            )):
        raise ValueError("Pytest shard manifest source_inputs must map relative source paths to SHA-256 digests.")
    return cast(ShardManifest, value)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("myrk-shards")
    group.addoption("--myrk-shard", default=None)
    group.addoption("--myrk-shard-manifest", default=None)


def pytest_configure(config: pytest.Config) -> None:
    mode = cast(ShardMode | None, config.getoption("myrk_shard"))
    manifest_path = cast(str | None, config.getoption("myrk_shard_manifest"))
    if mode is None:
        if manifest_path is not None:
            raise pytest.UsageError("--myrk-shard-manifest requires --myrk-shard.")
        return

    from myrk.check_plugin import PROJECT, SOURCE_ROOT, managed_check_run

    run = managed_check_run(config.option.basetemp)
    if run is None:
        raise pytest.UsageError("Pytest shard modes require the managed scripts/run_checks.py entry point.")
    reports = _plain_path(run / "reports/pytest-shards")
    reports.mkdir(parents=True)
    plugin = ShardPlugin(mode, SOURCE_ROOT, reports)
    config.pluginmanager.register(plugin, "myrk-pytest-shard-session")
    plugin.save_observation()
    try:
        if mode == "collect":
            if manifest_path is not None or not config.option.collectonly:
                raise ValueError("The collect shard mode requires --collect-only and no shard manifest.")
        else:
            if manifest_path is None:
                raise ValueError("Pytest shard workers require --myrk-shard-manifest.")
            plugin.manifest = _load_manifest(Path(manifest_path), PROJECT, SOURCE_ROOT)
            if mode not in plugin.manifest["lanes"]:
                raise ValueError("The selected worker is absent from the pytest shard manifest.")
    except (OSError, ValueError) as error:
        plugin.fail(str(error))


class ShardPlugin:
    def __init__(self, mode: ShardMode, source: Path, reports: Path) -> None:
        self.mode = mode
        self.source = source
        self.directory = reports
        self.manifest: ShardManifest | None = None
        self.collected_nodeids: list[str] = []
        self.selected_nodeids: list[str] = []
        self.reports: list[dict[str, object]] = []
        self.collection_errors: list[dict[str, str]] = []
        self.internal_errors: list[str] = []
        self.collection_completed = False
        self.session_finished = False
        self.interrupted = False
        self.externally_deselected = False
        self.exitstatus: int | None = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.finished_at: str | None = None

    def save_observation(self) -> None:
        if self.mode == "collect":
            return
        observation = {
            "version": 1, "mode": self.mode, "source_root": str(self.source),
            "collected_nodeids": self.collected_nodeids, "selected_nodeids": self.selected_nodeids,
            "reports": self.reports, "exitstatus": self.exitstatus,
            "collection_completed": self.collection_completed, "session_finished": self.session_finished,
            "collection_errors": self.collection_errors, "internal_errors": self.internal_errors,
            "interrupted": self.interrupted, "started_at": self.started_at, "finished_at": self.finished_at,
        }
        (self.directory / "observation.json").write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")

    def fail(self, message: str) -> NoReturn:
        self.collection_errors.append({"nodeid": "", "outcome": "failed", "longrepr": message})
        self.save_observation()
        raise pytest.UsageError(message)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> Iterator[None]:
        yield
        self.collected_nodeids = [item.nodeid for item in items]
        if len(self.collected_nodeids) != len(set(self.collected_nodeids)):
            self.fail("The actual pytest collection contains duplicate node IDs.")
        try:
            source_inputs = get_source_inputs(self.source)
        except (OSError, ValueError) as error:
            self.fail(str(error))
        if self.mode == "collect":
            lanes: ShardLanes = {
                "formal": [nodeid for nodeid in self.collected_nodeids if nodeid.startswith(FORMAL_NODE_PREFIX)],
                "other": [nodeid for nodeid in self.collected_nodeids if not nodeid.startswith(FORMAL_NODE_PREFIX)],
            }
            manifest: ShardManifest = {
                "version": 2, "source_root": str(self.source), "nodeids": self.collected_nodeids,
                "lanes": lanes, "source_inputs": source_inputs,
                "timing_update_eligible": not self.externally_deselected and not config.getini("addopts"),
            }
            (self.directory / "collection.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        else:
            assert self.manifest is not None
            if self.collected_nodeids != self.manifest["nodeids"]:
                self.fail("The worker's complete pytest collection differs from the manifest.")
            if source_inputs != self.manifest["source_inputs"]:
                self.fail("Pytest shard source inputs changed after collection.")
            selected = set(self.manifest["lanes"][self.mode])
            deselected = [item for item in items if item.nodeid not in selected]
            items[:] = [item for item in items if item.nodeid in selected]
            self.selected_nodeids = [item.nodeid for item in items]
            if self.selected_nodeids != self.manifest["lanes"][self.mode]:
                self.fail("The worker's selected pytest lane differs from the manifest.")
            if deselected:
                config.hook.pytest_deselected(items=deselected)
        self.collection_completed = True
        self.save_observation()

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors.append({"nodeid": report.nodeid, "outcome": report.outcome, "longrepr": str(report.longrepr)})

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        if items:
            self.externally_deselected = True

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.reports.append({
            "nodeid": report.nodeid, "when": report.when,
            "outcome": report.outcome, "duration": report.duration,
            "start": report.start, "stop": report.stop,
        })

    def pytest_internalerror(self, excrepr: object) -> None:
        self.internal_errors.append(str(excrepr))
        self.save_observation()

    def pytest_keyboard_interrupt(self) -> None:
        self.interrupted = True
        self.save_observation()

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.exitstatus = int(session.exitstatus)
        self.session_finished = True
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.save_observation()

    def pytest_unconfigure(self) -> None:
        self.save_observation()
