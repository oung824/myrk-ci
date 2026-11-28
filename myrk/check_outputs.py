"""Confine check outputs and preserve their evidence after runtime removal."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any
from uuid import uuid4


def validate_pytest_arguments(arguments: list[str]) -> None:
    for index, argument in enumerate(arguments):
        if argument.startswith("@"):
            raise ValueError("Pytest response files can hide output paths; expand their arguments directly.")
        plugin = arguments[index + 1] if argument == "-p" and index + 1 < len(arguments) else argument[2:] if argument.startswith("-p") else ""
        if argument == "--noconftest" or plugin.strip() in {"no:myrk.check_plugin", "no:pytest_myrk.check_plugin"}:
            raise ValueError("Pytest protection cannot be disabled; remove " + argument + (" " + plugin if argument == "-p" else ""))


def report_path(value: str, *, kind: str, run: Path, project: Path, source: Path) -> Path:
    requested = Path(value)
    if not value or ".." in requested.parts or (requested.drive and not requested.is_absolute()):
        raise ValueError(f"Output path must not escape its owned run ({kind}): {value}")
    original = requested if requested.is_absolute() else source / requested
    if any(part.is_symlink() or part.is_junction() for part in (original, *original.parents)):
        raise ValueError(f"Output paths must not use links ({kind}): {value}")
    original = original.resolve()
    if not original.is_relative_to(project):
        raise ValueError(f"Output path must be inside the project ({kind}): {value}")
    if original.is_relative_to(run / "reports"):
        destination = original
    else:
        relative = original.relative_to(project) if requested.is_absolute() else requested
        destination = run / "reports" / relative
    if any(part.is_symlink() or part.is_junction() for part in (destination, *destination.parents)):
        raise ValueError(f"Output paths must not use links ({kind}): {value}")
    return destination


def retain_reports(run: Path, result_path: Path, *, owner: str) -> dict[str, Any] | None:
    reports = run / "reports"
    if not reports.exists():
        return None
    if any(path.is_symlink() or path.is_junction() for path in (reports, *reports.rglob("*"), result_path.parent, *result_path.parent.parents)):
        raise ValueError("Check report preservation refused linked output paths.")
    files = sorted(path for path in reports.rglob("*") if path.is_file())
    if not files:
        return None
    size = sum(path.stat().st_size for path in files)
    destination = result_path.parent / f"{run.name}.reports"
    reports.replace(destination)
    return {
        "owner": owner, "paths": [str(destination)], "bytes": size,
        "files": [str(destination / path.relative_to(reports)) for path in files],
        "reason": "Reports explicitly requested for this check command.",
        "delete_when": "The requested reports have been reviewed or their necessary results saved with the task.",
    }


def configuration_path(arguments: list[str], source: Path) -> Path:
    validate_pytest_arguments([*arguments, *shlex.split(os.environ.get("PYTEST_ADDOPTS", ""))])
    config_file = None
    for index, argument in enumerate(arguments):
        if argument in {"-c", "--config-file"}:
            if index + 1 == len(arguments):
                raise ValueError("Pytest configuration requires a project-local path.")
            config_file = arguments[index + 1]
        elif argument.startswith("--config-file="):
            config_file = argument.partition("=")[2]
        elif argument.startswith("-c"):
            config_file = argument[2:].removeprefix("=")
    configuration = Path(config_file) if config_file is not None else source / "pyproject.toml"
    return configuration if configuration.is_absolute() else source / configuration


def configuration_input(path: Path, project: Path) -> dict[str, str | None]:
    if (".." in path.parts or any(part.is_symlink() or part.is_junction() for part in (path, *path.parents))
            or not path.resolve().is_relative_to(project)):
        raise ValueError(f"Pytest configuration must be an ordinary project-local path: {path}")
    digest = None
    if path.is_file():
        with path.open("rb") as current:
            digest = hashlib.file_digest(current, "sha256").hexdigest()
    return {"path": str(path.resolve()), "sha256": digest}


def confine_outputs(check: str, arguments: list[str], run: Path, *, source: Path, project: Path) -> list[str]:
    """Resolve supported tool output switches inside this command's owned run."""
    rewritten = list(arguments)
    if check == "mypy":
        if any(argument.startswith("@") for argument in rewritten):
            raise ValueError("Mypy response files can hide output paths; pass their arguments directly.")
        from mypy.config_parser import parse_config_file
        from mypy.main import define_options
        from mypy.options import Options

        option_parser, _, _ = define_options(stdout=io.StringIO(), stderr=io.StringIO())
        for index, argument in enumerate(rewritten):
            flag, separator, value = argument.partition("=")
            if flag == "--":
                break
            if not flag.startswith("--") or flag in option_parser._option_string_actions:
                continue
            matches = [name for name in option_parser._option_string_actions if name.startswith(flag)]
            if len(matches) == 1:
                rewritten[index] = matches[0] + ("=" + value if separator else "")
        config_file = None
        for index, argument in enumerate(rewritten):
            if argument == "--config-file" and index + 1 < len(rewritten):
                config_file = rewritten[index + 1]
            elif argument.startswith("--config-file="):
                config_file = argument.partition("=")[2]
        if config_file is None:
            config_file = next((str(source / name) for name in ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg") if (source / name).is_file()), "")
            rewritten.append("--config-file=" + config_file)
        if config_file:
            configured = Options()
            filename = Path(config_file)
            if not filename.is_absolute():
                filename = source / filename
            parse_config_file(configured, lambda: None, str(filename), stdout=io.StringIO(), stderr=io.StringIO())
            existing = {argument.partition("=")[0] for argument in rewritten}
            for kind, location in configured.report_dirs.items():
                flag = "--" + kind + "-report"
                if flag not in existing:
                    rewritten.extend([flag, location])
            if configured.junit_xml and "--junit-xml" not in existing:
                rewritten.extend(["--junit-xml", configured.junit_xml])
    index = 0
    while index < len(rewritten):
        argument = rewritten[index]
        flag, separator, value = argument.partition("=")
        if check == "pytest" and flag == "--debug" and not separator and (index + 1 == len(rewritten) or rewritten[index + 1].startswith("-")):
            separator, value = "=", "pytestdebug.log"
        if flag in {"-o", "--override-ini"}:
            override = value if separator else (rewritten[index + 1] if index + 1 < len(rewritten) else "")
            if override.startswith("log_file="):
                destination = report_path(override.partition("=")[2], kind="log_file", run=run, project=project, source=source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                rewritten[index if separator else index + 1] = (flag + "=" if separator else "") + "log_file=" + str(destination)
            index += 1 if separator else 2
            continue
        output_flag = flag in {"--junitxml", "--junit-xml", "--log-file", "--html", "--json-report-file", "--output-file"}
        output_flag = output_flag or (check == "pytest" and flag == "--debug")
        output_flag = output_flag or (check == "mypy" and flag.startswith("--") and flag.endswith("-report"))
        coverage = flag == "--cov-report"
        if not output_flag and not coverage:
            index += 1
            continue
        if not separator:
            index += 1
            if index >= len(rewritten):
                raise ValueError(f"Output option {flag} requires a path.")
            value = rewritten[index]
        prefix = ""
        if coverage:
            kind, colon, location = value.partition(":")
            if kind in {"term", "term-missing", "annotate"} and not colon:
                if kind != "annotate":
                    index += 1
                    continue
            if kind == "" or kind.startswith("term"):
                index += 1
                continue
            prefix = kind + ":"
            value = location if colon else {"xml": "coverage.xml", "json": "coverage.json", "lcov": "coverage.lcov"}.get(kind, "coverage-" + kind)
        destination = report_path(value, kind=flag, run=run, project=project, source=source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rewritten[index] = (flag + "=" if separator else "") + prefix + str(destination)
        index += 1
    return rewritten


def finalize_check_outputs(
    run: Path, parent: Path, result_path: Path, result: dict[str, Any],
    checks: tuple[str, ...], commands: list[dict[str, Any]], stop_errors: list[str],
) -> None:
    results = result_path.parent
    owner = result["owner"]
    failure_logs: list[str] = result["failure_logs"]
    try:
        for check, entry in zip(checks, commands):
            stdout_path, stderr_path = run / f"{check}.stdout.txt", run / f"{check}.stderr.txt"
            entry["output_tail"] = stdout_path.read_text(encoding="utf-8", errors="replace")[-2000:] if stdout_path.exists() else ""
            if entry["exit_code"]:
                for source in (stdout_path, stderr_path):
                    if source.is_file():
                        destination = results / f"{run.name}.{source.name}"
                        source.replace(destination)
                        failure_logs.append(str(destination))
    except OSError as error:
        stop_errors.append(f"Cannot save the check output: {error}")
    if stop_errors:
        result["cleanup_error"] = "; ".join(stop_errors)
    else:
        try:
            retained_reports = retain_reports(run, result_path, owner=str(owner))
            if retained_reports is not None:
                result["retained_reports"] = retained_reports
        except (OSError, ValueError) as error:
            result["cleanup_error"] = str(error)
    if "cleanup_error" in result:
        pass
    elif any(path.is_symlink() or path.is_junction() for path in (run, *run.parents)) or run.parent != parent:
        result["cleanup_error"] = "The check runtime path changed; automatic removal was refused."
    else:
        try:
            shutil.rmtree(run)
            result["runtime_removed"] = not run.exists()
            if not result["runtime_removed"]:
                result["cleanup_error"] = "The check runtime still exists after removal."
        except OSError as error:
            result["cleanup_error"] = str(error)
    if failure_logs:
        result["retained"] = {
            "owner": owner, "reason": "Original output of a failed backend check.",
            "delete_when": "The failure is resolved or its necessary evidence is saved with the task.",
            "paths": failure_logs,
        }
    if not result["runtime_removed"]:
        result["exit_code"] = result["exit_code"] or 1
        result["retained_runtime"] = {
            "path": str(run), "owner": owner,
            "reason": result["cleanup_error"],
            "delete_when": "The recorded process has exited and the path is ordinary and no longer locked.",
        }


def source_version(source: Path) -> dict[str, Any]:
    git = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(source)]
    try:
        commit = subprocess.run([*git, "rev-parse", "--show-toplevel", "HEAD"], capture_output=True)
    except OSError:
        return {"commit": None, "working_tree": "unknown"}
    if commit.returncode:
        return {"commit": None, "working_tree": "unknown"}
    checkout, revision = commit.stdout.decode("utf-8").splitlines()
    if Path(checkout) != source:
        return {"commit": None, "working_tree": "unknown"}
    scopes = ["myrk", "scripts", "tests", "desktop", "conftest.py", "pyproject.toml", "pytest.ini",
              "mypy.ini", ".mypy.ini", "setup.cfg", "tox.ini", "requirements*"]
    tracked = subprocess.run([*git, "ls-files", "--cached", "-z", "--", *scopes], capture_output=True)
    filters = subprocess.run([*git, "check-attr", "-z", "--stdin", "filter"], input=tracked.stdout, capture_output=True)
    if tracked.returncode or filters.returncode or any(value not in (b"unspecified", b"unset")
            for value in filters.stdout.split(b"\0")[2::3]):
        return {"commit": revision, "working_tree": "unknown"}
    status = subprocess.run(
        [*git, "status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "--", *scopes], capture_output=True,
    )
    return {"commit": revision,
            "working_tree": "unknown" if status.returncode else "modified" if status.stdout else "clean"}


def successful_pytest_status(coverage: dict[str, Any]) -> str:
    selected = coverage.get("pytest")
    if selected is None or not selected["node_count"] or selected.get("setup_or_fixtures_only"):
        return "not_executed"
    return "collected" if selected["collect_only"] else "passed"


class CheckLaunch:
    """Verify a launch and result outside the runtime."""

    def __init__(self, project: Path, source: Path, result_path: Path, invocation_id: str,
                 mode: str, arguments: list[str], *, parent: str | None = None,
                 group: str | None = None, version: dict[str, Any] | None = None) -> None:
        directory = project / ".scratch/check-runs/launches"
        if any(path.is_symlink() or path.is_junction() for path in (directory, *directory.parents)):
            raise ValueError(f"Check launch directory must not use links: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{invocation_id}.json"
        self.project = project
        self.result_path = result_path
        self.coverage: dict[str, Any] = {"arguments": arguments}
        self.record: dict[str, Any] = {
            "version": 1, "invocation_id": invocation_id, "mode": mode, "pid": os.getpid(),
            "project_root": str(project), "source_root": str(source), "result_path": str(result_path),
            "started_at": datetime.now(timezone.utc).isoformat(), "coverage": self.coverage,
        }
        if task_id := os.environ.get("MYRK_TASK_ID"):
            self.record["task_id"] = task_id
        if parent is not None:
            self.record.update(parent_invocation_id=parent, group=group)
        self.record["source_version"] = dict(version) if version is not None else source_version(source)
        if self.path.exists():
            raise FileExistsError(f"Check invocation already recorded: {self.path}")
        publish_json(self.path, self.record)

    def set_groups(self, groups: dict[str, str]) -> None:
        self.record["groups"] = groups
        publish_json(self.path, self.record)

    def bind_pytest_config(self, path: Path) -> None:
        binding = configuration_input(path, self.project)
        version = self.record["source_version"]
        if version.get("pytest_config", binding) != binding:
            raise ValueError(f"Pytest configuration differs from the parent check inputs: {path}")
        version["pytest_config"] = binding
        publish_json(self.path, self.record)

    def verify_pytest_config(self, result: dict[str, Any]) -> None:
        binding = self.record["source_version"].get("pytest_config")
        if binding is None:
            return
        result["pytest_config"] = binding
        path = Path(binding["path"])
        try:
            unchanged = configuration_input(path, self.project) == binding
        except (OSError, ValueError):
            unchanged = False
        result["pytest_config_unchanged"] = unchanged
        if not unchanged:
            result["pytest_config_error"] = f"Pytest configuration changed during the check: {path}"
            result["exit_code"] = result["exit_code"] or 1

    def collect_backend_pytest_coverage(self, run: Path, result: dict[str, Any]) -> None:
        path = run / "pytest-coverage.json"
        if path.is_file():
            self.coverage["pytest"] = json.loads(path.read_text(encoding="utf-8"))
        self.verify_pytest_config(result)

    def finish(self, result: dict[str, Any], status: str, *, not_run: list[str] | None = None,
               required_artifacts: list[Path] | None = None) -> None:
        paths = {*(required_artifacts or []), *(Path(path) for path in result.get("failure_logs", [])),
                 *(Path(path) for path in result.get("retained", {}).get("paths", [])),
                 *(Path(path) for path in result.get("retained_reports", {}).get("files", []))}
        artifacts = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
        result.update(invocation_id=self.record["invocation_id"], result_path=str(self.result_path),
                      artifact_inventory_complete=True)
        if coverage := self.coverage.get("pytest"):
            result["pytest_coverage"] = coverage
            if not self.record.get("parent_invocation_id"):
                current = source_version(Path(self.record["source_root"]))
                version = self.record["source_version"]
                coverage["checkout_unchanged"] = current["working_tree"] != "unknown" and all(
                    version[key] == value for key, value in current.items()
                )
                if status == "passed" and not coverage.get("passed_calls"):
                    status = "no_tests_passed"
                elif status == "passed" and not (coverage.get("selected_inputs_unchanged") and coverage["checkout_unchanged"]):
                    status = "inputs_unverified"
        payload = publish_json(self.result_path, result)
        self.record["terminal"] = {
            "status": status, "result_sha256": hashlib.sha256(payload).hexdigest(), "artifact_inventory_complete": True,
        }
        if not_run:
            self.record["terminal"]["not_run"] = not_run
        if artifacts:
            self.record["terminal"]["required_artifacts"] = artifacts
        publish_json(self.path, self.record)


def settle_backend_launch(
    launch: CheckLaunch, result: dict[str, Any], checks: tuple[str, ...], commands: list[dict[str, Any]],
) -> None:
    if result.get("interrupted") == "KeyboardInterrupt" or any(
        entry["exit_code"] in (2, 130) and name == "pytest" for name, entry in zip(checks, commands)
    ):
        status = "interrupted"
    elif (not commands and result.get("interrupted")) or any("startup_error" in entry for entry in commands):
        status = "startup_failed"
    elif result["exit_code"]:
        status = "failed"
    else:
        status = successful_pytest_status(launch.coverage) if "pytest" in checks else "passed"
    launch.finish(result, status)


def publish_json(path: Path, content: dict[str, Any]) -> bytes:
    payload = (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    pending = path.with_name(f".{path.stem}-{uuid4().hex}.tmp")
    try:
        with pending.open("xb") as output:
            output.write(payload)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return payload
