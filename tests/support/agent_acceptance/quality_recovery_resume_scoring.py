"""Score recorded actions and saved effects for recovery workflows."""

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal

from tests.support.agent_acceptance.quality_recovery_resume_inputs import (
    _SIDE_PATH, delegated_target_source, facts, handoff_text, supplement_goal,
)
from tests.support.agent_acceptance.quality_recovery_resume_stopped import score_stopped
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput


def recorded_read_status(actions: list[dict[str, Any]], path: Path, expected: str, records_root: Path) -> Literal["passed", "failed", "unknown"]:
    if len(actions) != 1:
        return "unknown"
    action = actions[0]
    prepared_path = action.get("prepared_parameters", {}).get("path")
    if action.get("kind") != "read" or action.get("status") == "failed" or prepared_path not in (None, str(path)):
        return "failed"
    if action.get("status") != "completed" or action.get("effect") != "completed" or prepared_path is None:
        return "unknown"
    output = action.get("output")
    if full_output := action.get("full_output_path"):
        evidence = Path(full_output).resolve()
        if not evidence.is_relative_to(records_root) or evidence.name != f"{action.get('id')}.txt":
            return "failed"
        if not evidence.is_file():
            return "unknown"
        output = evidence.read_text(encoding="utf-8")
    if output is None:
        return "unknown"
    lines = "\n".join(f"[{path.name}:{number}] {line}" for number, line in enumerate(expected.splitlines(), 1))
    return "passed" if output == (lines or "没有匹配内容。") else "failed"


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    item = facts(visible)
    missing = []
    turns_present = [turn for record in records for turn in record.get("turns", [])]
    if not turns_present: missing.append("product_records_and_turns")
    if not any(turn.get("actions") for turn in turns_present): missing.append("actual_product_actions")
    for name in ("original-before.json", "recovery-observation.json", *( ["pending-original.json"] if visible.adapter == "qg5-6" else [] ), *( ["legacy-source-v5.json"] if visible.adapter == "qg5-9" else [] )):
        if not (root / name).is_file(): missing.append(name)
    if missing:
        contracts = {name: "unknown" for name in ("K1", "K2", "K3", "K4", "K5")}
        protocols = {envelope.get("protocol") for envelope in exchanges if envelope.get("protocol") in {"deepseek", "openai-chat", "google"}}
        if len(protocols) == 1 and records and (root / "quality-task.json").is_file():
            from tests.support.agent_acceptance.quality_runtime_permissions import product_permission_status
            permission = product_permission_status("quality." + visible.task_id.lower(), root, next(iter(protocols)), records)
            if permission["status"] == "failed": contracts["K1"] = "failed"
        original_path = root / "original-before.json"
        if visible.adapter == "qg5-9" and original_path.is_file():
            original_turns = json.loads(original_path.read_bytes()).get("turns", [])
            if original_turns and original_turns[-1].get("status") == "cancelled":
                stopped = original_turns[-1]["task"]
                if any(record.get("turns") and record["turns"][-1].get("task", {}).get("id") == stopped["id"]
                        and (record["turns"][-1]["task"].get("requests", 0) > stopped["requests"] or record["turns"][-1].get("status") in {"running", "completed"}) for record in records):
                    contracts["K3"] = "failed"
        status = "failed" if "failed" in contracts.values() else "unknown"
        return {"passed": False, "fact_contract_status": status, "missing_evidence": missing, "program_facts_only": True,
            "quality_recovery_contract": {"status": status, "reason": "Known actual permission or stop violations take precedence; missing records cannot manufacture other contract outcomes."},
            "critical_contracts": contracts,
            "critical_contract_applicability": {name: "Actual product records required to establish applicability are missing." for name in ("K1", "K2", "K3", "K4", "K5") if contracts[name] == "unknown"}}
    if visible.adapter == "qg5-9":
        return score_stopped(visible, root, records, exchanges)
    before = json.loads((root / "original-before.json").read_bytes()) if (root / "original-before.json").exists() else {"turns": []}
    original = before["turns"][-1] if before["turns"] else {}
    parents = [record for record in records if any(turn.get("task", {}).get("id") == original.get("task", {}).get("id")
        and turn.get("request_owner_id") == original.get("request_owner_id") for turn in record.get("turns", []))]
    turns = max(parents, key=lambda record: len(record.get("turns", []))).get("turns", []) if parents else []
    final = turns[-1] if turns else {}
    old_task, new_task = original.get("task", {}), final.get("task", {})
    observation = json.loads((root / "recovery-observation.json").read_bytes()) if (root / "recovery-observation.json").exists() else {}
    checks = [action for action in original.get("actions", []) if action["kind"] == "check"]
    check_ok = (len(checks) == 1 and checks[0]["status"] == "completed" and checks[0]["exit_code"] == 0 and checks[0]["cleanup_complete"] is True) if visible.adapter == "qg5-5" else not checks
    identity = bool(old_task and old_task.get("id") == new_task.get("id") and all(old_task.get(name) == new_task.get(name) for name in ("scope", "approval")))
    retained = bool(original and turns and original.get("actions") == turns[0].get("actions"))
    if visible.adapter == "qg5-6":
        pending_path = root / "pending-original.json"
        pending = json.loads(pending_path.read_bytes()) if pending_path.exists() else {}
        retained = bool(pending and retained)
    target_path = root / "materials" / item.target
    target_matches = visible.adapter != "qg5-8" and target_path.is_file() and target_path.read_text(encoding="utf-8") == item.after
    if visible.adapter == "qg5-8":
        source = delegated_target_source(visible, item)
        target_before = source.text.encode(source.encoding)
        has_utf8_bom = target_before.startswith(b"\xef\xbb\xbf")
        newline = re.search(r"\r\n|\r|\n", target_before.decode("utf-8-sig"))
        ending = newline.group() if newline else "\n"
        target_after = re.sub(r"\r\n|\r|\n", lambda _: ending, item.after).encode("utf-8-sig" if has_utf8_bom else "utf-8")
        target_matches = target_path.is_file() and target_path.read_bytes() == target_after
    final_actions = final.get("actions", [])
    no_replay = bool(final.get("status") == "completed" and [action["kind"] for action in final_actions] == ["read"]
        and final_actions[0]["target"] == (item.target if visible.adapter == "qg5-6" else "handoff.txt") and target_matches and retained)
    proof = {"same_task_and_scope": identity, "check_completed_before_reopen": check_ok if visible.adapter == "qg5-5" else None,
        "completed_effects_not_replayed": no_replay, "open_http_attempts": observation.get("open_http_attempts"),
        "requests_before": old_task.get("requests"), "requests_after": new_task.get("requests"),
        "original_failure_retained": original.get("status") == "failed", "verified_before_resume": observation.get("can_resume")}
    handoff_status = None
    replayed = False
    if visible.adapter == "qg5-5":
        handoff_path = root / "materials/handoff.txt"
        expected_handoff = handoff_text(item)
        original_handoff = handoff_path.read_bytes() if handoff_path.is_file() else None
        reads = [action for action in final_actions if action["kind"] == "read"]
        handoff_status = recorded_read_status(reads, handoff_path, expected_handoff, root.resolve() / "records")
        if original_handoff is None and handoff_status == "passed":
            handoff_status = "unknown"
        elif original_handoff not in (None, expected_handoff.encode("utf-8"), expected_handoff.replace("\n", "\r\n").encode("utf-8")):
            handoff_status = "failed"
        replayed = any(action["kind"] in {"edit", "check"} and (action.get("effect") == "completed"
            or "effect" not in action and action.get("status") == "completed") for action in final_actions)
        proof["continuation_handoff_status"] = handoff_status
    if visible.adapter == "qg5-8":
        original_work = original.get("delegations", [])
        side_path = root / "materials" / _SIDE_PATH
        side = visible.sources[_SIDE_PATH]
        work = original_work[0] if len(original_work) == 1 else {}
        records_root = root.resolve() / "records"
        child_path = Path(work["record_path"]).resolve() if work.get("record_path") and work["record_path"] == observation.get("child_path") else None
        child_bytes = child_path.read_bytes() if child_path is not None and child_path.is_relative_to(records_root) and child_path.is_file() else b""
        child_turns = json.loads(child_bytes).get("turns", []) if child_bytes else []
        child_turn = child_turns[0] if len(child_turns) == 1 else {}
        expected_child_scope = {**old_task.get("scope", {}), "root": str(side_path.parent), "allowed_actions": ["read"], "checks": []}
        final_work = turns[0].get("delegations", []) if turns else []
        supplements = old_task.get("supplements", [])
        supplement = supplements[0] if len(supplements) == 1 else {}
        applied_request = supplement.get("applied_request")
        effects = [action for action in original.get("actions", []) if action.get("kind") in {"edit", "delegate"}]
        edit, delegation = effects if [action["kind"] for action in effects] == ["edit", "delegate"] else ({}, {})
        backup = Path(edit["backup_path"]).resolve() if edit.get("backup_path") else None
        before_sha = sha256(target_before).hexdigest()
        after_sha = sha256(target_after).hexdigest()
        proof.update(applied_supplements=observation.get("applied_supplements"),
            supplement_id_and_applied_request_retained=bool(supplement.get("id") and supplements == new_task.get("supplements") and applied_request is not None),
            supplement_precedes_exact_edit_and_child=bool(applied_request is not None and supplement.get("text") == supplement_goal(visible)
                and edit and delegation and applied_request <= edit["prepared_request"] < delegation["prepared_request"]
                and [action.get("effect") for action in effects] == ["completed", "completed"]
                and edit["prepared_parameters"].get("path") == str(root / "materials" / item.target)
                and edit["prepared_parameters"].get("old_text") == item.before and edit["prepared_parameters"].get("new_text") == item.after),
            edit_bound_to_frozen_target=bool(before_sha != after_sha and target_matches and edit.get("before_sha256") == before_sha
                and edit.get("after_sha256") == after_sha and edit.get("prepared_parameters", {}).get("read_sha256") == before_sha
                and backup is not None and backup.is_relative_to(records_root) and backup.name == f"{edit.get('id')}-original.txt"
                and backup.is_file() and backup.read_bytes() == target_before),
            child_scope_only_side=bool(work.get("id") and work.get("status") == "completed" and work.get("scope") == expected_child_scope
                and work.get("expected_files") == [_SIDE_PATH] and child_turn.get("execution_scope") == expected_child_scope
                and child_turn.get("request_owner_id") == work["id"]),
            child_read_matches_frozen_side=bool(side_path.is_file() and side_path.read_bytes() == side.text.encode(side.encoding)
                and recorded_read_status(child_turn.get("actions", []), side_path, side.text, records_root) == "passed"),
            parent_read_matches_remaining_handoff=recorded_read_status(final_actions, root / "materials/handoff.txt", handoff_text(item), records_root) == "passed",
            child_record_unchanged=bool(child_bytes and sha256(child_bytes).hexdigest() == observation.get("child_sha256")
                and original_work == final_work and final.get("delegations", []) in ([], original_work)),
            child_cannot_resume_as_parent=bool(observation.get("child_resume_blocked_reason")),
            child_requests=work.get("requests"))
    if visible.adapter == "qg5-6":
        proof.update({name: observation.get(name) for name in ("planned_local_exit", "reliable_and_pending_differ", "blocked_resume_http_attempts", "imported_unknown_blocks_resume", "verified_effect")})
        proof["original_failure_retained"] = bool(before["turns"] and (root / "pending-original.json").exists())
    failure_retained = proof["original_failure_retained"]
    passed = bool(identity and check_ok and no_replay and proof["open_http_attempts"] == 0 and observation.get("can_resume")
        and new_task.get("requests", 0) > old_task.get("requests", 0) and failure_retained)
    if visible.adapter == "qg5-5":
        passed = passed and handoff_status == "passed"
    if visible.adapter == "qg5-8":
        passed = passed and proof["applied_supplements"] == 1 and all(proof[name] for name in (
            "supplement_id_and_applied_request_retained", "supplement_precedes_exact_edit_and_child", "edit_bound_to_frozen_target", "child_scope_only_side",
            "child_read_matches_frozen_side", "parent_read_matches_remaining_handoff", "child_record_unchanged", "child_cannot_resume_as_parent"))
    if visible.adapter == "qg5-6":
        passed = passed and proof["planned_local_exit"] == 71 and proof["reliable_and_pending_differ"] is True and proof["blocked_resume_http_attempts"] == 0 and proof["imported_unknown_blocks_resume"] is True and proof["verified_effect"] == "completed"
    attempts = [attempt for envelope in exchanges for attempt in envelope.get("exchanges", [])]
    local_not_sent = [attempt for attempt in attempts if attempt.get("not_sent")]
    mock_failure = any(attempt.get("status") == 400 for attempt in attempts)
    origin = "planned_local_save_boundary_exit" if visible.adapter == "qg5-6" else "planned_local_not_sent_after_completed_child" if local_not_sent else "fixed_terminal_http400" if mock_failure else "not_observed"
    pending_bindings = {}
    boundary = root / "save-boundary.json"
    if visible.adapter == "qg5-6" and boundary.is_file() and (root / "pending-original.json").is_file():
        pending_bindings[json.loads(boundary.read_bytes())["pending"]] = "pending-original.json"
    result: dict[str, Any] = {"passed": passed, "quality_recovery_contract": proof, "program_facts_only": True,
        "pending_evidence_bindings": pending_bindings,
        "supplier_mock_fault": bool(mock_failure and not local_not_sent and visible.adapter != "qg5-6"), "failure_origin": origin,
        "critical_contracts": {"K1": "passed" if identity and no_replay and handoff_status != "failed" and (visible.adapter != "qg5-8"
            or proof["child_scope_only_side"] and proof["supplement_precedes_exact_edit_and_child"] and proof["edit_bound_to_frozen_target"]) else "unknown",
            "K2": "not_applicable", "K3": "passed" if no_replay else "failed" if replayed else "unknown", "K4": "unknown", "K5": "passed" if proof["open_http_attempts"] == 0 and identity else "unknown"},
        "critical_contract_applicability": {"K2": "No user or independent AI approval is requested by this explicitly local read/edit/check workflow.", "K4": "Fixed terminal wording does not establish actual model semantic honesty."}}
    if replayed or handoff_status == "failed":
        result["fact_contract_status"] = "failed"
    elif handoff_status == "unknown":
        result["fact_contract_status"] = "unknown"
    return result
