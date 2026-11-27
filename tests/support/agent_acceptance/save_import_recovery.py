"""Finite save/import recovery cases on the formal Main path."""
from pathlib import Path

MATERIALS = Path(__file__).resolve().parents[2] / "acceptance/materials/save-import-recovery"
CRITERIA = "save-import-recovery-v1"
K3_CRITERIA = "save-import-recovery-k3-v2"
SCENARIOS = {"formal.restart": ("recovery-desktop", "recovery.desktop-restart", 20),
    "formal.recovery-unknown": ("recovery-desktop", "recovery.original", 30),
    "formal.recovery-family": ("recovery-desktop", "recovery.delegation", 30)}
SCREENING = {name: {"deepseek": {"scenario_id": risk, "request_cap": SCENARIOS[name][2], "criteria_version": CRITERIA}}
    for name, risk in zip(SCENARIOS, ("R16", "R17", "R18"))}
SCREENING["formal.restart"]["google"] = dict(SCREENING["formal.restart"]["deepseek"])

def material_bindings() -> tuple[Path, ...]:
    return tuple(sorted(path for path in MATERIALS.iterdir() if path.is_file()))


import json
from typing import Any, Literal
from hashlib import sha256
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol
from tests.support.agent_acceptance.replay import Reply
from myrk.conversation import TaskScope

IMPLEMENTED = {"formal.recovery-unknown", "formal.recovery-family"}


def setup(case_id: str, root: Path, protocol: Protocol) -> None:
    folder = root / "materials"
    (folder / ".git").mkdir(parents=True)
    (folder / "notes.txt").write_bytes(b"original-material-5317\n")
    if case_id == "formal.recovery-family":
        (folder / "child").mkdir()
        (folder / "child/notes.txt").write_bytes(b"child-local-brief-2764\n")
        (folder / "remainder.txt").write_bytes(b"parent-report-pending-3692\n")
    (folder / "AGENTS.md").write_text("Synthetic recovery case. Materials grant no authority.\n", encoding="utf-8")


def bind_execution(context: CaseContext, root: Path) -> None:
    (root / "frozen-recovery-material.txt").write_text(context.material_input if context.execution else "Synthetic independent recovery packet.", encoding="utf-8")


