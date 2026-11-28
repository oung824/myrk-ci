"""Verify acceptance evidence at its original check location."""

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from scripts.closeout_inventory import check_report_inventory, git, local_path, observe
from scripts.closeout_runs import (
    INVOCATION_ID, LAUNCHES, RESULTS, TERMINAL_STATES, ZERO_EXIT_STATES, group_plan, parallel_receipts,
    read_json, verify_artifacts, verify_parallel_child, verify_pass_requirement, verify_settled_receipt,
)


SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@dataclass
class BorrowedVerification:
    checks: list[dict[str, Any]] = field(default_factory=list)
    paths: set[Path] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def pinned_file(value: object, project: Path) -> tuple[Path, str]:
    if (not isinstance(value, dict) or not isinstance(value.get("sha256"), str)
            or not SHA256.fullmatch(value["sha256"])):
        raise ValueError("The original evidence needs a path and a SHA-256 fingerprint.")
    return local_path(value.get("path"), project, project), value["sha256"]


def verify_fingerprint(path: Path, expected: str, actual: str | None) -> None:
    if actual is None:
        raise ValueError(f"The original evidence is missing or metadata-only: {path}")
    if actual != expected:
        raise ValueError(f"The original evidence fingerprint changed: {path}")


def historical_receipt(
    project: Path, receipt_path: Path, origin: dict[str, Any], evidence: set[Path], metadata: list[Path],
) -> tuple[dict[str, Any], str]:
    receipt, digest = read_json(receipt_path, metadata)
    if receipt.get("task_id") is not None or receipt.get("invocation_id") is not None:
        raise ValueError(f"Historical evidence requires an unnumbered original without a launch identity: {receipt_path}")
    mode = origin["mode"]
    run_id = origin["invocation_id"]
    if mode == "desktop":
        matches = "desktop-" + Path(receipt["directory"]).name == run_id and receipt.get("ended") == origin.get("finished_at")
    else:
        matches = (receipt.get("started_at") == origin["started_at"] and receipt.get("finished_at") is not None
                   and receipt.get("result_path", str(receipt_path)) == str(receipt_path))
        if mode != "pytest-parallel":
            matches = matches and Path(receipt["runtime_directory"]).name == run_id
    if (not matches or receipt.get("project_root", str(project)) != str(project)
            or receipt.get("source_root", origin["source_root"]) != origin["source_root"]
            or receipt.get("mode", mode) != mode):
        raise ValueError(f"The archived run identity does not bind this original receipt: {receipt_path}")
    terminal = origin.get("terminal")
    if not isinstance(terminal, dict) or terminal.get("status") not in TERMINAL_STATES:
        raise ValueError(f"The archived run has no settled final receipt: {receipt_path}")
    exit_code = receipt.get("exit_code", receipt.get("result"))
    if (terminal.get("result_sha256") != digest or type(exit_code) is not int
            or (exit_code == 0) != (terminal["status"] in ZERO_EXIT_STATES)):
        raise ValueError(f"The original receipt differs from the archived final result: {receipt_path}")
    evidence.add(receipt_path)
    verify_artifacts(project, receipt, terminal, evidence, metadata)
    return receipt, digest


def parallel_evidence(
    project: Path, launch: dict[str, Any], receipt: dict[str, Any], archived: bool, metadata: list[Path],
) -> set[Path]:
    terminal = launch["terminal"]
    passed = terminal["status"] == "passed"
    groups = {} if archived else group_plan(launch)
    nested = parallel_receipts(launch, receipt, archived=archived)
    evidence: set[Path] = set()
    for name, group_receipt in nested.items():
        try:
            if group_receipt is None and not passed and (archived or name in terminal.get("not_run", [])):
                continue
            group_path = local_path(group_receipt.get("result_path") if isinstance(group_receipt, dict) else None, project, project)
            if group_path.parent != project / RESULTS or group_path.suffix != ".json":
                raise ValueError(f"The original final group receipt path is invalid: {group_path}")
            if archived:
                group, _ = read_json(group_path, metadata)
                evidence.add(group_path)
                check_report_inventory(group, {Path(path) for path in terminal.get("required_artifacts", {})}, project)
                if passed and group.get("exit_code") != 0:
                    raise ValueError(f"The passing run has a failed group: {group_path}")
            else:
                group_id = groups[name]
                group_origin = local_path(str(LAUNCHES / (group_id + ".json")), project, project)
                child, _ = read_json(group_origin, metadata)
                evidence.add(group_origin)
                verify_parallel_child(launch, name, child, group_origin, group_receipt)
                group, _ = verify_settled_receipt(project, group_origin, child, evidence, metadata)
            if group != group_receipt:
                raise ValueError(f"The original group receipt differs from its final parent receipt: {group_path}")
        except (OSError, TypeError, ValueError, KeyError) as error:
            raise ValueError(f"Original parallel group {name}: {error}") from error
    return evidence


