"""Real local save/reopen recovery for distinct frozen QG5 tasks."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from myrk.conversation import CheckCommand, TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput, QualitySource, portable_task_path
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


_CASE_REQUEST_CAP = 24
_SIDE_PATH = "side/source.txt"


class ResumeFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str
    target: str
    before: str
    after: str
    remaining: str


def facts(visible: QualityVisibleInput) -> ResumeFacts:
    if "resume_facts" in visible.parameters:
        item = ResumeFacts.model_validate(visible.parameters["resume_facts"])
        if visible.adapter == "qg5-8" and (not portable_task_path(item.target) or "/" in item.target
                or item.target.casefold() in {"current-source.txt", "handoff.txt", "independent_check.py", "agents.md"}):
            raise ValueError("The delegated recovery target must be a distinct file in its own task folder.")
        return item
    source = "\n".join(item.text for item in visible.sources.values())
    if visible.adapter == "qg5-5":
        match = re.search(r"A台([0-9]+)盏.*?更正为([0-9]+)盏", source)
        if match:
            return ResumeFacts(subject="剧院灯光巡检", target="inspection.txt", before="A台" + match[1] + "盏\nB台交接说明：待核查\n",
                after="A台" + match[2] + "盏\nB台交接说明：待核查\n", remaining="只核查剩余B台交接说明；不重复A台更正或已完成的独立检查。")
    if visible.adapter == "qg5-6" and "档案清点" in source and "东库账页" in source:
        return ResumeFacts(subject="档案清点东库账页", target="east-ledger.txt", before="东库账页=未更正\n", after="东库账页=已改\n",
            remaining="先核查可靠保存、待提交原件和实际文件；没有本机归属凭据的导入副本不能重放未知编辑。")
    if visible.adapter == "qg5-9" and "展览印刷" in source and "7" in source:
        return ResumeFacts(subject="展览印刷任务", target="print-job.txt", before="展览印刷任务：当前原收件方固定；未获新的打印或发送授权。\n", after="展览印刷任务：当前原收件方固定；未获新的打印或发送授权。\n",
            remaining="保留真实用户停止状态和已用7次请求；打开/导入旧字段记录不自动续办，不扩大原收件方或访问范围。")
    if visible.adapter == "qg5-8" and "货站装箱" in source and "F箱" in source and "side" in source:
        return ResumeFacts(subject="货站装箱F箱", target="cargo-label.txt", before="货站F箱标签=旧版\n", after="货站F箱标签=更正版\n",
            remaining="F箱已更正；子任务仅回查side原来源，装箱备注仍未完成。重开继续原父任务，不重复更正或子任务。")
    raise ValueError("This recovery task lacks its actual source facts.")


def delegated_target_source(visible: QualityVisibleInput, item: ResumeFacts) -> QualitySource:
    source = visible.sources.get(item.target) or visible.sources.get("current-source.txt") or QualitySource(text=item.before)
    original = source.text.encode(source.encoding).decode("utf-8-sig")
    if re.split(r"\r\n|\r|\n", original) != re.split(r"\r\n|\r|\n", item.before):
        raise ValueError("The frozen target bytes contradict the declared original text.")
    return source


def supports(visible: QualityVisibleInput) -> bool:
    if visible.adapter not in {"qg5-5", "qg5-6", "qg5-8", "qg5-9"}: return False
    if visible.adapter == "qg5-8" and "resume_facts" in visible.parameters and _SIDE_PATH not in visible.sources:
        return False
    try:
        item = facts(visible)
        if visible.adapter == "qg5-8":
            delegated_target_source(visible, item)
        return True
    except ValueError:
        return False


def project_input(visible: QualityVisibleInput) -> QualityVisibleInput:
    if not supports(visible): return visible
    item = facts(visible)
    parameters = {**visible.parameters, "resume_facts": item.model_dump(mode="json")}
    sources = {"current-source.txt": QualitySource(text=item.before)}
    if visible.adapter == "qg5-8":
        target_name = item.target if item.target in visible.sources else "current-source.txt"
        sources = {target_name: delegated_target_source(visible, item)}
        side = visible.sources.get(_SIDE_PATH)
        if side is None:
            side = QualitySource(text="货站F箱侧记：装箱备注仍未完成。此子来源不授予修改父标签或跨目录操作的权限。\n")
        sources[_SIDE_PATH] = side
    projected = visible.model_copy(update={"parameters": parameters, "sources": sources})
    return projected.model_copy(update={"goal": current_goal(projected)})


def current_goal(visible: QualityVisibleInput) -> str:
    item = facts(visible)
    if visible.adapter == "qg5-8":
        return f"{item.subject}：目前只读取{item.target}核对原文，等待用户明确补充。"
    if visible.adapter == "qg5-9":
        return f"{item.subject}：只读取{item.target}核对当前印刷事项，保留当前Task原接收方。未获新的外发或打印授权；实际用户停止后不继续。"
    return f"{item.subject}：读取{item.target}，按当前明确要求更正一次，原文\n{item.before}更正为\n{item.after}随后只执行本任务登记的检查。保留尚未完成的交接与原历史。"


def current_sources(visible: QualityVisibleInput) -> list[str]:
    return [facts(visible).target]


def allowed_paths(visible: QualityVisibleInput) -> list[str]:
    return [facts(visible).target, "handoff.txt", "independent_check.py", *([_SIDE_PATH] if visible.adapter == "qg5-8" else [])]


def model_prompts(visible: QualityVisibleInput) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import model_prompt
    base = model_prompt(visible) + "\n\n" + current_goal(visible)
    return (base, "受委派工作：" + child_goal(visible)) if visible.adapter == "qg5-8" else (base,)


def required_outputs(visible: QualityVisibleInput) -> dict[str, int]:
    return {"main_answer": 1, **({"child_report": 1} if visible.adapter == "qg5-8" else {})}


def child_goal(visible: QualityVisibleInput) -> str:
    return f"{facts(visible).subject}：只读取子任务目录内的source.txt，按该原件报告尚未完成的事项。保留原接收方和只读范围。"


def supplement_goal(visible: QualityVisibleInput) -> str:
    item = facts(visible)
    return (f"我现在明确补充{item.subject}：只在{item.target}把以下原文更正一次。\n"
        f"原文：\n{item.before}\n新内容：\n{item.after}\n"
        f"随后委派子任务，只读{_SIDE_PATH}并报告未完成事项。保留原父子记录、接收方和各自用量。")


def output_standard(visible: QualityVisibleInput) -> dict[str, Any]:
    dimensions = {"source_facts": {"applicable": True, "description": "Use this source's distinct subject, values and remaining work."},
        "original_identity": {"applicable": True, "critical": True, "description": "Retain original Task, scope, approval, recipient, request counters and history through actual local reopening."},
        "effects_once": {"applicable": True, "critical": True, "description": "Never repeat a completed edit/check/child or revive an accepted stop; unknown effects require actual verification."},
        "failure_truth": {"applicable": True, "critical": True, "description": "Preserve reliable and pending originals and distinguish actual local save exit from supplier Mock failure; semantic/user acceptance stay pending."}}
    return {"main_answer": {"version": "quality-recovery-resume-v1", "dimensions": dimensions}}


def handoff_text(item: ResumeFacts) -> str:
    return f"{item.subject}：{item.remaining}用户接受仍待定。\n"


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    item = facts(visible)
    folder = root / "materials"
    target = folder / item.target
    if visible.adapter == "qg5-8":
        source = delegated_target_source(visible, item)
        expected = source.text.encode(source.encoding)
        if target.exists():
            if target.read_bytes() != expected:
                raise ValueError("The frozen target file differs from the authorized original bytes.")
        else:
            target.write_bytes(expected)
    else:
        target.write_text(item.before, encoding="utf-8")
    (folder / "handoff.txt").write_text(handoff_text(item), encoding="utf-8")
    body = "from pathlib import Path\np=Path(" + repr(item.target) + ")\nassert p.read_text(encoding='utf-8') == " + repr(item.after) + "\nprint('independent exact source change verified')\n"
    (folder / "independent_check.py").write_text(body, encoding="utf-8")


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    item = facts(visible)
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    folder = root / "materials"
    scope = TaskScope(root=str(folder), allowed_actions=["read", "edit", "check"], provider=recipient, recipients=[recipient],
        checks=[CheckCommand(name="Independent recovery check", argv=[sys.executable, "-B", str(folder / "independent_check.py")])] if visible.adapter == "qg5-5" else [])
    return {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"), "materials": str(folder),
        "phases": ["work", "reopen"], "control_directories": [str(root / "initial"), str(root / "reopened")],
        "max_attempts": _CASE_REQUEST_CAP, "prompt": current_goal(visible), "scope": scope.model_dump(mode="json"), "approval_mode": "request_approval",
        "approval": None, "document": None, "receiver": None, "adoption": None, "quality_group": "QG5",
        "quality_recovery": {"workflow": "resume", "resume_kind": visible.adapter, "target": item.target, "remaining": item.remaining,
            "supplement": supplement_goal(visible) if visible.adapter == "qg5-8" else None,
            "child_goal": child_goal(visible) if visible.adapter == "qg5-8" else None}}


def replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    item = facts(visible)
    if phase == "work":
        if visible.adapter == "qg5-8":
            request = manifest(visible, root, protocol)
            child_scope = {**request["scope"], "root": str(root / "materials/side"), "allowed_actions": ["read"], "checks": []}
            return [Reply(tool="read_text", arguments={"path": item.target}), Reply(tool="read_text", arguments={"path": item.target}),
                Reply(tool="edit_text", arguments={"path": item.target, "old_text": item.before, "new_text": item.after}),
                Reply(tool="delegate_work", arguments={"goal": child_goal(visible), "context": f"只回查{item.subject}的子任务原件。",
                    "evidence_requirements": "引用原件并保留实际子任务记录和未完成状态。", "expected_files": [_SIDE_PATH], "scope": child_scope}),
                Reply(tool="read_text", arguments={"path": "source.txt", "max_lines": 1000}),
                Reply(text=f"已只读子任务的source.txt。原文：\n{visible.sources[_SIDE_PATH].text.rstrip()}"),
                Reply(status=400)]
        if visible.adapter == "qg5-9":
            return [Reply(tool="read_text", arguments={"path": item.target}), Reply(text="The authorized source was read.")]
        if visible.adapter == "qg5-6":
            return [Reply(tool="read_text", arguments={"path": item.target}),
                Reply(tool="edit_text", arguments={"path": item.target, "old_text": item.before, "new_text": item.after})]
        return [Reply(tool="read_text", arguments={"path": item.target}),
            Reply(tool="edit_text", arguments={"path": item.target, "old_text": item.before, "new_text": item.after}),
            Reply(tool="run_check", arguments={"name": "Independent recovery check"}), Reply(status=400)]
    if visible.adapter == "qg5-6":
        return [Reply(tool="read_text", arguments={"path": item.target}), Reply(text=item.subject + "：本地保存替换前退出，原可靠与待提交记录分别保留；实际核查后沿原Task继续，没有重放已改文件。用户接受仍待定。")]
    if visible.adapter == "qg5-9":
        return []
    if visible.adapter == "qg5-8":
        return [Reply(tool="read_text", arguments={"path": "handoff.txt", "max_lines": 1000}),
            Reply(text=f"{item.subject}：{item.remaining} 本次只读取交接记录，保留原更正和子任务记录。用户接受仍待定。")]
    return [Reply(tool="read_text", arguments={"path": "handoff.txt"}),
        Reply(text=item.subject + "：重开阶段只核对交接记录，保留原更正和独立检查。用户接受仍待定。")]


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
            from tests.support.agent_acceptance.quality_runtime import product_permission_status
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


async def exercise_resume(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    from tests.support.agent_acceptance.frozen import source_material_path
    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression":
        raise SharedControlError("Recovery exercise permits only explicit implementation regression materials.")
    case_id = "quality." + task_id.lower()
    if not builtin_registry(quality_packet=reference).cases[case_id].formal_live:
        raise SharedControlError("This canonical recovery template is not implemented.")
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    records = Path(request["records"])
    permissions = records / "settings/permissions.json"
    permissions.parent.mkdir(parents=True)
    permissions.write_text(json.dumps(request["approval"]), encoding="utf-8")
    review = root / "review-context"
    review.mkdir()
    source_task = next(task for task in packet.tasks if task.task_id == task_id)
    for label, source in (("input", source_task.input_path), ("gold", source_task.gold_path), ("freeze", reference.path)):
        (review / (label + ".original.json")).write_bytes(source_material_path(source).read_bytes())
    context = CaseContext(root, protocol, uuid4().hex, case_id)
    if visible.adapter == "qg5-6":
        return await exercise_unknown(context, visible, request, records)
    if visible.adapter == "qg5-9":
        return await exercise_stopped(context, visible, request, records)
    if visible.adapter == "qg5-8":
        return await exercise_delegated(context, visible, request, records)
    async with ServiceChannel(context, "initial", replies(visible, root, protocol), records=records) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await initial.receive("started")
        await initial.receive("finished", task_id=started["task_id"])
    path = Path(started["path"])
    (root / "original-before.json").write_bytes(path.read_bytes())
    async with ServiceChannel(context, "reopened", replies(visible, root, protocol, "reopen"), records=records) as reopened:
        await reopened.send({"action": "open", "path": str(path)})
        await reopened.receive("conversation")
        open_count = len(json.loads((reopened.root / "exchange.json").read_bytes())["exchanges"])
        await reopened.configure()
        await reopened.send({"action": "verify_recovery", "path": str(path), "task_id": started["task_id"]})
        verified = await reopened.receive("recovery")
        await reopened.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        await reopened.receive("finished", task_id=started["task_id"])
    (root / "recovery-observation.json").write_text(json.dumps({"open_http_attempts": open_count, "can_resume": verified["recovery"]["can_resume"]}), encoding="utf-8")
    saved = json.loads(path.read_bytes())
    exchanges = [json.loads((channel.root / "exchange.json").read_bytes()) for channel in (initial, reopened)]
    result = score(visible, root, [saved], exchanges)
    result.update(real_http_attempts=0, independent_quality_sample_increment=0, formal_execution=False,
        observation_layer="desktop-service-native-sdk-executor", actual_human_review="not_performed")
    return CaseOutcome("passed" if result["passed"] else "failed", result,
        {**initial.evidence(), **reopened.evidence(), "conversation": path, "original": root / "original-before.json"},
        case_id=case_id, protocol=protocol, critical_contracts=result["critical_contracts"])


async def exercise_unknown(context: CaseContext, visible: QualityVisibleInput, request: dict[str, Any], records: Path) -> CaseOutcome:
    root = context.root
    async with ServiceChannel(context, "initial", replies(visible, root, context.protocol), records=records, interrupt_after_edit=True) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await initial.receive("started")
        await initial.process.wait()
    path = Path(started["path"])
    reliable = path.read_bytes()
    pending = path.with_suffix(".json.tmp").read_bytes()
    (root / "original-before.json").write_bytes(reliable)
    (root / "pending-original.json").write_bytes(pending)
    async with ServiceChannel(context, "reopened", replies(visible, root, context.protocol, "reopen"), records=records) as reopened:
        await reopened.send({"action": "open", "path": str(path)})
        opened = await reopened.receive("conversation")
        open_count = len(json.loads((reopened.root / "exchange.json").read_bytes())["exchanges"])
        await reopened.configure()
        await reopened.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        blocked = await reopened.receive("error")
        blocked_count = len(json.loads((reopened.root / "exchange.json").read_bytes())["exchanges"])
        await reopened.send({"action": "import", "path": str(root / "original-before.json")})
        imported = await reopened.receive("conversation")
        await reopened.send({"action": "verify_recovery", "path": imported["path"], "task_id": started["task_id"]})
        unowned = await reopened.receive("recovery")
        await reopened.send({"action": "resume_task", "path": imported["path"], "task_id": started["task_id"]})
        imported_blocked = await reopened.receive("error")
        await reopened.send({"action": "open", "path": str(path)})
        await reopened.receive("conversation")
        await reopened.send({"action": "verify_recovery", "path": str(path), "task_id": started["task_id"]})
        verified = await reopened.receive("recovery")
        await reopened.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        await reopened.receive("finished", task_id=started["task_id"])
    observation = {"open_http_attempts": open_count, "can_resume": verified["recovery"]["can_resume"],
        "planned_local_exit": initial.process.returncode, "reliable_and_pending_differ": reliable != pending,
        "blocked_resume_http_attempts": blocked_count,
        "imported_unknown_blocks_resume": unowned["recovery"]["can_resume"] is False and bool(imported_blocked["message"]),
        "verified_effect": verified["recovery"]["actions"][-1]["effect"], "blocked_reason": blocked["message"],
        "open_effect": opened["recovery"]["actions"][-1]["effect"]}
    (root / "recovery-observation.json").write_text(json.dumps(observation), encoding="utf-8")
    saved = json.loads(path.read_bytes())
    result = score(visible, root, [saved], [json.loads((channel.root / "exchange.json").read_bytes()) for channel in (initial, reopened)])
    result.update(real_http_attempts=0, independent_quality_sample_increment=0, formal_execution=False,
        observation_layer="desktop-service-native-sdk-executor", actual_human_review="not_performed")
    return CaseOutcome("passed" if result["passed"] else "failed", result,
        {**initial.evidence(), **reopened.evidence(), "conversation": path, "original": root / "original-before.json",
            "pending": root / "pending-original.json", "local_exit": initial.root / "planned-local-exit.json", "imported.conversation": Path(imported["path"])},
        case_id=context.case_id, protocol=context.protocol, critical_contracts=result["critical_contracts"])


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


async def exercise_delegated(context: CaseContext, visible: QualityVisibleInput, request: dict[str, Any], records: Path) -> CaseOutcome:
    root = context.root
    async with ServiceChannel(context, "initial", replies(visible, root, context.protocol), records=records, gates=[0]) as initial:
        await initial.configure()
        await initial.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await initial.receive("started")
        await initial.arrived(0)
        await initial.send({"action": "send", "path": started["path"], "prompt": supplement_goal(visible)})
        await initial.receive("supplement", state="received")
        initial.release(0)
        failed = await initial.receive("finished", task_id=started["task_id"])
    path = Path(started["path"])
    (root / "original-before.json").write_bytes(path.read_bytes())
    work = failed["turn"]["delegations"][0]
    child_path = Path(work["record_path"])
    child_bytes = child_path.read_bytes()
    (root / "original-child.json").write_bytes(child_bytes)
    async with ServiceChannel(context, "reopened", replies(visible, root, context.protocol, "reopen"), records=records) as reopened:
        await reopened.send({"action": "open", "path": str(child_path)})
        blocked = await reopened.receive("error")
        await reopened.send({"action": "open", "path": str(path)})
        await reopened.receive("conversation")
        open_count = len(json.loads((reopened.root / "exchange.json").read_bytes())["exchanges"])
        await reopened.configure()
        await reopened.send({"action": "verify_recovery", "path": str(path), "task_id": started["task_id"]})
        verified = await reopened.receive("recovery")
        await reopened.send({"action": "resume_task", "path": str(path), "task_id": started["task_id"]})
        await reopened.receive("finished", task_id=started["task_id"])
    observation = {"open_http_attempts": open_count, "can_resume": verified["recovery"]["can_resume"],
        "applied_supplements": sum(event["event"] == "supplement" and event.get("state") == "applied" for event in initial.events),
        "child_path": str(child_path), "child_sha256": sha256(child_bytes).hexdigest(), "child_resume_blocked_reason": blocked["message"]}
    (root / "recovery-observation.json").write_text(json.dumps(observation), encoding="utf-8")
    result = score(visible, root, [json.loads(path.read_bytes())], [json.loads((channel.root / "exchange.json").read_bytes()) for channel in (initial, reopened)])
    result.update(real_http_attempts=0, independent_quality_sample_increment=0, formal_execution=False,
        observation_layer="desktop-service-native-sdk-executor", actual_human_review="not_performed")
    return CaseOutcome("passed" if result["passed"] else "failed", result,
        {**initial.evidence(), **reopened.evidence(), "conversation": path, "original": root / "original-before.json", "child.conversation": child_path, "original_child": root / "original-child.json"},
        case_id=context.case_id, protocol=context.protocol, critical_contracts=result["critical_contracts"])
