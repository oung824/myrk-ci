"""Trace task checks to their launch records and completed evidence."""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import Any

from scripts.closeout_inventory import check_report_inventory, local_path, read_json as read_json


LAUNCHES = Path(".scratch/check-runs/launches")
RESULTS = Path(".scratch/check-runs/results")
INVOCATION_ID = re.compile(r"[0-9a-f]{32}")
ZERO_EXIT_STATES = {"passed", "collected", "not_executed", "no_tests_passed", "inputs_unverified"}
TERMINAL_STATES = ZERO_EXIT_STATES | {"failed", "interrupted", "startup_failed"}


@dataclass
class RunVerification:
    runs: list[dict[str, Any]] = field(default_factory=list)
    paths: set[Path] = field(default_factory=set)
    originals: set[Path] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def load_launches(project: Path, metadata: list[Path]) -> dict[str, dict[str, Any]]:
    directory = local_path(str(LAUNCHES), project, project)
    launches: dict[str, dict[str, Any]] = {}
    for candidate in sorted(directory.glob("*.json")):
        if not INVOCATION_ID.fullmatch(candidate.stem):
            continue
        try:
            path = local_path(str(candidate), project, project)
            launch, _ = read_json(path, metadata)
            if (launch.get("version") != 1 or launch.get("invocation_id") != path.stem
                    or not isinstance(launch.get("project_root"), str) or Path(launch["project_root"]) != project
                    or not isinstance(launch.get("source_root"), str) or not isinstance(launch.get("mode"), str)
                    or not isinstance(launch.get("coverage"), dict) or not isinstance(launch.get("source_version"), dict)
                    or not isinstance(launch.get("started_at"), str)):
                raise ValueError(f"The launch record has an invalid identity: {path}")
            if datetime.fromisoformat(launch["started_at"]).utcoffset() is None:
                raise ValueError(f"The launch record has no time zone: {path}")
        except (OSError, ValueError) as error:
            launches[candidate.stem] = {"unreadable": str(error)}
            continue
        launches[path.stem] = launch
    return launches


def verify_artifacts(
    project: Path, receipt: dict[str, Any], terminal: dict[str, Any], evidence: set[Path], metadata: list[Path],
    *, allow_unsealed: bool = False,
) -> None:
    sealed = {local_path(value, project, project): expected for value, expected in terminal.get("required_artifacts", {}).items()}
    if allow_unsealed and receipt.get("artifact_inventory_complete") is not True:
        retained = receipt.get("retained")
        reported = [*receipt.get("failure_logs", []), *receipt.get("reports", []),
                    *receipt.get("retained_reports", {}).get("paths", [])]
        if isinstance(retained, dict):
            reported.extend(retained.get("paths", []))
        for value in reported:
            path = local_path(value, project, project)
            if path.is_dir():
                evidence.update(local_path(str(file), project, project) for file in path.rglob("*") if file.is_file())
            else:
                evidence.add(path)
    else:
        check_report_inventory(receipt, set(sealed), project)
    for path, expected in sealed.items():
        if not path.is_relative_to(project / RESULTS):
            raise ValueError(f"The required check artifact is outside the results directory: {path}")
        evidence.add(path)
        if not path.is_file():
            raise ValueError(f"The required check artifact is missing: {path}")
        if any(path.is_relative_to(item) for item in metadata) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"The required check artifact fingerprint changed from the settled launch: {path}")


