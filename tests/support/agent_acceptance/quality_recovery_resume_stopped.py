"""Observe stopped recovery, preserving the original and its legacy projection."""

from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from tests.support.agent_acceptance.core import CaseContext, CaseOutcome
from tests.support.agent_acceptance.quality_recovery_resume_inputs import _CASE_REQUEST_CAP, replies
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


async def hold_stop_checkpoint(root: Path, records: Path, sdk_input_id: str) -> None:
    marker = root / "quality-stop-arrived.json"
    if marker.exists():
        return
    saved_turns = [turn for path in records.glob("*.json") for turn in json.loads(path.read_bytes()).get("turns", [])]
    read_completed = any(action["kind"] == "read" and action["status"] == "completed"
                         for turn in saved_turns for action in turn["actions"])
    if not read_completed:
        return
    pending = marker.with_suffix(".tmp")
    pending.write_text(json.dumps({"sdk_input_id": sdk_input_id, "origin": "terminal_response_after_completed_read"}), encoding="utf-8")
    pending.replace(marker)
    async with asyncio.timeout(30):
        while not (root / "quality-stop-release.txt").is_file():
            await asyncio.sleep(.01)


def score_stopped(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    original = json.loads((root / "original-before.json").read_bytes())
    legacy_path = root / "legacy-source-v5.json"
    legacy = legacy_path.read_bytes()
    observation = json.loads((root / "recovery-observation.json").read_bytes())
    initial = original["turns"][-1]
    imported = json.loads(Path(observation["imported_path"]).read_bytes())["turns"][-1]
    old, new = initial["task"], imported["task"]
    stop_input = observation.get("stop_sdk_input_id")
    captured = [envelope["exchanges"] for envelope in exchanges]
    original_capture = [rows for rows in captured if stop_input and any(row.get("sdk_input_id") == stop_input for row in rows)]
    original_count = None
    reopened_count = None
    stopped_at_boundary = False
    if len(captured) == 2 and len(original_capture) == 1:
        [work] = original_capture
        [reopen] = [rows for rows in captured if rows is not work]
        sent = [row for row in work if row.get("not_sent") is not True]
        original_count, reopened_count = len(sent), len(reopen)
        stopped_at_boundary = bool(sent and len(work) == len(sent) and sent[-1].get("sdk_input_id") == stop_input)
    accepted_stop = stopped_at_boundary and observation.get("stop_accepted_task_id") == old["id"]
    inactive_after_reopen = reopened_count == 0 and observation["reopened_http_attempts"] == 0
    stopped = initial["status"] == imported["status"] == "cancelled" and not imported.get("service_failure")
    legacy_limit = json.loads(legacy)["turns"][-1]["task"]["request_limit"]
    expected_limit = old["request_limit"] if old["request_limit"] is not None else _CASE_REQUEST_CAP
    usage_preserved = (original_count == observation["original_http_attempts"] == old["requests"] == new["requests"] == new["total_requests"]
        and new["request_policy"] == "shared" and new["request_limit"] == legacy_limit == observation.get("legacy_projection_request_limit")
        and legacy_limit == expected_limit and new["requests"] <= expected_limit)
    backups = list((Path(observation["imported_path"]).parent / "backups").glob(Path(observation["imported_path"]).stem + "-v5-*.json"))
    same = old["id"] == new["id"] and old["scope"] == new["scope"] and old["approval"] == new["approval"]
    preserved = bool(len(backups) == 1 and backups[0].read_bytes() == legacy and sha256(legacy).hexdigest() == observation["legacy_sha256"])
    proof = {"original_http_attempts": original_count, "requests_before": old["requests"], "requests_after": new["requests"],
        "accepted_stop_binding": accepted_stop, "stopped_status_retained": stopped, "request_usage_preserved": usage_preserved,
        "open_import_and_blocked_resume_http_attempts": reopened_count, "same_task_scope_recipient": same,
        "legacy_original_and_backup_preserved": preserved, "legacy_missing_fields": observation["legacy_missing_fields"],
        "resume_blocked_reason": observation["resume_blocked_reason"],
        "original_product_request_limit": old["request_limit"], "legacy_projection_request_limit": new["request_limit"],
        "legacy_projection_note": "The separate v5 copy retains an explicit cap, or uses the frozen case cap for an independent original."}
    stop_preserved = accepted_stop and stopped and inactive_after_reopen
    passed = bool(usage_preserved and stop_preserved and same and preserved and proof["resume_blocked_reason"])
    return {"passed": passed, "quality_recovery_contract": proof, "program_facts_only": True,
        "critical_contracts": {"K1": "passed" if same else "failed", "K2": "not_applicable", "K3": "passed" if stop_preserved else "failed", "K4": "unknown", "K5": "passed" if same and preserved and inactive_after_reopen and usage_preserved else "unknown"},
        "critical_contract_applicability": {"K2": "This stopped read-only task triggers no approval decision.", "K4": "Fixed terminal outputs do not establish actual model semantic honesty."}}


async def exercise_stopped(context: CaseContext, visible: QualityVisibleInput, request: dict[str, Any], records: Path) -> CaseOutcome:
    root = context.root
    async with ServiceChannel(context, "initial", replies(visible, root, context.protocol), records=records, gates=[1]) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await initial.receive("started")
        await initial.arrived(1)
        held = json.loads((initial.root / "exchange.json").read_bytes())["exchanges"][-1]
        await initial.send({"action": "stop", "path": started["path"]})
        stop = await initial.receive("stopping", task_id=started["task_id"])
        initial.release(1)
        async with asyncio.timeout(30):
            while not Path(started["path"]).is_file() or json.loads(Path(started["path"]).read_bytes())["turns"][-1]["status"] != "cancelled":
                await asyncio.sleep(.01)
    path = Path(started["path"])
    raw = path.read_bytes()
    (root / "original-before.json").write_bytes(raw)
    legacy = json.loads(raw)
    legacy["version"] = 5
    missing = ("request_owner_id", "request_account", "execution_scope", "execution_facts", "request_policy", "total_requests", "delegation_max_turns", "max_turns", "turns_used")
    for turn in legacy["turns"]:
        for field in missing:
            turn.pop(field, None)
            turn["task"].pop(field, None)
        if turn["task"]["request_limit"] is None:
            turn["task"]["request_limit"] = request["max_attempts"]
    legacy_limit = legacy["turns"][-1]["task"]["request_limit"]
    legacy_path = root / "legacy-source-v5.json"
    legacy_bytes = (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    legacy_path.write_bytes(legacy_bytes)
    async with ServiceChannel(context, "reopened", [Reply(status=400)], records=records) as reopened:
        await reopened.send({"action": "open", "path": str(path)})
        await reopened.receive("conversation")
        await reopened.send({"action": "import", "path": str(legacy_path)})
        imported = await reopened.receive("conversation")
        await reopened.send({"action": "open", "path": imported["path"]})
        await reopened.receive("conversation")
        await reopened.configure()
        await reopened.send({"action": "resume_task", "path": imported["path"], "task_id": started["task_id"]})
        blocked = await reopened.receive("error")
    initial_exchange = json.loads((initial.root / "exchange.json").read_bytes())
    reopened_exchange = json.loads((reopened.root / "exchange.json").read_bytes())
    observation = {"original_http_attempts": len(initial_exchange["exchanges"]), "reopened_http_attempts": len(reopened_exchange["exchanges"]),
        "imported_path": imported["path"], "legacy_sha256": sha256(legacy_bytes).hexdigest(), "legacy_missing_fields": missing,
        "legacy_projection_request_limit": legacy_limit, "stop_sdk_input_id": held["sdk_input_id"],
        "stop_accepted_task_id": stop["task_id"], "resume_blocked_reason": blocked["message"]}
    (root / "recovery-observation.json").write_text(json.dumps(observation), encoding="utf-8")
    result = score_stopped(visible, root, [json.loads(Path(imported["path"]).read_bytes())], [initial_exchange, reopened_exchange])
    result.update(real_http_attempts=0, independent_quality_sample_increment=0, formal_execution=False,
        observation_layer="desktop-service-native-sdk-executor", actual_human_review="not_performed")
    return CaseOutcome("passed" if result["passed"] else "failed", result,
        {**initial.evidence(), **reopened.evidence(), "conversation": Path(imported["path"]), "original": root / "original-before.json", "legacy": legacy_path},
        case_id=context.case_id, protocol=context.protocol, critical_contracts=result["critical_contracts"])
