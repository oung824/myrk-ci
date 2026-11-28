"""Real local save/reopen recovery for distinct frozen QG5 tasks."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_recovery_resume_inputs import (
    ResumeFacts as ResumeFacts,
    allowed_paths as allowed_paths,
    child_goal as child_goal,
    current_goal as current_goal,
    current_sources as current_sources,
    delegated_target_source as delegated_target_source,
    facts as facts,
    handoff_text as handoff_text,
    manifest as manifest,
    model_prompts as model_prompts,
    output_standard as output_standard,
    project_input as project_input,
    replies as replies,
    required_outputs as required_outputs,
    setup as setup,
    supplement_goal as supplement_goal,
    supports as supports,
)
from tests.support.agent_acceptance.quality_recovery_resume_scoring import recorded_read_status as recorded_read_status, score as score
from tests.support.agent_acceptance.quality_recovery_resume_stopped import (
    exercise_stopped as exercise_stopped,
    hold_stop_checkpoint as hold_stop_checkpoint,
    score_stopped as score_stopped,
)
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.service_channel import ServiceChannel


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
