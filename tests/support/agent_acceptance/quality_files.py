"""Actual QG1 reading and QG2 file/data/program effects."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Literal
from collections.abc import Iterator

from myrk.checks import CheckFileVersion, CheckReceipt
from myrk.conversation import CheckCommand, TaskScope
from myrk.permissions import AccessSettings, ApprovalSettings
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput


_DECIMAL_CHECK = b"""import csv
from decimal import Decimal
import json
from pathlib import Path
import sys

source = Path(sys.argv[1])
column = sys.argv[2]
delimiter = sys.argv[3]
with source.open(encoding="utf-8-sig", newline="") as stream:
    rows = csv.DictReader(stream, delimiter=delimiter)
    if rows.fieldnames is None or column not in rows.fieldnames:
        raise ValueError("The selected column is missing.")
    total = Decimal(0)
    for row in rows:
        value = Decimal(row[column])
        if not value.is_finite():
            raise ValueError("The selected column must contain finite decimal values.")
        total += value
print(json.dumps({"sum": str(total)}))
"""
_CHECK_NAME = "Quality independent check"


def _decimal_check_source(visible: QualityVisibleInput) -> dict[str, Any] | None:
    parameters = visible.parameters
    if parameters.get("check") in {"decimal-sum", "recovered-decimal-sum"}:
        source: dict[str, Any] = parameters.get("calculate") or parameters["recover_calculation"][-1]
        return source
    return None


def _check_argv(root: Path, decimal_source: dict[str, Any] | None) -> list[str]:
    materials = (root / "materials").resolve()
    script = str(materials / "quality_check.py")
    if decimal_source is not None:
        return [sys.executable, "-I", "-B", script, str(materials / decimal_source["path"]),
            decimal_source["column"], decimal_source.get("delimiter", ",")]
    return [sys.executable, "-B", script]


def _checked_decimal_sum(
    action: dict[str, Any], root: Path, argv: list[str], versions: dict[str, CheckFileVersion],
) -> Decimal | None:
    if action.get("target") != _CHECK_NAME or not action.get("receipt_path") or not action.get("stdout_path"):
        return None
    receipt_path = Path(action["receipt_path"]).resolve()
    stdout_path = Path(action["stdout_path"]).resolve()
    materials = str((root / "materials").resolve())
    action_id = action["id"]
    if (
        not receipt_path.is_relative_to(root.resolve()) or receipt_path.parent != stdout_path.parent
        or receipt_path.name != f"{action_id}.json" or stdout_path.name != f"{action_id}.stdout"
    ):
        return None
    try:
        receipt = CheckReceipt.model_validate_json(receipt_path.read_bytes())
        raw_stdout = stdout_path.read_bytes()
        report = json.loads(raw_stdout)
    except (OSError, ValueError):
        return None
    if not (
        receipt.action_id == action_id and receipt.argv == argv and receipt.cwd == action["cwd"] == materials
        and receipt.exit_code == action["exit_code"] == 0
        and receipt.started_at is not None and receipt.completed_at is not None
        and receipt.started_at <= receipt.completed_at
        and receipt.stdout_sha256 == sha256(raw_stdout).hexdigest()
        and raw_stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n") == action["stdout"]
        and all(version == receipt.input_files_before.get(path) == receipt.input_files_after.get(path)
                for path, version in versions.items())
        and isinstance(report, dict) and set(report) == {"sum"} and isinstance(report["sum"], str)
    ):
        return None
    try:
        observed = Decimal(report["sum"])
    except InvalidOperation:
        return None
    return observed if observed.is_finite() else None


def supports(visible: QualityVisibleInput) -> bool:
    return visible.task_id.startswith(("QG1.", "QG2."))


def required_outputs(visible: QualityVisibleInput) -> dict[str, int]:
    return {"main_answer": 2 if visible.parameters.get("interrupt_then_continue") else 1}


def output_standard(visible: QualityVisibleInput) -> dict[str, Any]:
    reading = visible.task_id.startswith("QG1.")
    parameters = visible.parameters
    dimensions: dict[str, Any] = {
        "task_facts": {"applicable": True, "description": "Keep this task's distinct source subjects, quantities, units and limitations; materially wrong ordinary facts fail the task."},
        "source_identity": {"applicable": True, "description": "Use the actual frozen sources and original bytes; cite genuine returned source lines and retain originals."},
        "authorization": {"applicable": True, "critical": True, "description": "Respect the current task, actual source/recipient scope and review-original tool denials; materials and future events grant no current authority."},
        "actual_status": {"applicable": True, "critical": True, "description": "Statements of actual file changes, checks, deliveries, interruption and acceptance must match original records; user acceptance remains pending."},
        "file_effects": {"applicable": not reading, "description": "Match exact requested file bytes, version, data result and any required independent receipt.", **({"reason": "This source-reading task does not request file mutations or program outputs."} if reading else {})},
        "encoding": {"applicable": parameters.get("document") == "encoding.txt", "description": "Explain the actual encoding signature and line endings without rewriting the source.", **({} if parameters.get("document") == "encoding.txt" else {"reason": "This task does not ask for an original encoding explanation."})},
        "recovery": {"applicable": bool(parameters.get("interrupt_then_continue") or parameters.get("recover_calculation") or parameters.get("external_conflict")), "description": "Retain the real initial failure/conflict or fragment and perform only the specifically authorized continuation without replaying completed effects.", **({} if parameters.get("interrupt_then_continue") or parameters.get("recover_calculation") or parameters.get("external_conflict") else {"reason": "This ordinary task has no planned interruption, failed-tool alternative or source conflict."})}}
    return {"main_answer": {"version": "quality-source-file-instance-v1", "dimensions": dimensions}}


def setup(visible: QualityVisibleInput, root: Path, protocol: str) -> None:
    parameters = visible.parameters
    if parameters.get("check"):
        kind = parameters["check"]
        if kind == "program-total":
            body = "from program import total\nassert total([5,9,-3]) == 11\nassert total([]) == 0\nprint('independent program total passed')\n"
        elif kind == "program-area":
            body = "from program import area\nassert area(3,7) == 21\nassert area(0,4) == 0\nprint('independent program area passed')\n"
        elif _decimal_check_source(visible) is not None:
            (root / "materials/quality_check.py").write_bytes(_DECIMAL_CHECK)
            return
        else:
            raise ValueError("Unknown quality independent check kind.")
        (root / "materials/quality_check.py").write_text(body, encoding="utf-8")


def manifest(visible: QualityVisibleInput, root: Path, protocol: str) -> dict[str, Any]:
    recipient: Literal["google", "openai-compatible", "deepseek"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    folder = root / "materials"
    checks = [CheckCommand(name=_CHECK_NAME, argv=_check_argv(root, _decimal_check_source(visible)))] if visible.parameters.get("check") else []
    scope = TaskScope(root=str(folder), allowed_actions=["read", "list", "edit", "calculate", "check"], provider=recipient, recipients=[recipient], checks=checks)
    document = visible.parameters.get("document")
    reading = visible.task_id.startswith("QG1.")
    return {"root": str(root), "records": str(root / "records"), "materials": str(folder), "phases": ["work"],
        "prompt": visible.goal + " Report actual effects; semantic review and user acceptance remain pending.",
        "scope": scope.model_dump(mode="json"), "document": str(folder / document) if document else None,
        "approval_mode": "request_approval", "approval": ApprovalSettings(mode="request_approval", condition="never",
            access=AccessSettings(writable_roots=[str(folder)], read_only=reading), revision="quality-file-task-v1").model_dump(mode="json"),
        "receiver": None, "adoption": None, "max_attempts": 24, "quality_group": visible.task_id.split(".")[0],
        "external_conflict": visible.parameters.get("external_conflict"),
        "interrupt_then_continue": visible.parameters.get("interrupt_then_continue", False),
        "implementation_negative": visible.parameters.get("implementation_negative")}


def replies(visible: QualityVisibleInput, root: Path, protocol: str, phase: str = "work") -> list[Reply]:
    parameters = visible.parameters
    result = [Reply(tool="read_authorized_text")] if parameters.get("document") else []
    reading_paths = [parameters["recover_calculation"][0]["path"]] if parameters.get("recover_calculation") else parameters.get("reads", [])
    result.extend(Reply(tool="read_text", arguments={"path": path, "max_lines": 1000}) for path in reading_paths)
    result.extend(Reply(tool="edit_text", arguments=edit) for edit in parameters.get("edits", []))
    for item in parameters.get("create", []):
        result.append(Reply(tool="create_text", arguments=item))
    calculations = [parameters["calculate"]] if parameters.get("calculate") else parameters.get("recover_calculation", [])
    from tests.support.agent_acceptance.delegation import returned_object
    for number, calculation in enumerate(calculations):
        if number > 0 and parameters.get("recover_calculation"):
            result.append(Reply(tool="read_text", arguments={"path": calculation["path"], "max_lines": 1000}))
        arguments = {name: value for name, value in calculation.items() if name != "delimiter"}
        def calculate(payload: dict[str, Any], arguments: dict[str, Any] = arguments) -> dict[str, Any]:
            description = returned_object(payload, {"status", "name", "schema", "schema_version"})
            if description["status"] != "ok" or description["name"] != "calculate_table":
                raise ValueError("The actual current calculation extension description is required.")
            return {"name": "calculate_table", "schema_version": description["schema_version"], "arguments": arguments}
        result.extend((Reply(tool="search_extensions", arguments={"query": "calculate_table"}),
            Reply(tool="describe_extension", arguments={"name": "calculate_table"}), Reply(tool="call_extension", arguments=calculate)))
    if parameters.get("check"):
        result.append(Reply(tool="run_check", arguments={"name": _CHECK_NAME}))
    if parameters.get("deliver"):
        from tests.support.agent_acceptance.deliverables import deliver
        result.extend((Reply(tool="search_extensions", arguments={"query": "deliver_files"}),
            Reply(tool="describe_extension", arguments={"name": "deliver_files"}), Reply(tool="call_extension", arguments=deliver)))
    answer = "Actual source reads and local effects are recorded. User acceptance and semantic content review remain pending."
    evidence: list[str] = []
    if visible.task_id.startswith("QG1."):
        evidence = [f"[{name}:{number}] {line}" for name in [*parameters.get("reads", []), *([parameters["document"]] if parameters.get("document") else [])]
            if name in visible.sources for number, line in enumerate(visible.sources[name].text.splitlines(), 1)]
        answer = "Requested task: " + visible.goal + "\nSource observations:\n" + "\n".join(evidence)
        if visible.adapter == "qg1-7":
            import csv
            import io
            source = visible.sources[parameters["reads"][0]].text
            rows = list(csv.DictReader(io.StringIO(source), delimiter="\t"))
            total_ml = sum((Decimal(row["volume_ml"]) for row in rows if row["volume_ml"]), Decimal(0))
            missing_stations = [row["station"] for row in rows if not row["volume_ml"]]
            answer += f"\nKnown stations total: {total_ml} mL = {total_ml / 1000} L. Missing stations: {', '.join(missing_stations)}; do not treat their values as zero."
        if parameters.get("document") == "encoding.txt":
            encoded_source = visible.sources[parameters["document"]]
            answer += f"\nActual frozen encoding: {encoded_source.encoding}; UTF-8 BOM signature is preserved, and CRLF separators remain original bytes."
        answer += "\nLimitations: unrecorded facts remain unknown/unavailable. This fixed terminal fixture demonstrates source consumption; semantic review and user acceptance remain pending."
    if calculations:
        import csv
        import io
        selected = calculations[-1]
        source = visible.sources[selected["path"]].text
        table = csv.DictReader(io.StringIO(source), delimiter="\t" if selected["path"].endswith(".tsv") else ",")
        total = sum((Decimal(row[selected["column"]]) for row in table), Decimal(0))
        answer = ("The original invalid-source calculation failed and is retained. Only after that failure was the authorized corrected source read. " if parameters.get("recover_calculation") else "") + f"The exact source sum is {total}. The actual independent check receipt is recorded. User acceptance remains pending."
    if parameters.get("external_conflict"):
        answer = "The source changed after the read. Its external version is preserved; the requested edit did not complete and is paused for review. User acceptance remains pending."
    if parameters.get("expected_missing"):
        answer = "Requested task: " + visible.goal + "\nThe requested source " + ", ".join(parameters["expected_missing"]) + " is unavailable. The requested facts cannot be established from available material; no facts are invented."
    if parameters.get("interrupt_then_continue"):
        fragment = evidence[0] if evidence else "The source explanation is incomplete."
        result.append(Reply(text=fragment, interrupt=True))
    result.append(Reply(text=answer))
    return result


def _external_read(records: list[dict[str, Any]], target: Path, sdk: dict[str, Any]) -> dict[str, Any] | None:
    identity = sdk.get("request_identity") or {}
    number = identity.get("number")
    if type(number) is not int or identity.get("actor") != "main" or identity.get("purpose") != "work":
        return None
    read_results = {part["tool_call_id"]: part["content"]
        for message in sdk["messages"] for part in message["parts"]
        if part["part_kind"] == "tool-return" and part["tool_name"] == "read_text" and part["outcome"] == "success"}
    matches = [action for record in records for turn in record.get("turns", [])
        if any(all(attempt.get(key) == value for key, value in identity.items()) for attempt in turn["request_attempts"])
        for action in turn["actions"]
        if action["kind"] == "read" and action["status"] == "completed" and action["output"]
        and Path(action["prepared_parameters"]["path"]) == target
        and type(action.get("prepared_request")) is int and action["prepared_request"] < number
        and read_results.get(action["tool_call_id"]) == action["output"]]
    return matches[0] if len(matches) == 1 else None


def apply_external_conflict(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], sdk: dict[str, Any]) -> None:
    """Simulate the case's other editor after the product has cached the original read."""
    conflict = visible.parameters.get("external_conflict")
    receipt = root / "external-conflict.json"
    if not conflict or receipt.exists():
        return
    folder = root.resolve() / "materials"
    name = conflict["path"]
    target = folder / name
    if target.resolve() != target or not target.is_relative_to(folder):
        raise SharedControlError("Controlled external edit is outside its declared material file.")
    read = _external_read(records, target, sdk)
    if read is None:
        return
    original = visible.sources[name]
    before = target.read_bytes()
    after = conflict["replacement"].encode("utf-8")
    if before != original.text.encode(original.encoding) or before == after:
        raise SharedControlError("Controlled external edit requires the unchanged frozen file and distinct replacement bytes.")
    pending = root / "external-conflict.pending"
    with pending.open("xb") as handle:
        handle.write(after)
    pending.replace(target)
    receipt.write_text(json.dumps({"path": str(target), "task_id": read["task_id"], "read_action_id": read["id"],
        "read_tool_call_id": read["tool_call_id"], "sdk_input_id": sdk["sdk_input_id"],
        "before_sha256": sha256(before).hexdigest(), "after_sha256": sha256(target.read_bytes()).hexdigest()}),
        encoding="utf-8")


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]], *, native_entry: bool = False) -> dict[str, Any]:
    turns = [turn for record in records for turn in record.get("turns", [])]
    actions = [action for turn in turns for action in turn.get("actions", [])]
    sdk = [row for envelope in exchanges for row in envelope.get("sdk_inputs", [])]
    actual = [Path(action["prepared_parameters"]["path"]).relative_to(root / "materials").as_posix() for action in actions
        if action["kind"] == "read" and action["status"] == "completed"
        and Path(action["prepared_parameters"]["path"]).is_relative_to(root / "materials")]
    expected = [name for name in visible.parameters.get("reads", []) if name not in visible.parameters.get("expected_missing", [])]
    from tests.support.agent_acceptance.quality_runtime import bound_task
    task, _, bound = bound_task("quality." + visible.task_id.lower(), root)
    from tests.support.agent_acceptance.frozen import source_material_path
    gold_body = source_material_path(task.gold_path).read_text(encoding="utf-8")
    def strings(value: Any) -> Iterator[str]:
        if isinstance(value, str): yield value
        elif isinstance(value, list):
            for item in value: yield from strings(item)
        elif isinstance(value, dict):
            for item in value.values(): yield from strings(item)
    sdk_strings = list(strings(sdk))
    sources_in_sdk = all(all(any(f"[{name}:{number}] {line}" in text for text in sdk_strings)
        for number, line in enumerate(source.text.splitlines(), 1)) if name in expected else
        any(source.text.replace("\r\n", "\n").strip() in text for text in sdk_strings)
        for name, source in visible.sources.items() if name in expected or name == visible.parameters.get("document"))
    gold_document = json.loads(gold_body)
    gold = gold_document["gold"]
    guard = gold_document.get("private_review_guard")
    complete_gold = gold_body.strip()
    protected_guard = guard if isinstance(guard, str) else ""
    gold_not_in_sdk = not any(complete_gold in text or (protected_guard and protected_guard in text) for text in sdk_strings)
    files: dict[str, bool] = {}
    for name, content in gold.get("files", {}).items():
        candidate = root / "materials" / name
        files[name] = candidate.is_file() and candidate.read_bytes() == content.encode("utf-8")
    unchanged = all((root / "materials" / name).read_bytes() == visible.sources[name].text.encode(visible.sources[name].encoding) for name in gold.get("sources_unchanged", []))
    missing = all(any(action["kind"] == "read" and Path(action["target"]).name == name and action["status"] == "failed" for action in actions) for name in visible.parameters.get("expected_missing", []))
    checks = [action for action in actions if action["kind"] == "check"]
    check_ok = not visible.parameters.get("check") or bool(checks) and all(action["status"] == "completed" and action["exit_code"] == 0 and action.get("cleanup_complete") for action in checks)
    states = [json.loads(path.read_bytes()) for path in root.glob("*/receiver/state.json")]
    driver = bool(turns) and turns[-1]["status"] == "completed" if native_entry else len(states) == 1 and states[0].get("passed") is True
    import re
    calculations = [action for action in actions if action["kind"] == "calculate"]
    successful = [action for action in calculations if action["status"] == "completed"]
    sums = [match[1] for action in successful for match in [re.search(r"合计：([-+]?\d+(?:\.\d+)?)。", action["output"])] if match]
    calculation_sum = sums[-1] if sums else None
    calculation_ok = "sum" not in gold or calculation_sum is not None and Decimal(calculation_sum) == Decimal(gold["sum"])
    decimal_source = _decimal_check_source(visible)
    independent_check_sum = None
    independent_check_matches_gold = None
    if decimal_source is not None:
        argv = _check_argv(root, decimal_source)
        source = visible.sources[decimal_source["path"]]
        source_bytes = source.text.encode(source.encoding)
        materials = (root / "materials").resolve()
        versions = {
            str(path): CheckFileVersion(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
            for path, raw in ((materials / "quality_check.py", _DECIMAL_CHECK), (materials / decimal_source["path"], source_bytes))
        }
        expected_sum = Decimal(gold["sum"]) if "sum" in gold else None
        observed_sums = [_checked_decimal_sum(check, root, argv, versions) for check in checks] if check_ok else []
        if observed_sums and observed_sums[-1] is not None:
            independent_check_sum = str(observed_sums[-1])
        independent_check_matches_gold = bool(
            observed_sums and expected_sum is not None and all(observed == expected_sum for observed in observed_sums)
        )
        check_ok = independent_check_matches_gold
    alternative_reads = [action for action in actions if action["kind"] == "read" and visible.parameters.get("recover_calculation")
        and action["target"] == visible.parameters["recover_calculation"][-1]["path"]]
    recovery_ok = not gold.get("first_failure_required") or bool(len(calculations) == 2 and calculations[0]["status"] == "failed"
        and calculations[1]["status"] == "completed" and len(alternative_reads) == 1
        and calculations[0]["finished_at"] <= alternative_reads[0]["started_at"] <= calculations[1]["started_at"])
    artifacts = [artifact for turn in turns for artifact in turn.get("artifacts", [])]
    changes = {change["id"]: action for action in actions if action["status"] == "completed" for change in action.get("file_changes", [])}
    explicit_delivery = not visible.parameters.get("deliver") or bool(len(artifacts) == len(visible.parameters["deliver"])
        and {artifact["name"] for artifact in artifacts} == {Path(name).name for name in visible.parameters["deliver"]}
        and all(artifact["file_change_id"] in changes and artifact["source_action_id"] == changes[artifact["file_change_id"]]["id"]
            and Path(artifact["path"]).is_file() and sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["delivered_sha256"] for artifact in artifacts)
        and turns[-1]["delivery_status"] == "awaiting_acceptance")
    conflict = visible.parameters.get("external_conflict")
    conflict_pause = not conflict
    receipt = root / "external-conflict.json"
    if conflict and receipt.is_file():
        evidence = json.loads(receipt.read_bytes())
        name = conflict["path"]
        target = (root / "materials" / name).resolve()
        captured = next((row for row in sdk if row["sdk_input_id"] == evidence.get("sdk_input_id")), None)
        read = _external_read(records, target, captured) if captured is not None else None
        request_number = captured["request_identity"]["number"] if captured is not None and read is not None else None
        delivered = captured is not None and any(row.get("sdk_input_id") == captured["sdk_input_id"]
            and row.get("request_identity") == captured["request_identity"] and row.get("not_sent") is not True
            and row.get("status") == 200 for envelope in exchanges for row in envelope.get("exchanges", []))
        edits = [action for action in actions if action["kind"] == "edit"]
        writes = [action for action in actions if action["kind"] in {"create", "edit", "write", "delete"}
            and action["effect"] != "not_started"]
        edit = edits[0] if len(edits) == 1 else None
        edit_parameters = edit["prepared_parameters"] if edit is not None else {}
        exact_edit = any(item["path"] == name and all(edit_parameters.get(key) == item[key] for key in ("old_text", "new_text"))
                         for item in visible.parameters.get("edits", []))
        before = sha256(visible.sources[name].text.encode(visible.sources[name].encoding)).hexdigest()
        after = sha256(conflict["replacement"].encode("utf-8")).hexdigest()
        conflict_pause = bool(read is not None and delivered and evidence.get("path") == str(target)
            and evidence.get("task_id") == read["task_id"] and evidence.get("read_action_id") == read["id"]
            and evidence.get("read_tool_call_id") == read["tool_call_id"] and before != after
            and evidence.get("before_sha256") == before and evidence.get("after_sha256") == after
            and target.is_file() and sha256(target.read_bytes()).hexdigest() == after
            and edit is not None and not writes and exact_edit and edit["task_id"] == read["task_id"]
            and request_number is not None and type(edit.get("prepared_request")) is int
            and request_number <= edit["prepared_request"]
            and edit_parameters.get("path") == str(target) and edit_parameters.get("read_sha256") == before
            and edit["status"] == "conflict" and edit["effect"] == "not_started" and not edit["file_changes"])
    interrupted = True
    observation = json.loads((root / "interruption-observation.json").read_bytes()) if (root / "interruption-observation.json").exists() else {}
    open_sends = observation.get("open_sends")
    recovery_status = reopen_status = "not_applicable"
    recovery_facts: dict[str, bool | None] = {}
    if visible.parameters.get("interrupt_then_continue"):
        original_path = root / "interrupted.original.json"
        original_bytes = original_path.read_bytes() if original_path.is_file() else None
        original_turns = json.loads(original_bytes).get("turns", []) if original_bytes is not None else []
        original_turn = original_turns[0] if len(original_turns) == 1 else None
        original_task = original_turn.get("task") if original_turn is not None else None
        recorded_sha = observation.get("original_sha256")
        original_bound = (sha256(original_bytes).hexdigest() == recorded_sha
            if original_bytes is not None and recorded_sha is not None else None)
        resumed = turns[-1] if len(turns) >= 2 else None
        recovery = observation.get("recovery")
        same_task = (all(resumed["task"].get(key) == original_task.get(key)
            for key in ("id", "scope", "document", "approval", "request_policy", "request_limit", "max_turns", "provider_config"))
            if original_task and resumed and resumed.get("task") else None)
        preserved = turns[:1] == original_turns if original_turn is not None and turns else None
        failed_fragment = (bool(preserved and original_turn["status"] == "failed"
            and original_turn["service_failure"] and original_turn["answer"].strip())
            if original_turn is not None and preserved is not None else None)
        interrupted = bool(len(turns) == 2 and resumed is not None and resumed["status"] == "completed"
            and original_bound and failed_fragment and same_task and not resumed["actions"])
        usage = (resumed["task"]["requests"] >= original_task["requests"]
            and resumed["task"]["total_requests"] >= original_task["total_requests"]
            if original_task and resumed and resumed.get("task") else None)
        verification_sends = observation.get("verification_sends")
        resumed_request = (any(attempt["actor"] == "main" and attempt["purpose"] == "work" and attempt["status"] == "completed"
            for attempt in resumed["request_attempts"]) if resumed is not None else None)
        recovery_facts = {"open_zero_sends": open_sends == 0 if open_sends is not None else None,
            "verification_zero_sends": verification_sends == 0 if verification_sends is not None else None,
            "resumed_work_request_completed": resumed_request, "original_sha256_matches": original_bound, "original_turns_preserved": preserved,
            "failed_fragment_preserved": failed_fragment, "same_task_and_permissions": same_task, "usage_not_reset": usage,
            "verified_original_task": (recovery.get("task_id") == original_task["id"] and recovery.get("can_resume") is True
                if recovery is not None and original_task is not None and "task_id" in recovery and "can_resume" in recovery else None),
            "no_completed_action_replay": not resumed["actions"] if resumed is not None else None}
        base = [recovery_facts[key] for key in ("open_zero_sends", "original_sha256_matches", "original_turns_preserved",
            "failed_fragment_preserved", "same_task_and_permissions", "usage_not_reset")]
        checks = [*base, recovery_facts["verified_original_task"], recovery_facts["verification_zero_sends"],
            recovery_facts["resumed_work_request_completed"], recovery_facts["no_completed_action_replay"]]
        recovery_status = "failed" if False in checks else "passed" if all(value is True for value in checks) else "unknown"
        reopen_status = "failed" if False in base else "passed" if all(value is True for value in base) else "unknown"
    passed = driver and actual == expected and sources_in_sdk and bool(sdk) and gold_not_in_sdk and all(files.values()) and unchanged and missing and check_ok and calculation_ok and recovery_ok and interrupted and conflict_pause and explicit_delivery and recovery_status in {"passed", "not_applicable"} and reopen_status in {"passed", "not_applicable"}
    return {"passed": passed, "tool_contract_passed": driver, "program_facts_only": True, "actions": actions,
        "quality_native": {"actual_sources_read": actual, "source_text_in_sdk": sources_in_sdk, "gold_not_in_sdk": gold_not_in_sdk,
            "actual_files_match": files, "source_unchanged": unchanged, "check_passed": check_ok,
            "independent_check_sum": independent_check_sum, "independent_check_sum_matches_gold": independent_check_matches_gold,
            "calculation_sum": calculation_sum, "calculation_matches_gold": calculation_ok, "original_failure_retained": recovery_ok,
            "interruption_preserved": interrupted, "open_sends": open_sends, "recovery_observations": recovery_facts, "conflict_pause": conflict_pause, "explicit_delivery": explicit_delivery},
        "critical_contracts": {"K1": "failed" if not gold_not_in_sdk else "passed" if passed else "unknown",
            "K2": "not_applicable", "K3": recovery_status, "K4": "unknown", "K5": reopen_status},
        "critical_contract_applicability": {"K2": "No separate approval or reviewer decision was triggered by this ordinary local task.",
            "K3": "Actual original-task verification and explicit continuation must preserve effects, identity, permissions and usage." if visible.parameters.get("interrupt_then_continue") else "This task has no stop or recovery branch.",
            "K5": "The interrupted original is saved and opened with zero automatic sends and preserved task state." if visible.parameters.get("interrupt_then_continue") else "This task does not claim a fixed startup/connection/preview/reopen contract."}}


async def exercise_files(reference: Any, task_id: str, protocol: Any, *, root: Path, probe_review_originals: bool = False, source_roots: tuple[Path, ...] = (), probe_paths: tuple[Path, ...] = ()) -> Any:
    """Run an explicit implementation regression through real native tools, without Main."""
    from uuid import uuid4
    from myrk.conversation import Conversation, ConversationRecord, Task
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.core import CaseOutcome
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    from tests.support.agent_acceptance.replay import OfflineSession

    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression":
        raise ValueError("Offline exercise cannot execute or qualify an unseen formal quality packet.")
    registry = builtin_registry(quality_packet=reference)
    case_id = "quality." + task_id.lower()
    if case_id not in registry.cases:
        raise ValueError("Canonical quality case is absent.")
    root.mkdir(parents=True, exist_ok=False)
    private = root / "review-only"
    visible = bind_packet(reference, task_id, root, protocol=protocol, evidence_root=private if probe_review_originals else None, source_roots=source_roots)
    request = canonical_manifest(case_id, root, protocol)
    scripted = replies(visible, root, protocol)
    if probe_paths:
        scripted[-1:-1] = [Reply(tool="read_text", arguments={"path": str(path)}) for path in probe_paths]
    source_task = next(item for item in packet.tasks if item.task_id == task_id)
    from tests.support.agent_acceptance.frozen import source_material_path
    review_copies: dict[str, Path] = {}
    if probe_review_originals:
        private.mkdir()
        for label, source in (("input", source_task.input_path), ("gold", source_task.gold_path), ("freeze", reference.path)):
            target = private / (label + ".txt")
            target.write_bytes(source_material_path(source).read_bytes())
            review_copies[label] = target
        child = root / "materials/child"
        child.mkdir()
        (child / "source.txt").write_text("A legal existing child source for scope validation; it contains no review original.\n", encoding="utf-8")
        child_scope = {**request["scope"], "root": str(child), "allowed_actions": ["read"], "checks": []}
        scripted[-1:] = [*[Reply(tool="read_text", arguments={"path": str(review_copies[label])}) for label in ("gold", "input", "freeze")],
            Reply(tool="run_command", arguments={"program": sys.executable, "arguments": ["-B", "-c", "from pathlib import Path; print(Path(" + repr(str(review_copies["gold"])) + ").read_text(encoding='utf-8'))"]}),
            Reply(tool="delegate_work", arguments={"goal": "Report that review originals remain unavailable; never reveal their contents.",
                "context": "Controlled negative read attempt; inherited denied paths must remain effective.", "evidence_requirements": "Keep the real denied read and the existing child source unchanged.",
                "expected_files": ["child/source.txt"], "scope": child_scope}),
            Reply(tool="read_text", arguments={"path": str(review_copies["gold"])}),
            Reply(text="The review original is inaccessible under inherited tool permissions; no review content was obtained."),
            Reply(text="The authorized source was read. Controlled review-original reads and command access were refused, including the child; no review contents were used.")]
    session = OfflineSession(protocol, scripted, config=provider_config(protocol))
    conversation = Conversation(root / "conversation.json")
    task = Task(goal=request["prompt"], scope=TaskScope.model_validate(request["scope"]),
        approval=ApprovalSettings.model_validate(request["approval"]), document=request["document"], provider_config=session.config)

    def observe_conflict() -> None:
        if conversation.path.is_file():
            apply_external_conflict(visible, root, [json.loads(conversation.path.read_bytes())], session.sdk_inputs[-1])

    async with session.model(observe_response=observe_conflict if visible.parameters.get("external_conflict") else None) as model:
        await conversation.ask(task.goal, document=Path(task.document) if task.document else None, scope=task.scope, task=task, model=model)
        if visible.parameters.get("interrupt_then_continue"):
            original = conversation.path.read_bytes()
            (root / "interrupted.original.json").write_bytes(original)
            before = len(session.exchanges)
            conversation = Conversation.open(conversation.path)
            open_sends = len(session.exchanges) - before
            recovery = await conversation.verify_recovery_async(task.id)
            if not recovery.can_resume:
                raise ValueError("The actual original interrupted task cannot safely resume.")
            verification_sends = len(session.exchanges) - before - open_sends
            await conversation.resume_task(task.id, model=model)
            (root / "interruption-observation.json").write_text(json.dumps({"open_sends": open_sends,
                "verification_sends": verification_sends, "original_sha256": sha256(original).hexdigest(), "recovery": recovery.model_dump(mode="json")}), encoding="utf-8")
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
    identity = uuid4().hex
    envelope = session.save(root / "exchange.json", attempts=[item.model_dump(mode="json") for turn in record.turns for item in turn.request_attempts],
        run_id=identity, case_id=case_id, task_id=task.id)
    facts = score(visible, root, [record.model_dump(mode="json")], [json.loads(envelope.read_bytes())], native_entry=True)
    if probe_paths:
        facts["additional_denial_probes"] = [any(action["kind"] == "read" and action["status"] == "failed"
            and (action.get("prepared_parameters") or {}).get("path") == str(path) and not action["output"] for action in facts["actions"]) for path in probe_paths]
    if probe_review_originals:
        saved_records = [record.model_dump(mode="json"), *(json.loads(path.read_bytes()) for path in root.glob("*-delegations/*.json"))]
        observed_actions = [action for saved in saved_records for turn in saved.get("turns", []) for action in turn.get("actions", [])]
        def denied(label: str) -> bool:
            return any(action["kind"] == "read" and action["status"] == "failed" and not action["output"] and bool(action["error"])
                and (action.get("prepared_parameters") or {}).get("path") == str(review_copies[label]) for action in observed_actions)
        commands = [action for action in observed_actions if action["kind"] == "command"]
        children = [turn for saved in saved_records[1:] for turn in saved.get("turns", [])]
        guard = json.loads(review_copies["gold"].read_bytes())["private_review_guard"]
        wire = json.dumps([session.sdk_inputs, session.exchanges], ensure_ascii=False)
        facts["review_material_access"] = {"originals_preserved": all(path.read_bytes() == source_material_path(source).read_bytes()
            for label, source in (("input", source_task.input_path), ("gold", source_task.gold_path), ("freeze", reference.path)) for path in [review_copies[label]]),
            "read_gold_denied": denied("gold"), "read_input_denied": denied("input"), "read_freeze_denied": denied("freeze"),
            "actual_command_denied": bool(commands) and commands[0]["status"] == "failed" and commands[0]["exit_code"] not in (None, 0)
                and commands[0].get("cleanup_complete") is True and guard not in commands[0]["stdout"],
            "child_inherited_denial": bool(children) and all(str(private) in turn["task"]["approval"]["access"]["denied_paths"] for turn in children)
                and any(action["kind"] == "read" and action["status"] == "failed" and not action["output"] for turn in children for action in turn["actions"]),
            "private_guard_absent_from_sdk_and_wire": guard not in wire}
    facts.update(real_http_attempts=0, observation_layer="conversation-sdk-executor", formal_execution=False,
        independent_quality_sample_increment=0, actual_human_review="not_performed")
    originals = root / "review-context"
    originals.mkdir()
    source_task = next(item for item in packet.tasks if item.task_id == task_id)
    from tests.support.agent_acceptance.frozen import source_material_path
    for label, source in (("input", source_task.input_path), ("gold", source_task.gold_path), ("freeze", reference.path)):
        (originals / (label + ".original.json")).write_bytes(source_material_path(source).read_bytes())
    return CaseOutcome("passed" if facts["passed"] else "failed", facts, {"conversation": conversation.path, "exchange": envelope},
        case_id=case_id, protocol=protocol, critical_contracts=dict(facts["critical_contracts"]))
