"""Coordinate two independently owned backend check runs."""

from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import os
import subprocess
import sys
from threading import Event
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

from myrk.check_outputs import CheckLaunch, configuration_path
from scripts.pytest_shards import FORMAL_PARAMETER_FUNCTIONS, get_source_inputs, read_observation, read_timings, update_timing_index


def run_parallel(
    arguments: list[str], run_check: Callable[..., dict[str, Any]], *,
    source: Path, project: Path,
) -> dict[str, Any]:
    results = project / ".scratch/check-runs/results"
    if any(path.is_symlink() or path.is_junction() for path in (results, *results.parents)):
        raise ValueError("Parallel check results must not use links.")
    results.mkdir(parents=True, exist_ok=True)
    invocation_id = uuid4().hex
    result_path = results / f"parallel-{invocation_id}.json"
    launch = CheckLaunch(project, source, result_path, invocation_id, "pytest-parallel", arguments)
    groups = {"collection": uuid4().hex}
    launch.set_groups(groups)
    dispatched: set[str] = set()
    started = monotonic()
    stop = Event()
    result: dict[str, Any] = {
        "mode": "pytest-parallel", "owner": "parallel-backend-check", "pid": os.getpid(),
        "project_root": str(project), "source_root": str(source),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "result_path": str(result_path), "exit_code": 0, "runtime_removed": False,
        "execution_mode": "not_started", "collection_receipt": None, "lanes": {},
        "scheduling_mode": os.environ.get("MYRK_TEST_SCHEDULING", "duration"),
        "completeness": {"complete": False, "expected_count": 0, "reported_count": 0},
    }
    if task_id := os.environ.get("MYRK_TASK_ID"):
        result["task_id"] = task_id
    futures: dict[Future[dict[str, Any]], str] = {}
    executor: ThreadPoolExecutor | None = None
    plan: dict[str, Any] | None = None
    observed_seconds: dict[str, float] = {}
    timing_path = project / ".scratch/check-runs/timings/backend.json"
    environment: dict[str, Any] = {}

    def run_group(name: str, arguments: list[str]) -> dict[str, Any]:
        return run_check("pytest", arguments, stop=stop, below_normal=True, invocation_id=groups[name],
                         parent_invocation_id=invocation_id, group=name, version=launch.record["source_version"])

    try:
        if result["scheduling_mode"] not in {"legacy", "duration"}:
            raise ValueError("MYRK_TEST_SCHEDULING must be legacy or duration.")
        launch.bind_pytest_config(configuration_path(arguments, source))
        dependency_inputs = {name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("requirements.txt", "requirements-dev.txt", "requirements.lock", "desktop/package-lock.json")
            if (source / name).is_file()}
        environment = {"python": list(sys.version_info[:3]), "platform": sys.platform, "pytest": version("pytest"),
            "dependency_inputs": dependency_inputs}
        result["timing_index"] = {"path": str(timing_path), "read_status": "not_read", "update_status": "not_eligible"}
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.GetPriorityClass.argtypes, kernel.GetPriorityClass.restype = [wintypes.HANDLE], wintypes.DWORD
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000):
            raise ctypes.WinError(ctypes.get_last_error())
        result["coordinator_priority_class"] = int(kernel.GetPriorityClass(kernel.GetCurrentProcess()))
        dispatched.add("collection")
        collected = run_group("collection", [*arguments, "--collect-only", "-p", "scripts.pytest_shards",
                                              "--myrk-shard=collect"])
        result["collection_receipt"] = collected
        if collected["exit_code"]:
            result["exit_code"] = collected["exit_code"]
            return result
        roots = collected.get("retained_reports", {}).get("paths", [])
        paths = [Path(root) / "pytest-shards/collection.json" for root in roots]
        paths = [path for path in paths if path.is_file()]
        if len(paths) != 1:
            raise ValueError("The collection did not produce exactly one complete shard manifest.")
        plan = json.loads(paths[0].read_text(encoding="utf-8"))
        serial_nodes = [node for node in plan["nodeids"]
            if Path(node.partition("::")[0]).match("test_*_serial.py")]
        serial_set = set(serial_nodes)
        if result["scheduling_mode"] == "duration":
            timings: dict[str, float] = {}
            reference = os.environ.get("MYRK_TEST_TIMING_REFERENCE")
            result["timing_reference"] = {"status": "not_requested"}
            if reference:
                try:
                    reference_path = Path(reference)
                    if not reference_path.is_absolute():
                        reference_path = source / reference_path
                    timings.update(read_timings(reference_path, project)["node_seconds"])
                    result["timing_reference"] = {"status": "loaded", "path": str(reference_path)}
                except (OSError, ValueError) as error:
                    result["timing_reference"] = {"status": "fallback", "error": str(error)}
            try:
                previous = read_timings(timing_path, project)
                if previous.get("version") != 1 or previous.get("environment") != environment:
                    raise ValueError("Timing index format or dependency environment does not match.")
                timings.update(previous["node_seconds"])
                result["timing_index"]["read_status"] = "loaded"
            except (OSError, ValueError) as error:
                result["timing_index"].update(read_status="fallback", read_error=str(error))
            known = [timings[node] for node in plan["nodeids"] if node in timings]
            estimate = sum(known) / len(known) if known else 1.0
            result["timing_estimates"] = {"known_nodes": len(known), "unknown_nodes": len(plan["nodeids"]) - len(known),
                "unknown_node_seconds": estimate}
            units: dict[tuple[str, str], list[str]] = {}
            node_units: dict[str, tuple[str, str]] = {}
            for node in plan["nodeids"]:
                if node in serial_set:
                    continue
                function, parameter, _ = node.partition("[")
                unit = (("parameter", node) if parameter and function in FORMAL_PARAMETER_FUNCTIONS
                    else ("file", node.partition("::")[0]))
                node_units[node] = unit
                units.setdefault(unit, []).append(node)
            unit_costs = {unit: sum(timings.get(node, estimate) for node in nodes) for unit, nodes in units.items()}
            assignments: dict[tuple[str, str], str] = {}
            loads = {"lane1": 0.0, "lane2": 0.0}
            for unit in sorted(units, key=lambda unit: -unit_costs[unit]):
                lane = min(loads, key=lambda lane: loads[lane])
                assignments[unit] = lane
                loads[lane] += unit_costs[unit]
            plan["lanes"] = {lane: [node for node in plan["nodeids"]
                if node not in serial_set and assignments[node_units[node]] == lane]
                for lane in loads}
            result["estimated_lane_seconds"] = loads
        else:
            plan["lanes"] = {name: [node for node in nodes if node not in serial_set]
                for name, nodes in plan["lanes"].items()}
        plan["lanes"]["serial"] = serial_nodes
        manifest = results / f"pytest-plan-{invocation_id}.json"
        manifest.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        result["manifest_path"] = str(manifest)
        result["completeness"]["expected_count"] = len(plan["nodeids"])
        result["lanes"] = {
            name: {"expected_nodeids": nodes, "receipt": None}
            for name, nodes in plan["lanes"].items()
        }
        groups.update({name: uuid4().hex for name, nodes in plan["lanes"].items() if nodes})
        launch.coverage.update(node_count=len(plan["nodeids"]),
                               selection_sha256=hashlib.sha256(json.dumps(plan["nodeids"]).encode()).hexdigest())
        launch.record["source_version"]["inputs_sha256"] = hashlib.sha256(
            json.dumps(plan["source_inputs"], sort_keys=True).encode()).hexdigest()
        launch.set_groups(groups)
        active = [name for name, nodes in plan["lanes"].items() if nodes and name != "serial"]
        result["execution_mode"] = "parallel" if len(active) > 1 else "serial"
        result["dependency_preparation"] = {"status": "not_required"}
        if (source / "desktop/package.json").is_file():
            modules = project / "desktop/node_modules"
            link = source / "desktop/node_modules"
            if (not modules.is_dir() or not modules.resolve().is_relative_to(project)
                    or any(part.is_symlink() or part.is_junction() for part in (modules, *modules.parents))):
                raise ValueError("Shared desktop dependencies must be an existing ordinary project-local directory.")
            prepared = "existing"
            if not link.exists() and not link.is_junction() and not link.is_symlink():
                creation = subprocess.run([r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "New-Item -ItemType Junction -Path $env:MYRK_DEPENDENCY_LINK -Target $env:MYRK_DEPENDENCY_TARGET -ErrorAction Stop | Out-Null"],
                    env=dict(os.environ, MYRK_DEPENDENCY_LINK=str(link), MYRK_DEPENDENCY_TARGET=str(modules)),
                    check=False, capture_output=True, text=True, encoding="utf-8")
                if creation.returncode:
                    raise ValueError("Shared desktop dependency preparation failed: " + creation.stdout + creation.stderr)
                prepared = "linked"
            if link.resolve() != modules.resolve():
                raise ValueError("The source desktop dependency entry does not resolve to the shared project dependencies.")
            result["dependency_preparation"] = {"status": prepared, "path": str(link), "target": str(modules)}
        executor = ThreadPoolExecutor(max_workers=2)
        for name in active:
            future = executor.submit(run_group, name, [*arguments, "-p", "scripts.pytest_shards",
                "--myrk-shard=" + name, "--myrk-shard-manifest=" + str(manifest)])
            dispatched.add(name)
            futures[future] = name
        for future in as_completed(futures):
            receipt = future.result()
            result["lanes"][futures[future]]["receipt"] = receipt
            if receipt["exit_code"] not in (0, 1) or not receipt["runtime_removed"]:
                stop.set()
        executor.shutdown(wait=True)
        executor = None
        if serial_nodes and not stop.is_set():
            dispatched.add("serial")
            result["lanes"]["serial"]["receipt"] = run_group("serial", [*arguments, "-p", "scripts.pytest_shards",
                "--myrk-shard=serial", "--myrk-shard-manifest=" + str(manifest)])
    except BaseException as error:
        stop.set()
        result["exit_code"] = 130 if isinstance(error, KeyboardInterrupt) else 1
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        for future, name in futures.items():
            try:
                result["lanes"][name]["receipt"] = future.result()
            except Exception as error:
                result["lanes"][name]["error"] = f"{type(error).__name__}: {error}"
                result["exit_code"] = result["exit_code"] or 1
        if plan is not None:
            reported: list[str] = []
            completed: list[str] = []
            invalid: list[dict[str, Any]] = []
            for name, lane in result["lanes"].items():
                expected = lane["expected_nodeids"]
                receipt = lane["receipt"]
                if receipt is None:
                    if expected:
                        invalid.append({"lane": name, "error": "No final lane receipt."})
                    continue
                if receipt["exit_code"]:
                    result["exit_code"] = result["exit_code"] or receipt["exit_code"]
                paths = [Path(root) / "pytest-shards/observation.json"
                    for root in receipt.get("retained_reports", {}).get("paths", [])]
                paths = [path for path in paths if path.is_file()]
                if len(paths) != 1:
                    invalid.append({"lane": name, "error": "No unique lane observation."})
                    continue
                try:
                    observation = read_observation(paths[0])
                except (OSError, ValueError) as error:
                    invalid.append({"lane": name, "error": str(error)})
                    continue
                lane["observation_path"] = str(paths[0])
                reports = observation["reports"]
                phases = Counter((item["nodeid"], item["when"]) for item in reports)
                seen = {item["nodeid"] for item in reports}
                teardown = {item["nodeid"] for item in reports if item["when"] == "teardown"}
                setup_passed = {item["nodeid"] for item in reports
                    if item["when"] == "setup" and item["outcome"] == "passed"}
                malformed = [node for node in expected if phases[node, "setup"] != 1 or phases[node, "teardown"] != 1
                    or phases[node, "call"] > 1 or (node in setup_passed and phases[node, "call"] != 1)]
                duplicates = [{"nodeid": node, "phase": phase, "count": count}
                    for (node, phase), count in phases.items() if count != 1]
                unknown = [item for item in reports if item["when"] not in {"setup", "call", "teardown"}]
                valid = (set(expected) == seen == teardown and not malformed and not duplicates and not unknown
                    and observation["selected_nodeids"] == expected
                    and observation.get("collected_nodeids") == plan["nodeids"]
                    and observation.get("source_root") == str(source)
                    and observation.get("mode") == name
                    and observation.get("session_finished") is True
                    and not observation.get("collection_errors") and not observation.get("internal_errors"))
                lane["complete"] = valid
                lane["phase_outcomes"] = dict(Counter(item["when"] + ":" + item["outcome"] for item in reports))
                if valid:
                    for item in reports:
                        observed_seconds[item["nodeid"]] = observed_seconds.get(item["nodeid"], 0.0) + item["duration"]
                if not valid:
                    invalid.append({"lane": name, "missing": sorted(set(expected) - seen),
                        "unexpected": sorted(seen - set(expected)), "malformed": malformed, "duplicate_phases": duplicates})
                reported.extend(sorted(seen))
                completed.extend(sorted(teardown))
            duplicate_nodes = sorted(node for node, count in Counter(reported).items() if count > 1)
            result["completeness"] = {
                "complete": not invalid and not duplicate_nodes and set(plan["nodeids"]) == set(reported) == set(completed),
                "expected_count": len(plan["nodeids"]), "reported_count": len(set(reported)),
                "completed_count": len(set(completed)), "duplicate_nodes": duplicate_nodes, "invalid_lanes": invalid,
            }
            try:
                result["source_inputs_unchanged"] = get_source_inputs(source) == plan["source_inputs"]
            except (OSError, ValueError) as error:
                result["source_inputs_unchanged"] = False
                result["source_input_error"] = str(error)
            if not result["completeness"]["complete"] or not result["source_inputs_unchanged"]:
                result["exit_code"] = result["exit_code"] or 1
        receipts = [result["collection_receipt"], *(lane["receipt"] for lane in result["lanes"].values())]
        result["runtime_removed"] = all(receipt["runtime_removed"] for receipt in receipts if receipt is not None)
        if not result["runtime_removed"]:
            result["exit_code"] = result["exit_code"] or 1
        launch.verify_pytest_config(result)
        full_arguments = all(argument in {"-q", "-v", "-vv", "--disable-warnings"} for argument in arguments)
        if (plan is not None and result["exit_code"] == 0 and result["completeness"]["complete"]
                and result["runtime_removed"] and result.get("source_inputs_unchanged") is True
                and full_arguments and plan.get("timing_update_eligible") is True
                and not os.environ.get("PYTEST_ADDOPTS", "").strip()):
            update_timing_index(timing_path, result_path, result["timing_index"], environment,
                                plan["source_inputs"], observed_seconds)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["wall_seconds"] = monotonic() - started
        if result["exit_code"] in (2, 130):
            state = "interrupted"
        elif "collection" not in dispatched:
            state = "startup_failed"
        else:
            state = "failed" if result["exit_code"] else "passed"
        artifacts = [Path(lane["observation_path"]) for lane in result["lanes"].values() if "observation_path" in lane]
        if "manifest_path" in result:
            artifacts.append(Path(result["manifest_path"]))
        launch.finish(result, state, not_run=sorted(groups.keys() - dispatched), required_artifacts=artifacts)
    return result
