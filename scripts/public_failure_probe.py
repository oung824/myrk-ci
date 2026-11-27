"""Add bounded diagnostics for four public failure reproductions."""

import json
import os
from pathlib import Path

import pytest


ACTION_STATUSES = {
    "awaiting_approval", "cancelled", "completed", "conflict", "denied", "failed", "started",
}
RECORD_LIMIT = 1024 * 1024


def action_summary(values):
    return {
        "summary_truncation_count": sum("目标截短" in line for line in values["lines"]),
        "summary_delete_target_over_120": len(values["deletion"].target) > 120,
    }


def read_denied(values):
    case = values["case"]
    facts = case.facts
    actions = facts.get("actions", [])
    action = actions[0] if len(actions) == 1 else {}
    status = action.get("status")
    error = action.get("error") or ""
    run = Path(os.environ["MYRK_CHECK_RUN_DIRECTORY"]).resolve()
    exchange_path = case.evidence.get("exchange")
    exchange = read_owned_json(exchange_path, run) if exchange_path is not None else None
    return {
        "read_denied_turn_completed": facts.get("turn_status") == "completed",
        "read_denied_action_status": status if status in ACTION_STATUSES else "unknown",
        "read_denied_source_unchanged": facts.get("file_bytes") == b"prepared-original".hex(),
        "read_denied_canary_in_action_output": "prepared-original" in (action.get("output") or ""),
        "read_denied_sdk_evidence_available": exchange is not None and isinstance(exchange.get("sdk_inputs"), list),
        "read_denied_canary_in_sdk": exchange is not None and "prepared-original" in json.dumps(exchange.get("sdk_inputs", [])),
        "read_denied_os_error_5": "(os error 5)" in error or "[WinError 5]" in error,
        "read_denied_zh_message": "拒绝访问" in error,
        "read_denied_en_message": "access is denied" in error.casefold(),
        "read_denied_executor_reply": "执行程序拒绝或未完成动作：" in error,
        "read_denied_scope_self_check": (
            "系统访问范围自检" in error or "没有确认所需电脑系统限制能力" in error
        ),
    }


def read_owned_json(path, root):
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            return None
        with resolved.open("rb") as source:
            raw = source.read(RECORD_LIMIT + 1)
        if len(raw) <= RECORD_LIMIT:
            record = json.loads(raw)
            return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None
    return None


def recovery(values):
    fixture = values["tmp_path"].resolve()
    records = fixture / "records"
    check_pid = (fixture / "check.pid").is_file()
    expected_marker = (fixture / "interrupted.pid").is_file() if values.get("point") == "after_check" else check_pid
    result = {
        "recovery_marker_present": expected_marker,
        "recovery_check_pid_present": check_pid,
        "recovery_script_effect_present": (fixture / "runs.txt").is_file(),
        "recovery_service_pid_present": (records / "service.pid").is_file(),
        "recovery_edit_applied": False,
        "recovery_record_available": False,
        "recovery_check_started": False,
        "recovery_check_result": False,
        "recovery_check_receipt_exists": False,
        "recovery_check_started_file_exists": False,
    }
    try:
        note = (fixture / "notes.txt").resolve()
        if note.is_relative_to(fixture):
            with note.open("rb") as source:
                result["recovery_edit_applied"] = source.read(32) in {"苹果 5\n".encode(), "苹果 5\r\n".encode()}
    except OSError:
        pass
    saved_path = values["started"].get("path")
    if not isinstance(saved_path, str):
        return result
    path = Path(saved_path)
    if not path.is_absolute() or path.suffix != ".json":
        return result
    for record_path in (path, path.with_suffix(".json.tmp")):
        record = read_owned_json(record_path, records)
        if record is None:
            continue
        result["recovery_record_available"] = True
        for entry in record.get("action_evidence", ()):
            action = entry["action"]
            if action["kind"] != "check":
                continue
            result["recovery_check_started"] |= entry["phase"] == "started"
            result["recovery_check_result"] |= entry["phase"] == "result"
            if not action.get("receipt_path"):
                continue
            receipt = Path(action["receipt_path"])
            try:
                resolved = receipt.resolve()
                started = receipt.with_suffix(".started").resolve()
                if resolved.is_relative_to(records) and started.is_relative_to(records):
                    result["recovery_check_receipt_exists"] |= resolved.is_file()
                    result["recovery_check_started_file_exists"] |= started.is_file()
            except OSError:
                continue
    return result


PROBES = {
    "tests/acceptance/test_actions.py::test_local_effect_branches_keep_real_results[read-denied]": (51, read_denied),
    "tests/test_action_reporting_facts.py::test_bounded_action_summary_keeps_preparation_read_when_deletion_is_declined": (
        404, action_summary,
    ),
    "tests/test_recovery.py::test_interrupted_real_edit_is_verified_then_resumed_without_repeating[after_check]": (
        136, recovery,
    ),
    "tests/test_recovery.py::test_running_check_is_observed_without_retry_until_its_receipt_exists": (218, recovery),
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.when != "call" or not report.failed or call.excinfo is None or not call.excinfo.errisinstance(AssertionError):
        return
    probe = PROBES.get(item.nodeid)
    if probe is None:
        return
    line, extract = probe
    function = item.nodeid.partition("::")[2].partition("[")[0]
    for entry in reversed(call.excinfo.traceback):
        if entry.name == function and entry.lineno + 1 == line:
            item.user_properties.extend(extract(entry.locals).items())
            return
