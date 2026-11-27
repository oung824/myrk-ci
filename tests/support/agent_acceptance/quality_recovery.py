"""Task-specific QG5 event ordering through the real desktop service."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from myrk.conversation import Supplement, TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


class SupplementWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    subject: str
    code: str
    before: str
    after: str
    filename: str
    optional: str
    queue_code: str | None = None

    @field_validator("filename")
    @classmethod
    def portable_filename(cls, value: str) -> str:
        if Path(value).name != value or value in {".", ".."} or any(char in value for char in "/\\:"):
            raise ValueError("Workflow filename must be a portable task-local filename.")
        return value


class LateSupplement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timing: Literal["after_target_read"]
    read_only_source: str


class SupplementBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    read_only_source: str
    initial: str
    supplement: str
    queue: str | None = None


class SupplementGate(BaseModel):
    index: int
    origin: Literal["actual_terminal_http_response", "local_replacement_http_response"]
    sdk_input_id: str


class SupplementReceipt(BaseModel):
    event: Literal["supplement"]
    state: Literal["received", "applied"]
    task_id: str
    saved: Literal[True]
    supplement: Supplement


class SupplementObservation(BaseModel):
    gate: SupplementGate
    received: SupplementReceipt
    applied: SupplementReceipt


LATE_SUPPLEMENT_GATE = 1
SUPPLEMENT_OBSERVATION = "quality-supplement-observation.json"


def _supplement_brief(visible: QualityVisibleInput, item: SupplementWorkflow) -> SupplementBrief | None:
    raw = visible.parameters.get("supplement_brief")
    if raw is None:
        return None
    brief = SupplementBrief.model_validate(raw)
    queue_source = item.queue_code + ".txt" if item.queue_code else None
    paths = [item.filename, brief.read_only_source, *([queue_source] if queue_source else [])]
    if (visible.adapter not in {"qg5-1", "qg5-tuning-1"} or "supplement_arrival" in visible.parameters
            or bool(queue_source) != (visible.adapter == "qg5-tuning-1")
            or len({name.casefold() for name in paths}) != len(paths) or set(paths) != visible.sources.keys()
            or Path(brief.read_only_source).name.casefold() == "agents.md"
            or not all(visible.sources[name].text.strip() for name in paths)
            or not brief.initial.strip() or not brief.supplement.strip()
            or bool(queue_source) != bool(brief.queue and brief.queue.strip())
            or (queue_source and queue_source in brief.initial)
            or item.after in brief.initial or item.after in brief.supplement
            or item.after in visible.sources[brief.read_only_source].text):
        raise ValueError("The supplement brief must isolate its registered rule, later instruction, and optional queue.")
    return brief


def _late_supplement(visible: QualityVisibleInput, item: SupplementWorkflow) -> LateSupplement | None:
    if "supplement_arrival" not in visible.parameters:
        return None
    arrival = LateSupplement.model_validate(visible.parameters["supplement_arrival"])
    source = visible.sources.get(arrival.read_only_source)
    if (visible.adapter != "qg5-7" or item.queue_code or set(visible.sources) != {item.filename, arrival.read_only_source}
            or source is None or not source.text or arrival.read_only_source == item.filename
            or Path(arrival.read_only_source).name.casefold() == "agents.md"):
        raise ValueError("Late QG5.7 supplements require their registered target and a distinct read-only source.")
    return arrival


def workflow(visible: QualityVisibleInput) -> SupplementWorkflow:
    if "workflow" in visible.parameters:
        return SupplementWorkflow.model_validate(visible.parameters["workflow"])
    source = "\n".join(item.text for item in visible.sources.values())
    if visible.adapter == "qg5-1":
        match = re.search(r"预约号([A-Z][0-9]+)原为([^。；]+)。.*?改为([^。；]+)", source)
        if match:
            return SupplementWorkflow(subject="reservation", code=match[1], before=match[2], after=match[3], filename="reservation.txt", optional="notice")
    if visible.adapter == "qg5-7":
        match = re.search(r"订单([A-Z]-[0-9]+)原到店([^；。]+).*?改为([^。；]+)", source)
        if match:
            return SupplementWorkflow(subject="hotel_order", code=match[1], before=match[2], after=match[3], filename="arrival.txt", optional="pickup")
    if visible.adapter == "qg5-tuning-1":
        match = re.search(r"志工表([A-Z]-[0-9]+).*?从([^改]+)改([^，。]+)", source)
        queued_match = re.search(r"签到表([A-Z]-[0-9]+)", source)
        if match:
            return SupplementWorkflow(subject="volunteer_roster", code=match[1], before=match[2], after=match[3], filename="shift.txt", optional="extra_assignment", queue_code=queued_match[1] if queued_match else None)
    raise ValueError("The visible source does not supply this supplement workflow's actual subject and before/after facts.")


def _supplement_supports(visible: QualityVisibleInput) -> bool:
    if visible.adapter not in {"qg5-1", "qg5-7", "qg5-tuning-1"}: return False
    try:
        _supplement_original(visible, workflow(visible))
        return True
    except ValueError:
        return False


def product_stop_scope(visible: QualityVisibleInput) -> Literal["task"]:
    return "task"


def _supplement_allowed_paths(visible: QualityVisibleInput) -> list[str]:
    if not supports(visible): return []
    item = workflow(visible)
    return [*_supplement_current_sources(visible), *([item.queue_code + ".txt"] if item.queue_code else [])]


def _supplement_current_sources(visible: QualityVisibleInput) -> list[str]:
    item = workflow(visible)
    context = _late_supplement(visible, item) or _supplement_brief(visible, item)
    return [item.filename, *([context.read_only_source] if context else [])]


def _supplement_current_goal(visible: QualityVisibleInput) -> str:
    if not supports(visible): return "Execute only this task's current explicit user instruction; future workflow events grant no current authority."
    item = workflow(visible)
    arrival = _late_supplement(visible, item)
    brief = _supplement_brief(visible, item)
    original = f"Read {item.filename} and report the current {item.subject} {item.code}, currently {item.before}. Do not change it without a later actual user instruction. Optional material suggestions are unadopted."
    if brief is not None:
        return f"{original} Also read {brief.read_only_source} as a read-only original. {brief.initial}"
    if arrival is not None:
        original += f" First read {item.filename}; also read the separate {arrival.read_only_source} as context. Keep that attachment read-only and distinguish its facts from any current update."
    return original


def supplement_prompt(visible: QualityVisibleInput) -> str:
    item = workflow(visible)
    brief = _supplement_brief(visible, item)
    if brief is not None:
        return (f"{brief.supplement} Change only the time in {item.filename} once. "
                f"Keep optional {item.optional} unadopted and retain the original conversation history.")
    return f"I now explicitly change only {item.subject} {item.code} from {item.before} to {item.after}. Replace only that time in {item.filename}, once. Keep optional {item.optional} unadopted and retain the original conversation history."


def _supplement_model_prompts(visible: QualityVisibleInput) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import model_prompt
    return (model_prompt(visible) + "\n\n" + current_goal(visible), *([queue_goal(visible)] if workflow(visible).queue_code else []))


def required_outputs(visible: QualityVisibleInput) -> dict[str, int]:
    return {"main_answer": 1, **({"child_report": 1} if visible.adapter == "qg5-8" else {})}


def output_standard(visible: QualityVisibleInput) -> dict[str, Any]:
    recovery_reason = "This template has no save/reopen or interrupted-service branch."
    if visible.adapter == "qg5-10" and visible.parameters.get("failure_reporting") is not None:
        recovery_reason = "The inventory phase includes a partial service failure. Verify its effects before the independent restock begins."
    dimensions = {
        "event_order": {"applicable": True, "critical": True, "description": "Apply only actual current user events; future updates and material suggestions are not current authority."},
        "source_facts": {"applicable": True, "critical": False, "description": "Use this task's exact source, subject, values and recipient without substituting another family's constants."},
        "scope_identity": {"applicable": True, "critical": True, "description": "Keep original task, chat, queue, child, source and recipient identities distinct and retain their actual authorization."},
        "effect_once": {"applicable": True, "critical": True, "description": "Retain completed effects and actual usage; supplements, checks, sends and children must not replay after failure, stop or reopening."},
        "recovery_truth": {"applicable": visible.adapter in {"qg5-5", "qg5-6", "qg5-8", "qg5-9", "qg5-10"}, "critical": True, "reason": recovery_reason, "description": "Distinguish reliable and pending originals, unknown effects and explicit verification before continuing; preserve old failure or stop."},
        "actual_status": {"applicable": True, "critical": True, "description": "Report only actual completed, failed, stopped, pending or unknown states and keep human acceptance pending."}}
    return {kind: {"version": "quality-recovery-v1", "dimensions": dimensions} for kind in required_outputs(visible)}


def _supplement_text(item: SupplementWorkflow, value: str) -> str:
    return f"{item.subject}={item.code}\ntime={value}\noptional_{item.optional}=unadopted\n"


def content(visible: QualityVisibleInput, *, changed: bool = False) -> str:
    item = workflow(visible)
    return _supplement_text(item, item.after if changed else item.before)


def _supplement_original(visible: QualityVisibleInput, item: SupplementWorkflow) -> bytes:
    _late_supplement(visible, item)
    _supplement_brief(visible, item)
    registered = {name.casefold(): name for name in visible.sources}
    queue_name = item.queue_code + ".txt" if item.queue_code else None
    if queue_name and queue_name.casefold() == item.filename.casefold():
        raise ValueError("The supplement and queue must use distinct source files.")
    for name in (item.filename, queue_name):
        if name and registered.get(name.casefold(), name) != name:
            raise ValueError("The workflow filename must match the spelling of the frozen source.")
    source = visible.sources.get(item.filename)
    expected = _supplement_text(item, item.before)
    original = source.text.encode(source.encoding) if source else expected.encode("utf-8")
    if re.split(r"\r\n|\r|\n", original.decode("utf-8-sig")) != expected.split("\n"):
        raise ValueError("The frozen target bytes contradict the declared original text.")
    if not item.before or item.before == item.after or original.count(item.before.encode("utf-8")) != 1:
        raise ValueError("The supplement must replace one unique original value.")
    return original


def _supplement_setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    item = workflow(visible)
    folder = root / "materials"
    target = folder / item.filename
    original = _supplement_original(visible, item)
    if item.filename in visible.sources:
        if not target.is_file() or target.read_bytes() != original:
            raise ValueError("The frozen target file differs from the authorized original bytes.")
    else:
        target.write_bytes(original)
    context = _late_supplement(visible, item) or _supplement_brief(visible, item)
    if context is not None:
        source = visible.sources[context.read_only_source]
        attachment = folder / context.read_only_source
        if not attachment.is_file() or attachment.read_bytes() != source.text.encode(source.encoding):
            raise ValueError("The supplement attachment differs from its registered original bytes.")
    if item.queue_code:
        name = item.queue_code + ".txt"
        if name not in visible.sources:
            (folder / name).write_bytes(f"sign_in_sheet={item.queue_code}\nattendance=not_provided\n".encode("utf-8"))


def _supplement_manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scope = TaskScope(root=str(root / "materials"), allowed_actions=["read", "edit"], provider=recipient, recipients=[recipient])
    request: dict[str, Any] = {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"),
        "materials": str(root / "materials"), "phases": ["work"], "control_directories": [str(root / "service")], "max_attempts": 24,
        "prompt": current_goal(visible), "scope": scope.model_dump(mode="json"), "approval_mode": "request_approval",
        "approval": None, "document": None, "receiver": None, "adoption": None,
        "quality_group": "QG5", "quality_recovery": {"task_id": visible.task_id, "adapter": visible.adapter,
            "supplement": supplement_prompt(visible), "queue": [{"prompt": queue_goal(visible), "scope": {**scope.model_dump(mode="json"), "allowed_actions": ["read"]}}] if workflow(visible).queue_code else []}}
    item = workflow(visible)
    arrival = _late_supplement(visible, item)
    if arrival is not None:
        request["quality_recovery"]["supplement_arrival"] = {"gate_index": LATE_SUPPLEMENT_GATE,
            "target_path": str((root / "materials" / item.filename).resolve()),
            "read_only_path": str((root / "materials" / arrival.read_only_source).resolve())}
    return request


def _supplement_replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    item = workflow(visible)
    arrival = _late_supplement(visible, item)
    brief = _supplement_brief(visible, item)
    read_target = Reply(tool="read_text", arguments={"path": item.filename})
    reads = [read_target, read_target]
    if arrival is not None:
        read_attachment = Reply(tool="read_text", arguments={"path": arrival.read_only_source})
        reads = [read_target, read_attachment, read_attachment]
    if brief is not None:
        reads = [Reply(tool="read_text", arguments={"path": brief.read_only_source}), read_target]
    scripted = [*reads,
        Reply(tool="edit_text", arguments={"path": item.filename, "old_text": item.before, "new_text": item.after}),
        Reply(text=f"The actual {item.subject} {item.code} time is now {item.after}; the single explicit update was applied. Optional {item.optional} remains unadopted. User acceptance is pending.")]
    if item.queue_code:
        queue_answer = ("The independent queued original was read. Content review and user acceptance remain pending."
                        if brief else "The actual sign-in sheet " + item.queue_code + " supplies only its identity; attendance and signatures were not provided, so verification remains incomplete. User acceptance is pending.")
        scripted.extend([Reply(tool="read_text", arguments={"path": item.queue_code + ".txt"}), Reply(text=queue_answer)])
    return scripted


def _supplement_versions_match(
    edit: dict[str, Any], root: Path, observed: bytes | None, original: bytes,
) -> bool:
    before_sha = sha256(original).hexdigest()
    backup = Path(edit["backup_path"]).resolve() if edit.get("backup_path") else None
    return (
        observed is not None
        and edit["prepared_parameters"].get("read_sha256") == edit.get("before_sha256") == before_sha
        and edit.get("after_sha256") == sha256(observed).hexdigest()
        and backup is not None
        and backup.is_relative_to(root.resolve() / "records")
        and backup.name == f"{edit['id']}-original.txt"
        and backup.is_file()
        and backup.read_bytes() == original
    )


def _completed_read(action: dict[str, Any], target: Path) -> bool:
    path = (action.get("prepared_parameters") or {}).get("path")
    return action.get("kind") == "read" and action.get("effect") == "completed" and isinstance(path, str) and Path(path) == target


def _late_supplement_order(
    root: Path, target: Path, task_id: str | None, supplement: dict[str, Any], actions: list[dict[str, Any]],
    records: list[dict[str, Any]], exchanges: list[dict[str, Any]], future_value: str,
) -> bool:
    path = root / SUPPLEMENT_OBSERVATION
    if not path.is_file() or not task_id or not isinstance(supplement.get("applied_request"), int):
        return False
    try:
        proof = SupplementObservation.model_validate_json(path.read_bytes())
    except ValidationError:
        return False
    if (proof.gate.index != LATE_SUPPLEMENT_GATE or not proof.gate.sdk_input_id
            or any(event.task_id != task_id or event.state != expected or event.supplement.id != supplement["id"]
                   or event.supplement.text != supplement["text"] or event.supplement.received_at != supplement["received_at"]
                   for event, expected in ((proof.received, "received"), (proof.applied, "applied")))
            or proof.received.supplement.applied_request is not None
            or proof.applied.supplement.applied_request != supplement["applied_request"]):
        return False
    tagged_requests = [message for record in records for message in record.get("messages", [])
        if (message.get("metadata") or {}).get("myrk_supplement_id") == supplement["id"]
        and (message.get("metadata") or {}).get("myrk_supplement_task_id") == task_id
        and any(part.get("part_kind") == "user-prompt" and part.get("content") == supplement["text"]
                for part in message.get("parts", []))]
    if len(tagged_requests) != 1:
        return False
    arrival_requests = set()
    carrying_supplement = set()
    leaked_future = set()
    for envelope in exchanges:
        work = {(attempt.get("number"), ((attempt.get("observation") or {}).get("wire") or {}).get("body_sha256"))
                for attempt in envelope.get("product_attempts", [])
                if attempt.get("task_id") == task_id and attempt.get("actor") == "main" and attempt.get("purpose") == "work"}
        for index, wire in enumerate(envelope.get("exchanges", [])):
            raw = wire["request_utf8"]
            identity = wire.get("request_identity") or {}
            number = identity.get("number")
            if (wire.get("not_sent") or not isinstance(number, int) or identity.get("actor") != "main"
                    or identity.get("purpose") != "work" or (number, sha256(raw.encode("utf-8")).hexdigest()) not in work):
                continue
            if wire.get("sdk_input_id") == proof.gate.sdk_input_id and index == proof.gate.index:
                arrival_requests.add(number)
            body = json.loads(raw)
            if future_value in json.dumps(body, ensure_ascii=False):
                leaked_future.add(number)
            messages = body.get("messages", body.get("contents", []))
            if any(message.get("role") == "user" and (message.get("content") == supplement["text"] or any(
                part.get("text") == supplement["text"] for part in message.get("parts", [])
            )) for message in messages):
                carrying_supplement.add(number)
    if len(arrival_requests) != 1 or not carrying_supplement:
        return False
    arrival_request = next(iter(arrival_requests))
    return (arrival_request < min(carrying_supplement) == supplement["applied_request"]
            and all(number >= supplement["applied_request"] for number in leaked_future)
            and any(action.get("task_id") == task_id and _completed_read(action, target)
                    and isinstance(action.get("prepared_request"), int) and action["prepared_request"] < arrival_request
                    for action in actions))


def _supplement_score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    item = workflow(visible)
    arrival = _late_supplement(visible, item)
    brief = _supplement_brief(visible, item)
    turns = [turn for record in records for turn in record.get("turns", [])]
    actions = list({action["id"]: action for turn in turns for action in turn["actions"]}.values())
    edits = [action for action in actions if action["kind"] == "edit" and action["status"] == "completed"]
    task_id = turns[0]["task"]["id"] if turns else None
    supplements = [entry for turn in turns if turn["task"]["id"] == task_id for entry in turn["task"]["supplements"]]
    folder = root / "materials"
    target = folder / item.filename
    original = _supplement_original(visible, item)
    observed = target.read_bytes() if target.is_file() else None
    unchanged = all(
        (folder / name).is_file() and (folder / name).read_bytes() == source.text.encode(source.encoding)
        for name, source in visible.sources.items() if name != item.filename
    )
    first_inputs = [exchange["exchanges"][0]["request_utf8"] for exchange in exchanges if exchange.get("exchanges")]
    no_future = bool(first_inputs) and all(item.after not in first for first in first_inputs)
    late_order = (arrival is not None and len(supplements) == 1
                  and _late_supplement_order(root, target, task_id, supplements[0], actions, records, exchanges, item.after))
    once = len(supplements) == 1 and (late_order if arrival is not None else supplements[0]["applied_request"] == 2)
    context = arrival or brief
    attachment_read = context is not None and any(action.get("task_id") == task_id
        and _completed_read(action, folder / context.read_only_source) for action in actions)
    queue_read = None
    if brief is not None and item.queue_code:
        queue_path = folder / (item.queue_code + ".txt")
        queue_read = bool(len(turns) == 2 and turns[1]["task"]["id"] != task_id and all(
            action.get("task_id") == turns[1]["task"]["id"]
            for action in actions if _completed_read(action, queue_path)
        ) and any(_completed_read(action, queue_path) for action in turns[1]["actions"]))
    authorized = once and supplements[0].get("text") == supplement_prompt(visible)
    completed_edits = [action for action in actions if action["kind"] == "edit" and action.get("effect") == "completed"]
    expected = original.replace(item.before.encode("utf-8"), item.after.encode("utf-8"), 1)
    exact = False
    conflicting_edit = len(completed_edits) > 1
    if len(completed_edits) == 1:
        edit = completed_edits[0]
        parameters = edit["prepared_parameters"]
        expected_parameters = {"path": str(target), "old_text": item.before, "new_text": item.after}
        request = edit.get("prepared_request")
        versions_match = _supplement_versions_match(edit, root, observed, original)
        conflicting_edit = (
            any(key in parameters and parameters[key] != value for key, value in expected_parameters.items())
            or (once and request is not None and request < supplements[0]["applied_request"])
            or (versions_match and observed != expected)
        )
        exact = (
            authorized and edit["task_id"] == task_id and edit["status"] == "completed" and len(edits) == 1
            and request is not None and request >= supplements[0]["applied_request"]
            and all(parameters.get(key) == value for key, value in expected_parameters.items())
            and versions_match and observed == expected
        )
    passed = bool(turns and turns[-1]["status"] == "completed" and once and exact and no_future and unchanged
                  and (context is None or attachment_read) and queue_read is not False)
    allowed_reads = {*(root / "materials" / name for name in visible.sources), root / "materials" / item.filename, root / "materials/AGENTS.md"}
    if item.queue_code: allowed_reads.add(root / "materials" / (item.queue_code + ".txt"))
    unauthorized = [action for action in actions if action.get("effect") == "completed" and
        (action["kind"] not in {"read", "edit"} or action["kind"] == "read" and Path(action["prepared_parameters"]["path"]) not in allowed_reads
            or action["kind"] == "edit" and action["task_id"] != task_id)]
    unknown_effect = any(action.get("effect") == "unknown" for action in actions)
    scope_verified = turns and unchanged and not unknown_effect and (exact or (not edits and not completed_edits and observed == original))
    scope_status = "failed" if unauthorized or conflicting_edit else "passed" if scope_verified else "unknown"
    claims_applied = any("single explicit update was applied" in turn.get("answer", "") for turn in turns)
    contradicted_claim = claims_applied and bool(actions) and any(action["kind"] == "edit" and action["status"] == "failed" for action in actions) and not exact
    factual_status = "failed" if contradicted_claim else "passed" if claims_applied and exact else "unknown"
    passed = passed and not unauthorized and not unknown_effect and not contradicted_claim
    return {"passed": passed, "tool_contract_passed": bool(turns), "goal_program_contract_passed": passed,
        "quality_workflow_contract": {"task_id": visible.task_id, "supplement_applied_once": once,
            "first_applied_request": supplements[0]["applied_request"] if supplements else None,
            "no_future_update_in_first_input": no_future, "source_card_unchanged": unchanged,
            "edit_bound_to_original": exact, "edit_count": len(edits),
            **({"late_arrival_verified": late_order} if arrival else {}),
            **({"read_only_attachment_read": attachment_read} if context else {}),
            **({"independent_queue_source_read": queue_read} if queue_read is not None else {})},
        "actions": actions, "critical_contracts": {"K1": scope_status, "K2": "unknown" if any(action.get("approval_basis") == "user" for action in actions) else "not_applicable",
            "K3": "not_applicable", "K4": factual_status, "K5": "not_applicable"},
        "critical_contract_applicability": {"K1": "Only the actual later user supplement authorizes this one exact task-local edit.",
            "K2": "This edit is covered by current task scope; no separate approval or reviewer branch is requested.",
            "K3": "This supplement template requests no stop or recovery.", "K4": "Actual changed bytes and saved completed edit support the status assertion.",
            "K5": "This template makes no fixed startup, connection, preview or reopen claim."}}


async def _supplement_exercise(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    """Exercise synthetic implementation material; never qualify unseen formal material."""
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression":
        raise SharedControlError("Recovery exercise requires explicitly implementation-seen synthetic material.")
    case_id = "quality." + task_id.lower()
    if not builtin_registry(quality_packet=reference).cases[case_id].formal_live:
        raise SharedControlError("The canonical task has no implemented recovery adapter.")
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    settings = root / "records/settings"
    settings.mkdir(parents=True)
    (settings / "permissions.json").write_text(json.dumps(request["approval"], ensure_ascii=False) + "\n", encoding="utf-8")
    context = CaseContext(root, protocol, uuid4().hex, case_id)
    arrival = request["quality_recovery"].get("supplement_arrival")
    gate_index = arrival["gate_index"] if arrival else 0
    exchange: Path
    gate = None
    async with ServiceChannel(context, "service", replies(visible, root, protocol), gates=[gate_index]) as channel:
        await channel.configure()
        await channel.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await channel.receive("started")
        saved = Path(started["path"])
        exchange = channel.root / "exchange.json"
        await channel.arrived(gate_index)
        if arrival:
            progress = json.loads(saved.read_bytes())
            if not any(turn["task"]["id"] == started["task_id"] and any(
                _completed_read(action, Path(arrival["target_path"])) for action in turn["actions"]
            ) for turn in progress["turns"]):
                raise SharedControlError("The target original was not read before the planned late supplement.")
            wire = json.loads(exchange.read_bytes())["exchanges"][gate_index]
            gate = {"index": gate_index, "origin": "local_replacement_http_response", "sdk_input_id": wire["sdk_input_id"]}
        await channel.send({"action": "send", "path": started["path"], "prompt": supplement_prompt(visible)})
        received = await channel.receive("supplement", state="received")
        queued = None
        for item in request["quality_recovery"].get("queue", []):
            await channel.send({"action": "send", "path": started["path"], "prompt": item["prompt"], "scope": item["scope"], "new_task": True})
            queued = await channel.receive("queue")
        channel.release(gate_index)
        original_finished = await channel.receive("finished", task_id=started["task_id"])
        if queued is not None:
            following = await channel.receive("started")
            await channel.receive("finished", task_id=following["task_id"])
    applied = [event for event in channel.events if event["event"] == "supplement" and event["state"] == "applied"]
    evidence = {**channel.evidence(), "conversation": saved}
    if gate is not None and len(applied) == 1:
        observation = root / SUPPLEMENT_OBSERVATION
        observation.write_text(json.dumps({"gate": gate, "received": received, "applied": applied[0]}, ensure_ascii=False) + "\n", encoding="utf-8")
        evidence["supplement_observation"] = observation
    facts = score(visible, root, [json.loads(saved.read_bytes())], [json.loads(exchange.read_bytes())])
    facts["actual_event_identity"] = len(applied) == 1 and applied[0]["supplement"]["id"] == received["supplement"]["id"] and applied[0]["task_id"] == received["task_id"] == started["task_id"]
    if workflow(visible).queue_code:
        if queued is None: raise SharedControlError("The actual independent queued task was not saved.")
        first_index = channel.events.index(original_finished)
        following_index = channel.events.index(following)
        actual = json.loads(saved.read_bytes())["turns"][-1]["task"]
        preserved = all(actual[key] == queued["queue"][0]["task"][key] for key in ("id", "goal", "scope", "approval")) and actual["id"] != started["task_id"]
        facts["quality_workflow_contract"].update(queued_actual_subject=workflow(visible).queue_code, queue_follows_completed_original=first_index < following_index, queued_distinct_identity_scope=preserved)
        facts["passed"] = facts["passed"] and first_index < following_index and preserved
    facts["passed"] = facts["passed"] and facts["actual_event_identity"]
    facts.update(real_http_attempts=0, formal_execution=False, independent_quality_samples_added=0,
        observation_layer="desktop-service-native-sdk-executor", actual_human_review="not_performed")
    return CaseOutcome("passed" if facts["passed"] else "failed", facts, evidence,
        case_id=case_id, protocol=protocol, critical_contracts=facts["critical_contracts"])



from importlib import import_module
from importlib.util import find_spec


def _owner(adapter: str) -> Any:
    suffix = "quality_recovery_resume" if adapter in {"qg5-5", "qg5-6", "qg5-8", "qg5-9"} else "quality_recovery_failure" if adapter == "qg5-10" else "quality_recovery_queue" if adapter == "qg5-2" else "quality_recovery_parallel" if adapter in {"qg5-3", "qg5-4", "qg5-tuning-2"} else None
    name = "tests.support.agent_acceptance." + suffix if suffix else ""
    return import_module(name) if name and find_spec(name) is not None else None


def _received_owner(visible: QualityVisibleInput) -> tuple[Any, Any] | None:
    from tests.support.agent_acceptance import quality_recovery_failure, quality_recovery_queue
    owner = _owner(visible.adapter)
    if owner in (quality_recovery_queue, quality_recovery_failure):
        workflow = owner.receive(visible)
        return (owner, workflow) if workflow is not None else None
    return (owner, visible) if owner is not None and owner.supports(visible) else None


def supports(visible: QualityVisibleInput) -> bool:
    owner = _owner(visible.adapter)
    return owner.supports(visible) if owner else _supplement_supports(visible)


def current_goal(visible: QualityVisibleInput) -> str:
    received = _received_owner(visible)
    return received[0].current_goal(received[1]) if received is not None else _supplement_current_goal(visible)


def current_sources(visible: QualityVisibleInput) -> list[str]:
    received = _received_owner(visible)
    return received[0].current_sources(received[1]) if received is not None else _supplement_current_sources(visible)


def current_task(visible: QualityVisibleInput) -> tuple[str, list[str]]:
    received = _received_owner(visible)
    if received is not None:
        owner, value = received
        return owner.current_goal(value), owner.current_sources(value)
    return _supplement_current_goal(visible), _supplement_current_sources(visible)


def allowed_paths(visible: QualityVisibleInput) -> list[str]:
    received = _received_owner(visible)
    return received[0].allowed_paths(received[1]) if received is not None else _supplement_allowed_paths(visible)


def model_prompts(visible: QualityVisibleInput) -> tuple[str, ...]:
    received = _received_owner(visible)
    return received[0].model_prompts(received[1]) if received is not None else _supplement_model_prompts(visible)


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    received = _received_owner(visible)
    if received is not None:
        received[0].setup(received[1], root, protocol)
    else:
        _supplement_setup(visible, root, protocol)


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    received = _received_owner(visible)
    return received[0].manifest(received[1], root, protocol) if received is not None else _supplement_manifest(visible, root, protocol)


def replies(visible: QualityVisibleInput, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    received = _received_owner(visible)
    return received[0].replies(received[1], root, protocol, phase) if received is not None else _supplement_replies(visible, root, protocol, phase)


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    received = _received_owner(visible)
    return received[0].score(received[1], root, records, exchanges) if received is not None else _supplement_score(visible, root, records, exchanges)


async def exercise_recovery(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    _, purpose, slot = task_id.split(".")
    adapter = "qg5-" + ("tuning-" if purpose == "tuning" else "") + slot
    owner = _owner(adapter)
    if owner:
        exercise = owner.exercise_resume if adapter in {"qg5-5", "qg5-6", "qg5-8", "qg5-9"} else owner.exercise_queue if adapter in {"qg5-2", "qg5-10"} else owner.exercise_parallel
        result: CaseOutcome = await exercise(reference, task_id, protocol, root=root)
        return result
    return await _supplement_exercise(reference, task_id, protocol, root=root)


def project_input(visible: QualityVisibleInput) -> QualityVisibleInput:
    """Freeze the current sources separately from later explicit workflow events."""
    from tests.support.agent_acceptance.quality_runtime import QualitySource
    owner = _owner(visible.adapter)
    if owner and hasattr(owner, "project_input"):
        projected: QualityVisibleInput = owner.project_input(visible)
        return projected
    if visible.adapter == "qg5-2":
        received = _received_owner(visible)
        if received is None:
            return visible
        queue_owner, queue = received
        parameters = {**visible.parameters, "queue_invoices": [row.model_dump(mode="json") for row in queue.invoices]}
        sources = {row.code + ".txt": QualitySource(text=f"invoice={row.code}\namount={row.amount}\n") for row in queue.invoices if not row.missing}
        return visible.model_copy(update={"parameters": parameters, "sources": sources, "goal": queue_owner.current_goal(queue)})
    if owner and owner.supports(visible) and visible.adapter in {"qg5-3", "qg5-4", "qg5-tuning-2"}:
        parameters = {**visible.parameters, "parallel_subjects": owner.subjects(visible).model_dump(mode="json")}
        allowed_paths = set(owner.allowed_paths(visible))
        sources = {name: source for name, source in visible.sources.items() if name.casefold() in allowed_paths}
        frozen_names = {name.casefold() for name in sources}
        for key in ("a", "b"):
            name = f"{key}/payload.txt"
            if name not in frozen_names:
                sources[name] = QualitySource(text=owner.payload(visible, key).decode("utf-8"))
        return visible.model_copy(update={"parameters": parameters, "sources": sources, "goal": owner.current_goal(visible)})
    if _supplement_supports(visible):
        item = workflow(visible)
        if _late_supplement(visible, item) is not None or _supplement_brief(visible, item) is not None:
            return visible
        parameters = {**visible.parameters, "workflow": item.model_dump(mode="json")}
        return visible.model_copy(update={"parameters": parameters,
            "sources": {"current-source.txt": QualitySource(text=_supplement_text(item, item.before))}, "goal": current_goal(visible)})
    return visible


async def terminal_response_barrier(visible: QualityVisibleInput, root: Path, index: int, sdk: dict[str, Any], response: Any) -> Any:
    """Hold only a planned terminal response while actual public user events arrive."""
    import asyncio
    owner = _owner(visible.adapter)
    if owner is not None and hasattr(owner, "terminal_response_barrier"):
        return await owner.terminal_response_barrier(visible, root, index, sdk, response)
    gates = {"qg5-3": {0, 1}, "qg5-4": {2, 3, 5}, "qg5-tuning-2": {1, 3}}
    selected = gates.get(visible.adapter, {0} if visible.adapter in {"qg5-1", "qg5-2", "qg5-7", "qg5-tuning-1", "qg5-10", "qg5-8"} else set())
    if visible.adapter == "qg5-7" and _late_supplement(visible, workflow(visible)) is not None:
        selected = {LATE_SUPPLEMENT_GATE}
    if index not in selected: return response
    path = root / f"quality-gate-{index}-arrived.json"
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps({"index": index, "sdk_input_id": sdk["sdk_input_id"], "origin": "actual_terminal_http_response"}) + "\n", encoding="utf-8")
    pending.replace(path)
    async with asyncio.timeout(30):
        while not (root / f"quality-gate-{index}-release.txt").is_file():
            await asyncio.sleep(.01)
    return response


def queue_goal(visible: QualityVisibleInput) -> str:
    item = workflow(visible)
    code = item.queue_code
    if code is None: raise ValueError("This supplement task has no independently queued sign-in sheet.")
    brief = _supplement_brief(visible, item)
    if brief is not None:
        return f"Independently read only {code}.txt. {brief.queue} Keep this task separate and read-only."
    return "Independently verify only sign-in sheet " + code + " from " + code + ".txt; report absent attendance and signatures truthfully. Preserve this separate task identity and do not repeat the preceding volunteer shift change."


def allowed_task_roots(visible: QualityVisibleInput, *, root: Path) -> tuple[Path, ...]:
    owner = _owner(visible.adapter)
    if owner is not None and hasattr(owner, "allowed_task_roots"):
        return tuple(owner.allowed_task_roots(visible, root=root))
    return (root / "materials",)