def verify_settled_receipt(
    project: Path, launch_path: Path, launch: dict[str, Any], evidence: set[Path], metadata: list[Path],
    *, allow_unsealed: bool = False,
) -> tuple[dict[str, Any], str]:
    receipt_path = local_path(launch["result_path"], project, project)
    if receipt_path.parent != project / RESULTS or receipt_path.suffix != ".json":
        raise ValueError(f"The final receipt path is invalid: {receipt_path}")
    evidence.add(receipt_path)
    terminal = launch.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("status") not in TERMINAL_STATES:
        raise ValueError(f"The launch has no settled final receipt: {receipt_path}")
    if not isinstance(terminal.get("required_artifacts", {}), dict) or not isinstance(terminal.get("not_run", []), list):
        raise ValueError(f"The launch has an invalid terminal record: {launch_path}")
    if not receipt_path.is_file():
        raise ValueError(f"The final receipt is missing: {receipt_path}")
    receipt, actual = read_json(receipt_path, metadata)
    if (terminal.get("result_sha256") != actual or receipt.get("invocation_id") != launch["invocation_id"]
            or receipt.get("source_root") != launch["source_root"] or receipt.get("project_root") != launch["project_root"]
            or receipt.get("task_id") != launch.get("task_id") or receipt.get("result_path") != str(receipt_path)):
        raise ValueError(f"The final receipt differs from the settled launch: {receipt_path}")
    exit_code = receipt.get("exit_code", receipt.get("result"))
    if type(exit_code) is not int or (exit_code == 0) != (terminal["status"] in ZERO_EXIT_STATES):
        raise ValueError(f"The final receipt status differs from the settled launch: {receipt_path}")
    if ("pytest_coverage" in receipt and receipt["pytest_coverage"] != launch["coverage"].get("pytest")
            or "pytest_config" in receipt and receipt["pytest_config"] != launch["source_version"].get("pytest_config")):
        raise ValueError(f"The final pytest coverage or configuration differs from the settled launch: {receipt_path}")
    verify_artifacts(project, receipt, terminal, evidence, metadata, allow_unsealed=allow_unsealed)
    return receipt, actual


def verify_pass_requirement(launch: dict[str, Any], receipt: dict[str, Any], *, archived: bool = False) -> None:
    status = launch["terminal"]["status"]
    path = launch["result_path"]
    if status != "passed":
        raise ValueError(f"The requirement needs a passing result; actual status is {status}: {path}")
    if not archived and not launch.get("parent_invocation_id") and launch["mode"] in {"pytest", "bare-pytest", "all"}:
        coverage = receipt.get("pytest_coverage", {})
        passed = coverage.get("passed_calls")
        if (type(passed) is not int or passed <= 0 or not coverage.get("selected_files")
                or coverage.get("selected_inputs_unchanged") is not True or coverage.get("checkout_unchanged") is not True):
            raise ValueError(f"The passing result lacks verified pytest execution and input evidence: {path}")


def parallel_receipts(launch: dict[str, Any], receipt: dict[str, Any], *, archived: bool = False) -> dict[str, Any]:
    terminal = launch["terminal"]
    lanes = receipt["lanes"]
    nested = {name: lane["receipt"] for name, lane in lanes.items() if lane["expected_nodeids"]}
    nested["collection"] = receipt["collection_receipt"]
    groups = launch.get("groups", {})
    if not archived and groups.keys() != nested.keys():
        missing = sorted(nested.keys() - groups.keys())
        extra = sorted(groups.keys() - nested.keys())
        raise ValueError(f"The parallel group plan differs from its receipt (missing {missing}, extra {extra}): {receipt['result_path']}")
    if terminal["status"] == "passed":
        if receipt.get("source_inputs_unchanged") is not True or receipt["completeness"]["complete"] is not True:
            raise ValueError(f"The passing parallel receipt needs unchanged inputs and complete coverage: {receipt['result_path']}")
        sealed = terminal.get("required_artifacts", {})
        manifest = receipt.get("manifest_path")
        if not manifest or manifest not in sealed:
            raise ValueError(f"The parallel coverage needs its sealed collection manifest: {manifest or receipt['result_path']}")
        for name in nested.keys() - {"collection"}:
            observation = lanes[name].get("observation_path")
            if not observation or observation not in sealed:
                raise ValueError(f"The parallel group {name} needs its sealed observation: {observation or receipt['result_path']}")
    return nested


def group_plan(launch: dict[str, Any]) -> dict[str, str]:
    groups = launch.get("groups", {})
    if (not isinstance(groups, dict)
            or any(name not in {"collection", "lane1", "lane2", "formal", "other", "serial"}
                   or not isinstance(child, str) or not INVOCATION_ID.fullmatch(child) for name, child in groups.items())
            or len(set(groups.values())) != len(groups)):
        raise ValueError(f"The check launch has an invalid group plan: {launch['invocation_id']}")
    return groups