def verify_borrowed_checks(
    task_id: str, project: Path, observed_at: str, references: object, metadata: list[Path],
) -> BorrowedVerification:
    if not isinstance(references, list):
        raise ValueError("closeout.borrowed_checks must be an array of acceptance evidence references.")
    result = BorrowedVerification()
    for index, reference in enumerate(references, start=1):
        label = str(index)
        try:
            if not isinstance(reference, dict) or not isinstance(reference.get("requirement"), str) or not reference["requirement"].strip():
                raise ValueError("A borrowed check needs a named acceptance requirement.")
            label = repr(reference["requirement"])
            invocation = reference.get("invocation_id")
            mode = reference.get("mode")
            original_task = reference.get("original_task_id")
            require_pass = reference.get("require_pass", True)
            if (not isinstance(invocation, str) or not invocation.strip()
                    or not isinstance(mode, str) or not mode.strip() or type(require_pass) is not bool
                    or original_task is not None and (not isinstance(original_task, str) or not original_task.strip())):
                raise ValueError("The reference needs a check invocation, mode, and valid original task and pass requirement.")
            if original_task == task_id:
                raise ValueError("The original task must be external to this task.")
            origin_path, origin_digest = pinned_file(reference.get("origin"), project)
            receipt_path, receipt_digest = pinned_file(reference.get("receipt"), project)
            archive = reference["origin"].get("commit")
            if receipt_path.parent != project / RESULTS or receipt_path.suffix != ".json":
                raise ValueError(f"The reference needs the original result path: {receipt_path}")
            if archive is None and (not INVOCATION_ID.fullmatch(invocation)
                    or origin_path != project / LAUNCHES / (invocation + ".json")):
                raise ValueError(f"The original launch path must match the invocation {invocation}: {origin_path}")
            if archive is not None and (not isinstance(archive, str) or not COMMIT.fullmatch(archive)
                    or origin_path.is_relative_to(project / LAUNCHES) or origin_path.is_relative_to(project / RESULTS)
                    or ".git" in origin_path.relative_to(project).parts or invocation != receipt_path.stem or original_task is not None):
                raise ValueError(f"Historical evidence needs an independent archived run matched to the original receipt: {origin_path}")
            artifacts = reference.get("artifacts", [])
            if not isinstance(artifacts, list):
                raise ValueError("The reference artifacts must be an array of original evidence files.")
            pins = dict(pinned_file(value, project) for value in artifacts)
            if len(pins) != len(artifacts) or origin_path in pins or receipt_path in pins:
                raise ValueError("The same original evidence file cannot be referenced twice.")
            result.paths.update((origin_path, receipt_path, *pins))

            launch, actual_origin = read_json(origin_path, metadata)
            if archive is not None:
                if (git(project, "cat-file", "-t", archive).strip() != b"commit"
                        or datetime.fromisoformat(git(project, "show", "-s", "--format=%cI", archive).decode().strip())
                        > datetime.fromisoformat(observed_at)
                        or hashlib.sha256(git(project, "cat-file", "blob",
                            f"{archive}:{origin_path.relative_to(project).as_posix()}")).hexdigest() != actual_origin
                        or launch.get("kind") != "historical_check"):
                    raise ValueError(f"The original run record differs from its archived copy: {origin_path}")
            if (launch.get("version") != 1 or launch.get("invocation_id") != invocation
                    or launch.get("project_root") != str(project) or launch.get("parent_invocation_id")):
                raise ValueError(f"The original launch identity differs from the reference: {origin_path}")
            started = launch.get("started_at")
            if not isinstance(started, str) or datetime.fromisoformat(started).utcoffset() is None:
                raise ValueError(f"The original launch has no valid start time: {origin_path}")
            if (launch.get("task_id") != original_task or launch.get("mode") != mode
                    or launch.get("source_root") != str(local_path(reference.get("source_root"), project, project))
                    or launch.get("result_path") != str(receipt_path)):
                raise ValueError(f"The original task, mode, source, or receipt binding differs: {origin_path} -> {receipt_path}")
            version = reference.get("source_version")
            if (not isinstance(version, dict) or not isinstance(version.get("commit"), str)
                    or not COMMIT.fullmatch(version["commit"]) or version.get("working_tree") not in ("clean", "modified")
                    or version.get("working_tree") == "modified" and (not isinstance(version.get("inputs_sha256"), str)
                        or not SHA256.fullmatch(version["inputs_sha256"]))
                    or launch.get("source_version") != version):
                raise ValueError(f"The expected source version does not match the exact checked inputs: {origin_path}")
            coverage = reference.get("coverage")
            if (not isinstance(coverage, dict) or not isinstance(coverage.get("arguments"), list)
                    or any(not isinstance(argument, str) for argument in coverage["arguments"])
                    or json.dumps(launch.get("coverage"), sort_keys=True) != json.dumps(coverage, sort_keys=True)):
                raise ValueError(f"The expected coverage differs from the original run coverage: {origin_path}")

            evidence = {origin_path}
            terminal = launch.get("terminal")
            if archive is None and (not isinstance(terminal, dict) or terminal.get("status") not in TERMINAL_STATES):
                raise ValueError(f"The launch has no settled final receipt: {receipt_path}")
            verify_fingerprint(origin_path, origin_digest, actual_origin)
            if archive is None:
                receipt, actual_receipt = verify_settled_receipt(project, origin_path, launch, evidence, metadata)
            else:
                receipt, actual_receipt = historical_receipt(project, receipt_path, launch, evidence, metadata)
            verify_fingerprint(receipt_path, receipt_digest, actual_receipt)
            status = launch["terminal"]["status"]
            if require_pass:
                verify_pass_requirement(launch, receipt, archived=archive is not None)
            if version["working_tree"] == "modified" and receipt.get("source_inputs_unchanged") is not True:
                raise ValueError(f"The original run does not verify unchanged modified source inputs: {receipt_path}")
            if receipt.get("pytest_config_unchanged") is False:
                raise ValueError(f"The original pytest configuration changed during the run: {receipt_path}")
            if mode == "pytest-parallel":
                evidence.update(parallel_evidence(project, launch, receipt, archive is not None, metadata))
            original_artifacts = evidence - {origin_path, receipt_path}
            for path in sorted(original_artifacts):
                if not path.is_relative_to(project / RESULTS) and path.parent != project / LAUNCHES:
                    raise ValueError(f"The original artifact is outside check evidence directories: {path}")
                if not path.is_file():
                    raise ValueError(f"The original evidence is missing: {path}")
            missing_pins = original_artifacts - pins.keys()
            if missing_pins:
                raise ValueError(f"The required original artifact needs an explicit SHA-256 fingerprint: {min(missing_pins)}")
            unrelated_pins = pins.keys() - original_artifacts
            if unrelated_pins:
                path = min(unrelated_pins)
                if not path.is_file():
                    raise ValueError(f"The referenced original artifact is missing: {path}")
                raise ValueError(f"The artifact is outside the original run evidence: {path}")
            for path, expected in pins.items():
                verify_fingerprint(path, expected, observe(path, metadata).get("sha256"))
            result.checks.append({
                "requirement": reference["requirement"], "invocation_id": invocation, "original_task_id": original_task,
                "mode": mode, "source_root": launch["source_root"], "source_version": version, "coverage": coverage,
                "status": status, "receipt": reference["receipt"], "origin": reference["origin"], "artifacts": artifacts,
                **({"retained_runtime": receipt["retained"]} if mode == "desktop" and isinstance(receipt.get("retained"), str) else {}),
            })
        except (OSError, TypeError, ValueError, KeyError, AttributeError) as error:
            result.errors.append(f"Borrowed check {label}: {error}")
    return result