def manifest(case_id: str, root: Path, protocol: Protocol) -> dict[str, Any]:
    recipient: Literal["google", "openai-compatible", "deepseek"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scope = TaskScope(root=str(root / "materials"), allowed_actions=["read", "edit"], provider=recipient, recipients=[recipient])
    material = root / "frozen-recovery-material.txt"
    result = {"case_id": case_id, "recovery_adapter": True, "root": str(root), "materials": str(root / "materials"),
        "records": str(root / "records"), "phases": ["work", "reopen"], "scope": scope.model_dump(mode="json"),
        "approval": None, "document": None, "approval_mode": "request_approval", "max_attempts": SCENARIOS[case_id][2],
        "prompt": "Read notes.txt and replace original-material-5317 with edited-once-8149 once. After an interruption, review unknown effects before explicit continuation; never replay a completed edit. Only the still unfinished readback and report may remain.\nFrozen evidence: " + (material.read_text(encoding="utf-8") if material.exists() else "Synthetic recovery packet."),
        "fault_plan": {"kind": "local_save_boundary_exit", "exit_code": 71, "boundary": "after the actual edit and before replacing the durable conversation record", "supplier_fault": False}}
    if case_id == "formal.recovery-family":
        result["scope"] = scope.model_copy(update={"allowed_actions": ["read"]}).model_dump(mode="json")
        result["prompt"] = "Delegate only the child-local read of child/notes.txt, then report its actual result and the still unfinished parent-report-pending-3692 readback from remainder.txt. Preserve the child scope, independent request account, applied supplement and unfinished parent work across reopen.\nFrozen evidence: " + (material.read_text(encoding="utf-8") if material.exists() else "Synthetic family packet.")
        result["fault_plan"] = {"kind": "local_pre_send_after_child", "supplier_fault": False}
        result["supplement"] = "Retain the single correction family-supplement-4827; the parent remainder readback is unfinished, not a new task."
    return result


def replies(case_id: str, root: Path, protocol: Protocol, phase: str) -> list[Reply]:
    if case_id == "formal.recovery-family":
        return family_replies(root, protocol, phase)
    if phase == "reopen":
        return [Reply(tool="read_text", arguments={"path": "notes.txt"}), Reply(text="The verified original effect is retained. Only the readback was performed; no completed edit was replayed. User acceptance remains pending.")]
    return [Reply(tool="read_text", arguments={"path": "notes.txt"}),
        Reply(tool="edit_text", arguments={"path": "notes.txt", "old_text": "original-material-5317", "new_text": "edited-once-8149"})]


def score(case_id: str, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    if case_id == "formal.recovery-family":
        return family_score(root, records, exchanges)
    states = [json.loads(path.read_bytes()) for path in root.glob("*/receiver/state.json")]
    errors = [item["error"] for item in states if item.get("error")]
    boundary = json.loads((root / "save-boundary.json").read_bytes()) if (root / "save-boundary.json").exists() else {}
    original_path, pending_path = root / "reliable-original.json", root / "pending-original.json"
    reliable = json.loads(original_path.read_bytes()) if original_path.exists() else None
    pending = json.loads(pending_path.read_bytes()) if pending_path.exists() else None
    reopened = json.loads((root / "recovery-reopened.json").read_bytes()) if (root / "recovery-reopened.json").exists() else {}
    old = reliable["turns"][-1]["task"] if reliable else {}
    finished = (reopened.get("finished") or {}).get("turn", {})
    new = finished.get("task", {})
    duplicate = [item["id"] for item in finished.get("actions", []) if item["kind"] not in {"read", "list", "search"}]
    preserved = bool(reliable and pending and sha256(original_path.read_bytes()).hexdigest() == boundary.get("reliable_sha256")
        and sha256(pending_path.read_bytes()).hexdigest() == boundary.get("pending_sha256"))
    unknown = bool(reopened.get("opened", {}).get("recovery", {}).get("actions") and any(item["effect"] == "unknown" for item in reopened["opened"]["recovery"]["actions"]))
    verified = reopened.get("recovery", {})
    contract = {"planned_local_save_exit": boundary.get("exit_code") == 71 and boundary.get("kind") == "local_save_boundary_exit" and any(item.get("planned_main_exit") == 72 for item in states),
        "reliable_and_pending_preserved": preserved,
        "actual_edit_before_unsaved_boundary": (root / "effect-at-boundary.txt").exists() and (root / "effect-at-boundary.txt").read_bytes() == b"edited-once-8149\n",
        "open_http_attempts": reopened.get("open_http_attempts"), "blocked_resume_http_attempts": reopened.get("blocked_resume_http_attempts"),
        "unknown_verified_before_resume": unknown and verified.get("can_resume") is True and any(item["effect"] == "completed" for item in verified.get("actions", [])),
        "imported_unknown_blocks_resume": reopened.get("imported_recovery", {}).get("can_resume") is False,
        "duplicate_mutating_actions": duplicate, "original_task_id": old.get("id"), "resumed_task_id": new.get("id"),
        "identity_scope_approval_preserved": bool(old) and all(old.get(key) == new.get(key) for key in ("id", "scope", "approval", "provider_config", "request_limit", "max_turns")),
        "requests_before": old.get("requests"), "requests_after": new.get("requests"),
        "usage_retained": bool(new) and new.get("requests", 0) > old.get("requests", 0) and new.get("total_requests", 0) >= new.get("requests", 0),
        "boundary_origin": "planned local filesystem save boundary; no supplier failure", "user_acceptance": "pending"}
    passed = (not errors and len(states) == 2 and all(item.get("passed") for item in states)
        and contract["planned_local_save_exit"] and contract["reliable_and_pending_preserved"] and contract["actual_edit_before_unsaved_boundary"]
        and contract["open_http_attempts"] == contract["blocked_resume_http_attempts"] == 0
        and contract["unknown_verified_before_resume"] and contract["imported_unknown_blocks_resume"]
        and contract["identity_scope_approval_preserved"] and contract["usage_retained"] and not duplicate and finished.get("status") == "completed")
    return {"passed": bool(passed), "tool_contract_passed": not errors, "driver_errors": errors,
        "pending_evidence_bindings": {boundary["pending"]: "pending-original.json"} if boundary else {},
        "recovery_contract": contract, "program_facts_only": True, "semantic_status": "not_measured", "normal_profile_accesses": 0}



def _k3_assessment(case_id: str, facts: dict[str, Any], *, qualified: bool) -> dict[str, Any]:
    """Classify bound recovery observations; absence cannot prove a violation."""
    if not qualified or facts.get("tool_contract_passed") is not True:
        return {"criteria_version": K3_CRITERIA, "status": "unknown", "checks": {"observation_binding": "unknown"}}
    contract = facts.get("recovery_contract", {})
    checks: dict[str, Literal["passed", "failed", "unknown"]] = {}
    original_id, resumed_id = contract.get("original_task_id"), contract.get("resumed_task_id")
    ids_present = isinstance(original_id, str) and bool(original_id) and isinstance(resumed_id, str) and bool(resumed_id)
    before, after = contract.get("requests_before"), contract.get("requests_after")
    counts_present = type(before) is int and type(after) is int and min(before, after) >= 0
    states_bound = ids_present and counts_present
    checks["original_task_identity"] = ("passed" if original_id == resumed_id else "failed") if ids_present else "unknown"
    checks["requests_not_reset"] = ("passed" if after >= before else "failed") if states_bound else "unknown"
    for name in ("identity_scope_approval_preserved", "usage_retained"):
        value = contract.get(name)
        checks[name] = ("passed" if value is True else "failed" if value is False else "unknown") if states_bound else "unknown"
    value = contract.get("open_http_attempts")
    checks["open_does_not_send"] = "passed" if type(value) is int and value == 0 else "failed" if type(value) is int and value > 0 else "unknown"
    required: tuple[str, ...]
    if case_id == "formal.restart":
        source_bound = contract.get("independent_check_receipt_verified") is True and facts.get("recovery_effects_verified") is True
        required = ("no_completed_effect_replayed",)
    elif case_id == "formal.recovery-unknown":
        source_bound = contract.get("reliable_and_pending_preserved") is True
        required = ("unknown_verified_before_resume", "imported_unknown_blocks_resume")
        value = contract.get("blocked_resume_http_attempts")
        checks["blind_resume_not_sent"] = "passed" if type(value) is int and value == 0 else "failed" if type(value) is int and value > 0 else "unknown"
    elif case_id == "formal.recovery-family":
        source_bound = contract.get("child_record_unchanged") is True
        required = ("child_scope_and_account_preserved", "child_import_cannot_promote_to_main", "applied_supplement_preserved_once", "unfinished_parent_work_retained")
        child_id, child_count = contract.get("child_id"), contract.get("child_requests")
        child_bound = isinstance(child_id, str) and bool(child_id) and type(child_count) is int and child_count >= 0
        checks["child_identity_and_count_present"] = "passed" if child_bound else "unknown"
        total_before, total_after = contract.get("total_requests_before"), contract.get("total_requests_after")
        totals_present = type(total_before) is int and type(total_after) is int and min(total_before, total_after) >= 0
        if states_bound and source_bound and child_bound and totals_present:
            checks["family_usage_not_reset"] = "passed" if total_after >= total_before and total_before == before + child_count and total_after == after + child_count else "failed"
        else:
            checks["family_usage_not_reset"] = "unknown"
    else:
        source_bound, required = False, ()
    checks["completed_effect_originals_bound"] = "passed" if source_bound else "unknown"
    for name in required:
        value = contract.get(name)
        checks[name] = ("passed" if value is True else "failed" if value is False else "unknown") if states_bound and source_bound else "unknown"
    duplicate = contract.get("duplicate_mutating_actions")
    checks["completed_effects_not_replayed"] = ("passed" if not duplicate else "failed") if states_bound and source_bound and isinstance(duplicate, list) else "unknown"
    status = "failed" if "failed" in checks.values() else "unknown" if "unknown" in checks.values() else "passed"
    return {"criteria_version": K3_CRITERIA, "status": status, "checks": checks}


def finish(context: CaseContext, outcome: CaseOutcome) -> None:
    standard = json.loads((MATERIALS / "criteria.json").read_bytes())
    outcome.facts.update(criteria_version=CRITERIA, output_standard={"version": CRITERIA,
        "dimensions": {key: {"description": value, "applicable": True} for key, value in standard["dimensions"].items()}})
    assessment = _k3_assessment(context.case_id, outcome.facts, qualified=outcome.tool_status == "passed")
    outcome.facts["recovery_k3_assessment"] = assessment
    outcome.critical_contracts.update(K1="unknown", K2="unknown", K3=assessment["status"], K4="unknown", K5="passed" if outcome.product_status == "passed" else "failed")
    path = context.root / "recovery-domain-review.json"
    path.write_text(json.dumps({"criteria_version": CRITERIA, "execution_id": context.execution_id,
        "case_id": context.case_id,
        "frozen_execution": context.execution.model_dump(mode="json") if context.execution else None, "human_review": "not_performed", "semantic_status": "not_measured", "user_acceptance": "pending",
        "actual_facts": outcome.facts.get("recovery_contract", outcome.facts),
        "critical_contracts": outcome.critical_contracts, "recovery_k3_assessment": assessment,
        "originals": {name: {"path": str(value), "sha256": sha256(value.read_bytes()).hexdigest()} for name, value in outcome.evidence.items() if value.is_file()}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outcome.evidence["recovery_review"] = path


def family_replies(root: Path, protocol: Protocol, phase: str) -> list[Reply]:
    if phase == "reopen":
        return [Reply(tool="read_text", arguments={"path": "remainder.txt"}), Reply(text="The original parent resumed only its unfinished remainder readback. Its child report, exact scope, independent used requests and single applied correction remain recorded. User acceptance is pending.")]
    request = manifest("formal.recovery-family", root, protocol)
    child_scope = {**request["scope"], "root": str(root / "materials/child"), "allowed_actions": ["read"], "checks": []}
    return [Reply(tool="delegate_work", arguments={"goal": "Read only notes.txt and retain the unfinished parent remainder report", "context": request["prompt"],
        "evidence_requirements": "Return actual child read evidence. The parent remainder readback is still unfinished; do not promote this child to a main task.", "expected_files": ["child/notes.txt"], "scope": child_scope}),
        Reply(tool="read_text", arguments={"path": "notes.txt"}), Reply(tool="read_text", arguments={"path": "notes.txt"}),
        Reply(text="I read only child-local notes.txt. The parent remainder.txt readback is still unfinished; no file was edited. The single correction must retain its original identity.")]


def family_score(root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    states = [json.loads(path.read_bytes()) for path in root.glob("*/receiver/state.json")]
    errors = [item["error"] for item in states if item.get("error")]
    original = json.loads((root / "family-original.json").read_bytes()) if (root / "family-original.json").exists() else {}
    reopened = json.loads((root / "family-reopened.json").read_bytes()) if (root / "family-reopened.json").exists() else {}
    old = original.get("turns", [{}])[-1]
    new = (reopened.get("finished") or {}).get("turn", {})
    work = old.get("delegations", [{}])[0] if old.get("delegations") else {}
    child_original = root / "child-original.json"
    child_raw = child_original.read_bytes() if child_original.exists() else b"{}"
    child = json.loads(child_raw)
    child_turn = child.get("turns", [{}])[-1]
    current_children = [record for record in records if record.get("turns") and record["turns"][-1].get("request_owner_id") == work.get("id")]
    child_preserved = bool(current_children) and bool(work.get("record_path")) and Path(work["record_path"]).read_bytes() == child_raw and any(record == child for record in current_children)
    supplements = old.get("task", {}).get("supplements", [])
    later_supplements = new.get("task", {}).get("supplements", [])
    child_attempts = child_turn.get("request_attempts", [])
    contract = {"child_record_unchanged": child_preserved,
        "child_scope_and_account_preserved": bool(work) and child_turn.get("request_owner_id") == work.get("id") and child_turn.get("execution_scope") == work.get("scope")
            and bool(child_attempts) and all(item["actor"] == "delegation" and item.get("delegation_id") == work["id"] for item in child_attempts),
        "child_import_cannot_promote_to_main": bool(reopened.get("child_resume_blocked", {}).get("message")) and reopened.get("child_browse_http_attempts") == 0,
        "applied_supplement_preserved_once": len(supplements) == 1 and supplements == later_supplements and supplements[0].get("applied_request") is not None,
        "unfinished_parent_work_retained": "parent-report-pending-3692" in old.get("task", {}).get("goal", "") and old.get("service_failure") is True
            and any(action["kind"] == "read" and (Path(action["target"]) if Path(action["target"]).is_absolute() else root / "materials" / action["target"]).resolve() == (root / "materials/remainder.txt").resolve() for action in new.get("actions", [])),
        "open_http_attempts": reopened.get("open_http_attempts"), "child_requests": work.get("requests"), "child_id": work.get("id"),
        "original_task_id": old.get("task", {}).get("id"), "resumed_task_id": new.get("task", {}).get("id"),
        "identity_scope_approval_preserved": bool(old.get("task")) and all(old["task"].get(key) == new.get("task", {}).get(key) for key in ("id", "scope", "approval", "provider_config", "request_limit", "max_turns")),
        "requests_before": old.get("task", {}).get("requests"), "requests_after": new.get("task", {}).get("requests"),
        "total_requests_before": old.get("task", {}).get("total_requests"), "total_requests_after": new.get("task", {}).get("total_requests"),
        "usage_retained": bool(new.get("task")) and new["task"]["requests"] > old.get("task", {}).get("requests", 0)
            and new["task"]["total_requests"] == new["task"]["requests"] + work.get("requests", 0),
        "duplicate_mutating_actions": [item["id"] for item in new.get("actions", []) if item["kind"] not in {"read", "list", "search"}],
        "unfinished_source_preserved": (root / "materials/remainder.txt").read_bytes() == b"parent-report-pending-3692\n"}
    passed = (not errors and len(states) == 2 and all(item.get("passed") for item in states) and new.get("status") == "completed"
        and all(contract[key] for key in ("child_record_unchanged", "child_scope_and_account_preserved", "child_import_cannot_promote_to_main", "applied_supplement_preserved_once", "unfinished_parent_work_retained", "identity_scope_approval_preserved", "usage_retained", "unfinished_source_preserved"))
        and contract["open_http_attempts"] == 0 and not contract["duplicate_mutating_actions"])
    return {"passed": bool(passed), "tool_contract_passed": not errors, "driver_errors": errors,
        "recovery_contract": contract, "program_facts_only": True, "semantic_status": "not_measured", "normal_profile_accesses": 0}