def verify_parallel_child(
    parent: dict[str, Any], name: str, child: dict[str, Any], origin: Path, nested: dict[str, Any] | None,
) -> None:
    version = parent["source_version"]
    if name == "collection":
        version = {key: value for key, value in version.items() if key != "inputs_sha256"}
    if (child.get("version") != 1 or child.get("invocation_id") != origin.stem
            or child.get("parent_invocation_id") != parent["invocation_id"] or child.get("group") != name
            or child.get("mode") != "pytest" or child.get("source_version") != version
            or any(child.get(field) != parent.get(field) for field in ("task_id", "project_root", "source_root"))
            or (nested is not None and child.get("result_path") != nested.get("result_path"))):
        raise ValueError(f"The group launch does not match its recorded parallel parent: {origin}")
    expected = "collected" if name == "collection" and "pytest" in child["coverage"] else "passed"
    if parent.get("terminal", {}).get("status") == "passed" and child.get("terminal", {}).get("status") != expected:
        raise ValueError(f"The passing parallel run has a group without a successful {name} result: {child['result_path']}")


def requirements_for_runs(value: object) -> dict[str, bool]:
    if not isinstance(value, list):
        raise ValueError("closeout.check_runs must be an array of check requirements.")
    requirements: dict[str, bool] = {}
    for entry in value:
        if (not isinstance(entry, dict) or not isinstance(entry.get("invocation_id"), str)
                or not INVOCATION_ID.fullmatch(entry["invocation_id"])
                or type(entry.get("require_pass", False)) is not bool):
            raise ValueError("A check requirement needs an invocation_id and an optional boolean require_pass.")
        invocation_id = entry["invocation_id"]
        if invocation_id in requirements:
            raise ValueError(f"Duplicate check run requirement: {invocation_id}")
        requirements[invocation_id] = entry.get("require_pass", False)
    return requirements


