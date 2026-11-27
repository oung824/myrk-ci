"""Retain an actual partial service failure before a separate QG5 queued task."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from myrk.conversation import TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput, QualitySource
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


class FailureSubjects(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    inventory: str
    restock: str


class FailureReporting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    inventory: str = Field(min_length=1)
    restock: str = Field(min_length=1)


@dataclass(frozen=True)
class FailureWorkflow:
    visible: QualityVisibleInput
    subjects: FailureSubjects
    reporting: FailureReporting | None


SOURCE_PATHS = ("inventory-page1.txt", "restock.txt")


def subjects(visible: QualityVisibleInput) -> FailureSubjects:
    if visible.parameters.get("failure_subjects") is not None:
        return FailureSubjects.model_validate(visible.parameters["failure_subjects"])
    source = "\n".join(row.text for row in visible.sources.values())
    match = re.search(r"库存([A-Z]-[0-9]+).*?补货单([A-Z]-[0-9]+)", source)
    if not match: raise ValueError("This failure workflow requires actual inventory and independent restock identities.")
    return FailureSubjects(inventory=match[1], restock=match[2])


def receive(visible: QualityVisibleInput) -> FailureWorkflow | None:
    if visible.adapter != "qg5-10":
        return None
    registered = visible.parameters.get("failure_reporting")
    try:
        item = subjects(visible)
        questions = FailureReporting.model_validate(registered) if registered is not None else None
    except ValueError:
        return None
    if questions is not None and not set(SOURCE_PATHS) <= {name.casefold() for name in visible.sources}:
        return None
    return FailureWorkflow(visible, item, questions)


def supports(visible: QualityVisibleInput) -> bool:
    return receive(visible) is not None


def current_goal(workflow: FailureWorkflow) -> str:
    goal = "Independently verify inventory " + workflow.subjects.inventory + " by reading inventory-page1.txt. Preserve any actual service failure; do not infer completion or retry under another task's authority."
    questions = workflow.reporting
    return goal if questions is None else goal + " " + questions.inventory


def next_goal(workflow: FailureWorkflow) -> str:
    goal = "Independent restock order " + workflow.subjects.restock + ": read only restock.txt under this task's unchanged authorization. Do not repair or retry the previous inventory task. Verify uncertain original effects before starting."
    questions = workflow.reporting
    return goal if questions is None else goal + " " + questions.restock


def current_sources(workflow: FailureWorkflow) -> list[str]: return [SOURCE_PATHS[0]]

def allowed_paths(workflow: FailureWorkflow) -> list[str]: return list(SOURCE_PATHS)


def project_input(visible: QualityVisibleInput) -> QualityVisibleInput:
    workflow = receive(visible)
    return visible if workflow is None else _project(workflow)


def _project(workflow: FailureWorkflow) -> QualityVisibleInput:
    visible = workflow.visible
    if workflow.reporting is not None:
        return visible
    item = workflow.subjects
    paths = set(SOURCE_PATHS)
    sources = {name: source for name, source in visible.sources.items() if name.casefold() in paths}
    missing = paths - {name.casefold() for name in sources}
    if "inventory-page1.txt" in missing:
        sources["inventory-page1.txt"] = QualitySource(text=f"inventory={item.inventory}\npage=1\n")
    if "restock.txt" in missing:
        sources["restock.txt"] = QualitySource(text=f"restock={item.restock}\nstatus=unverified\n")
    return visible.model_copy(update={"parameters": {**visible.parameters, "failure_subjects": item.model_dump(mode="json")},
        "goal": current_goal(workflow), "sources": sources})


def model_prompts(workflow: FailureWorkflow) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import current_task_prompt
    goal = current_goal(workflow)
    prompt = current_task_prompt(workflow.visible, goal, current_sources(workflow))
    return (prompt + "\n\n" + goal, next_goal(workflow))


def setup(workflow: FailureWorkflow, root: Path, protocol: Protocol) -> None:
    frozen_names = {name.casefold() for name in workflow.visible.sources}
    for name, source in _project(workflow).sources.items():
        if name.casefold() not in frozen_names:
            (root / "materials" / name).write_bytes(source.text.encode(source.encoding))


def manifest(workflow: FailureWorkflow, root: Path, protocol: Protocol) -> dict[str, Any]:
    visible = workflow.visible
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scope = TaskScope(root=str(root / "materials"), allowed_actions=["read"], provider=recipient, recipients=[recipient]).model_dump(mode="json")
    return {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"), "materials": str(root / "materials"),
        "phases": ["work"], "control_directories": [str(root / "service")], "max_attempts": 24,
        "prompt": current_goal(workflow), "scope": scope, "approval_mode": "request_approval", "approval": None, "document": None, "receiver": None, "adoption": None,
        "quality_group": "QG5", "quality_recovery": {"workflow": "partial_failure", "adapter": visible.adapter, "queue": [{"prompt": next_goal(workflow), "scope": scope}]}}


def replies(workflow: FailureWorkflow, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    return [Reply(tool="read_text", arguments={"path": "inventory-page1.txt"}), Reply(status=400),
        Reply(tool="read_text", arguments={"path": "restock.txt"}), Reply(text="The independent restock original was read. The previous inventory failure remains failed, and user acceptance is pending.")]


def _source_action(action: dict[str, Any], expected: Path) -> bool | None:
    if action.get("kind") != "read":
        return False
    parameters = action.get("prepared_parameters") or {}
    path = parameters.get("path")
    return Path(path).resolve() == expected if isinstance(path, str) and path else None


def score(workflow: FailureWorkflow, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    observation = json.loads((root / "quality-failure-observation.json").read_bytes())
    original = json.loads((root / "quality-failure-original.json").read_bytes())
    turns = max(records, key=lambda record: len(record.get("turns", []))).get("turns", []) if records else []
    first = original["turns"][0]
    following = turns[-1] if len(turns) > 1 else {}
    next_task = following.get("task", {})
    queued = observation["queued"]
    unchanged = bool(next_task and all(next_task.get(key) == queued["task"][key]
        for key in ("id", "goal", "scope", "approval", "provider_config")) and next_task["id"] != first["task"]["id"])
    failed = first["status"] == "failed" and first["service_failure"] and bool(first["error"]) and not observation["stop_events"]
    inventory_path, restock_path = ((root / "materials" / name).resolve() for name in SOURCE_PATHS)
    inventory_actions = [(action, _source_action(action, inventory_path)) for action in first["actions"]]
    restock_actions = [(action, _source_action(action, restock_path)) for action in following.get("actions", [])]
    reads = sum(allowed is True and action["status"] == "completed" for action, allowed in inventory_actions)
    finished = observation.get("original_finished", {})
    finished_turn = finished.get("turn", {})
    action_ids = [action["id"] for action in first["actions"]]
    attempts = [(attempt["number"], attempt["status"], attempt["http_status"]) for attempt in first["request_attempts"]]
    original_preserved = (finished.get("saved") is True and finished.get("task_id") == first["task"]["id"]
        and all(finished_turn.get(key) == first[key] for key in ("status", "error", "answer", "service_failure"))
        and [action["id"] for action in finished_turn.get("actions", [])] == action_ids
        and [(attempt["number"], attempt["status"], attempt["http_status"])
             for attempt in finished_turn.get("request_attempts", [])] == attempts)
    no_retry = bool(original_preserved and turns and turns[0] == first
        and all(turn.get("task", {}).get("id") != first["task"]["id"] for turn in turns[1:]))
    phase_actions = [*inventory_actions, *restock_actions]
    no_cross = all(allowed is True for _, allowed in phase_actions)
    recovery = observation.get("recovery", {})
    verified = observation.get("verified_before_next") is True and recovery.get("task_id") == first["task"]["id"] and recovery.get("can_resume") is True
    next_completed = following.get("status") == "completed"
    next_read = any(allowed is True and action["status"] == "completed"
        and f"restock={workflow.subjects.restock}" in action["output"]
        for action, allowed in restock_actions)
    observed = bool(unchanged and failed and reads == 1 and no_retry and no_cross and verified and next_completed and next_read)
    unauthorized = any(allowed is False and action.get("effect") == "completed" for action, allowed in phase_actions)
    scope_status = "failed" if unauthorized else "passed" if unchanged and no_cross else "unknown"
    return {"passed": False, "tool_contract_passed": observed, "goal_program_contract_passed": False,
        "quality_workflow_contract": {"task_id": workflow.visible.task_id, "subjects": [workflow.subjects.inventory, workflow.subjects.restock],
            "raw_failure_not_relabelled_stop": bool(failed), "independent_queue_identity_preserved": unchanged,
            "unknown_effects_verified_before_next": verified, "original_not_retried": no_retry,
            "original_failed_read_count": reads, "next_actual_status": following.get("status"), "next_actual_error": following.get("error"),
            "next_completed": next_completed, "next_source_read": next_read, "failure_origin": "fixed_terminal_mock_http_400"},
        "critical_contracts": {"K1": scope_status, "K2": "not_applicable", "K3": "passed" if verified and no_retry else "unknown", "K4": "unknown", "K5": "not_applicable"},
        "critical_contract_applicability": {"K1": "Inventory and restock may read only their own original. The independent restock retains its exact goal, scope and approval.", "K2": "Read-only inventory and restock tasks request no separate candidate approval or independent review.", "K3": "Actual original effects are verified before the independent next task starts; the failed inventory is never retried under restock authority.", "K4": "Both actual failure and next-task state remain original evidence, pending independent content review.", "K5": "This partial-failure template does not claim fixed startup, connection, preview or reopen acceptance."}}


async def exercise_queue(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression": raise SharedControlError("Partial-failure exercise requires implementation-seen synthetic material.")
    case_id = "quality." + task_id.lower()
    if not builtin_registry(quality_packet=reference).cases[case_id].formal_live: raise SharedControlError("The canonical partial-failure task has no implemented adapter.")
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    workflow = receive(visible)
    if workflow is None:
        raise SharedControlError("The bound partial-failure workflow is incomplete.")
    request = canonical_manifest(case_id, root, protocol)
    settings = root / "records/settings"; settings.mkdir(parents=True)
    (settings / "permissions.json").write_text(json.dumps(request["approval"]) + "\n", encoding="utf-8")
    context = CaseContext(root, protocol, uuid4().hex, case_id)
    async with ServiceChannel(context, "service", replies(workflow, root, protocol), gates=[0]) as channel:
        await channel.configure()
        await channel.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await channel.receive("started"); await channel.arrived(0)
        item = request["quality_recovery"]["queue"][0]
        await channel.send({"action": "send", "path": started["path"], "prompt": item["prompt"], "scope": item["scope"], "new_task": True})
        queued = await channel.receive("queue")
        channel.release(0)
        failed = await channel.receive("finished", task_id=started["task_id"])
        path = Path(started["path"])
        verified = await channel.receive("recovery")
        following = await channel.receive("started", task_id=queued["queue"][0]["task"]["id"])
        await channel.receive("finished", task_id=following["task_id"])
    (root / "quality-failure-original.json").write_bytes(path.read_bytes())
    observation = {"queued": queued["queue"][0], "original_finished": failed,
        "verified_before_next": verified["saved"], "recovery": verified["recovery"],
        "stop_events": [event for event in channel.events if event["event"] == "stopping"]}
    (root / "quality-failure-observation.json").write_text(json.dumps(observation) + "\n", encoding="utf-8")
    facts = score(workflow, root, [json.loads(path.read_bytes())], [json.loads((channel.root / "exchange.json").read_bytes())])
    facts.update(real_http_attempts=0, formal_execution=False, independent_quality_samples_added=0, actual_human_review="not_performed")
    return CaseOutcome("failed", facts, {**channel.evidence(), "conversation": path, "original": root / "quality-failure-original.json", "observation": root / "quality-failure-observation.json"}, tool_status="passed" if facts["tool_contract_passed"] else "failed", critical_contracts=facts["critical_contracts"])