def verify_task_runs(
    source: Path, project: Path, baseline: dict[str, Any], closeout: dict[str, Any],
    original: dict[Path, dict[str, Any]], declared: dict[Path, dict[str, Any]], metadata: list[Path],
) -> RunVerification:
    task_id = baseline["task_id"]
    observed_at = datetime.fromisoformat(baseline["runs_observed_at"])
    if observed_at.utcoffset() is None:
        raise ValueError("The baseline check observation needs a time zone.")
    requirements = requirements_for_runs(closeout.get("check_runs", []))
    launches = load_launches(project, metadata)
    result = RunVerification()
    visited: set[str] = set()
    linked_receipts = {Path(launch["result_path"]): launch for launch in launches.values()
                       if isinstance(launch.get("result_path"), str)}

    def verify(invocation_id: str, launch: dict[str, Any], group: str | None = None) -> dict[str, Any] | None:
        label = f"Check run {invocation_id}" if group is None else f"Check group {group} ({invocation_id})"
        visited.add(invocation_id)
        launch_path = project / LAUNCHES / (invocation_id + ".json")
        result.paths.add(launch_path)
        if not launch or "unreadable" in launch:
            result.errors.append(f"{label} has a missing or unreadable launch: {launch.get('unreadable', invocation_id)}")
            return None
        evidence = {launch_path}
        report = {"invocation_id": invocation_id, "mode": launch["mode"], "source_version": launch["source_version"],
                  "coverage": launch["coverage"], "status": "unresolved"}
        if group is not None:
            report.update(group=group, parent_invocation_id=launch.get("parent_invocation_id"))
        result.runs.append(report)
        receipt = None
        try:
            receipt, actual = verify_settled_receipt(project, launch_path, launch, evidence, metadata, allow_unsealed=True)
            terminal = launch["terminal"]
            exit_code = receipt.get("exit_code", receipt.get("result"))
            report.update(status=terminal["status"], exit_code=exit_code, result_sha256=actual)
            if receipt.get("artifact_inventory_complete") is not True:
                report["artifact_inventory"] = "legacy"
            if launch["mode"] == "desktop" and isinstance(receipt.get("retained"), str):
                report["retained_runtime"] = receipt["retained"]
            if requirements.get(invocation_id):
                verify_pass_requirement(launch, receipt)
            result.originals.add(launch_path)
            result.originals.update(local_path(value, project, project)
                                    for value in (launch["result_path"], *terminal.get("required_artifacts", {})))
        except (OSError, ValueError, TypeError, KeyError) as error:
            result.errors.append(f"{label}: {error}")
            receipt = None
        finally:
            result.paths.update(evidence)
            report["evidence"] = [{"path": str(path), "outcome": declared.get(path, {}).get("outcome")} for path in sorted(evidence)]
        return receipt

    for invocation_id, launch in launches.items():
        if "unreadable" in launch or launch.get("parent_invocation_id"):
            continue
        launch_source = Path(launch["source_root"])
        same_source = launch_source == source
        claim = launch.get("task_id")
        explicit = invocation_id in requirements
        explicit_source = False
        if explicit and launch_source.is_absolute() and claim in (None, task_id):
            try:
                local_path(launch["source_root"], project, project)
                explicit_source = True
            except ValueError:
                pass
        if (same_source and claim == task_id) or explicit_source:
            receipt = verify(invocation_id, launch)
        elif explicit or claim == task_id or (same_source and not claim and datetime.fromisoformat(launch["started_at"]) >= observed_at):
            path = project / LAUNCHES / (invocation_id + ".json")
            result.paths.add(path)
            result.errors.append(f"Check run ownership unresolved: {path}")
            continue
        else:
            continue
        try:
            groups = group_plan(launch)
        except ValueError as error:
            result.errors.append(str(error))
            continue
        expected = {}
        if receipt and launch["mode"] == "pytest-parallel":
            try:
                expected = parallel_receipts(launch, receipt)
            except (TypeError, ValueError, KeyError, AttributeError) as error:
                result.errors.append(f"Check run {invocation_id}: {error}")
        for name, child_id in groups.items():
            child = launches.get(child_id, {})
            nested = expected.get(name)
            terminal = launch.get("terminal", {})
            if not child and receipt and terminal.get("status") != "passed" and name in terminal.get("not_run", []) and not nested:
                result.runs.append({"invocation_id": child_id, "parent_invocation_id": invocation_id, "group": name, "status": "not_run"})
                continue
            if child and "unreadable" not in child:
                child_path = project / LAUNCHES / (child_id + ".json")
                try:
                    verify_parallel_child(launch, name, child, child_path, nested)
                except (TypeError, ValueError, AttributeError) as error:
                    result.errors.append(f"Check group {name} ({child_id}): {error}")
            child_receipt = verify(child_id, child, name)
            if expected and child_receipt and nested != child_receipt:
                result.errors.append(f"Check group {name} ({child_id}) does not match the final parent receipt {launch['result_path']}.")

    for invocation_id in requirements.keys() - visited:
        result.errors.append(f"Required check run has no verified task launch: {invocation_id}")
    for invocation_id, launch in launches.items():
        if invocation_id not in visited and launch.get("parent_invocation_id") and (
                launch.get("task_id") == task_id or (not launch.get("task_id")
                and Path(launch["source_root"]) == source and datetime.fromisoformat(launch["started_at"]) >= observed_at)):
            path = project / LAUNCHES / (invocation_id + ".json")
            result.paths.add(path)
            result.errors.append(f"Check group ownership unresolved: {path}")
    for root in dict.fromkeys((source, project)):
        try:
            directory = local_path(str(root / RESULTS), project, project)
        except ValueError:
            if root == project:
                raise
            continue
        for path in directory.glob("*.json"):
            if not path.name.startswith(("parallel-", "b-", "p-", "desktop-")):
                continue
            bound_launch = linked_receipts.get(path)
            try:
                path = local_path(str(path), project, project)
                if bound_launch:
                    origin = project / LAUNCHES / (bound_launch["invocation_id"] + ".json")
                    if path.stat().st_mtime_ns < origin.stat().st_mtime_ns:
                        continue
                receipt, digest = read_json(path, metadata)
            except (OSError, ValueError):
                continue
            if bound_launch and all(receipt.get(field) == bound_launch.get(field) for field in
                                    ("invocation_id", "task_id", "project_root", "source_root")):
                continue
            claim = receipt.get("task_id")
            if not claim and receipt.get("source_root") == str(source):
                started = receipt.get("started_at", receipt.get("started"))
                try:
                    date = datetime.fromisoformat(started) if isinstance(started, str) else None
                    recent = date >= observed_at if date is not None and date.utcoffset() is not None else (
                        digest != original[path].get("sha256") if path in original
                        else path.stat().st_mtime_ns / 1e9 >= observed_at.timestamp()
                    )
                except (OSError, ValueError):
                    recent = True
            else:
                recent = False
            if claim == task_id or recent:
                result.paths.add(path)
                result.errors.append(f"Check result ownership unresolved without a matching launch: {path}")
    return result
