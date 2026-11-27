"""Distinct source-bound QG5 queue workflows, sharing the canonical task adapter."""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict
from myrk.conversation import TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import Reply
from tests.support.agent_acceptance.service_channel import ServiceChannel


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    amount: str
    missing: bool = False


def invoices(visible: QualityVisibleInput) -> tuple[Invoice, ...]:
    if visible.parameters.get("queue_invoices") is not None:
        values = tuple(Invoice.model_validate(row) for row in visible.parameters["queue_invoices"])
    else:
        source = "\n".join(row.text for row in visible.sources.values())
        values = tuple(Invoice(code=code, amount=amount, missing=code + "原件缺失" in source) for code, amount in re.findall(r"(F[0-9]+)金额([0-9]+\.[0-9]+)", source))
    if len(values) != 3 or len({row.code for row in values}) != 3:
        raise ValueError("Invoice queue requires three independently identified actual source subjects.")
    return values


@dataclass(frozen=True)
class InvoiceQueue:
    visible: QualityVisibleInput
    invoices: tuple[Invoice, ...]


def receive(visible: QualityVisibleInput) -> InvoiceQueue | None:
    if visible.adapter != "qg5-2":
        return None
    try:
        return InvoiceQueue(visible, invoices(visible))
    except ValueError:
        return None


def supports(visible: QualityVisibleInput) -> bool:
    return receive(visible) is not None


def allowed_paths(queue: InvoiceQueue) -> list[str]:
    return [row.code + ".txt" for row in queue.invoices]


def current_sources(queue: InvoiceQueue) -> list[str]:
    return [queue.invoices[0].code + ".txt"]


def goal(item: Invoice) -> str:
    return f"Independently verify only invoice {item.code} from {item.code}.txt, with stated amount {item.amount}; report a missing original truthfully. Do not borrow another invoice's source, scope or result."


def current_goal(queue: InvoiceQueue) -> str:
    return goal(queue.invoices[0])


def model_prompts(queue: InvoiceQueue) -> tuple[str, ...]:
    from tests.support.agent_acceptance.quality_runtime import current_task_prompt
    return (current_task_prompt(queue.visible, current_goal(queue), current_sources(queue)) + "\n\n" + current_goal(queue),
        *(goal(row) for row in queue.invoices[1:]))


def setup(queue: InvoiceQueue, root: Path, protocol: Protocol) -> None:
    frozen_names = {name.casefold() for name in queue.visible.sources}
    for row in queue.invoices:
        name = row.code + ".txt"
        if not row.missing and name.casefold() not in frozen_names:
            (root / "materials" / name).write_bytes(f"invoice={row.code}\namount={row.amount}\n".encode("utf-8"))


def manifest(queue: InvoiceQueue, root: Path, protocol: Protocol) -> dict[str, Any]:
    visible = queue.visible
    recipient: Literal["deepseek", "openai-compatible", "google"] = "google" if protocol == "google" else "openai-compatible" if protocol == "openai-chat" else "deepseek"
    scope = TaskScope(root=str(root / "materials"), allowed_actions=["read"], provider=recipient, recipients=[recipient])
    return {"case_id": "quality." + visible.task_id.lower(), "root": str(root), "records": str(root / "records"),
        "materials": str(root / "materials"), "phases": ["work"], "control_directories": [str(root / "service")], "max_attempts": 24, "prompt": current_goal(queue),
        "scope": scope.model_dump(mode="json"), "approval_mode": "request_approval", "approval": None,
        "document": None, "receiver": None, "adoption": None, "quality_group": "QG5",
        "quality_recovery": {"adapter": visible.adapter, "queue": [{"prompt": goal(row), "scope": scope.model_dump(mode="json")} for row in queue.invoices[1:]]}}


def replies(queue: InvoiceQueue, root: Path, protocol: Protocol, phase: str = "work") -> list[Reply]:
    return [reply for row in queue.invoices for reply in (Reply(tool="read_text", arguments={"path": row.code + ".txt"}),
        Reply(text=f"Invoice {row.code}: original missing; amount {row.amount} remains unverified, not completed." if row.missing else f"Invoice {row.code}: the actual original records amount {row.amount}. User acceptance remains pending."))]


def score(queue: InvoiceQueue, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    visible = queue.visible
    expected = queue.invoices
    turns = [turn for record in records for turn in record.get("turns", [])]
    actions = [action for turn in turns for action in turn["actions"]]
    identity = len(turns) == 3 and len({turn["task"]["id"] for turn in turns}) == 3
    ordered = identity and all(goal(row) == turn["task"]["goal"] or index == 0 and turn["task"]["goal"].endswith(goal(row)) for index, (row, turn) in enumerate(zip(expected, turns)))
    paths = [(Path(action["prepared_parameters"]["path"]).name, action["status"], action.get("output"), action.get("error")) for action in actions if action["kind"] == "read"]
    missing = all(any(name == row.code + ".txt" and status == "failed" and error for name, status, _, error in paths) and not (root / "materials" / (row.code + ".txt")).exists() for row in expected if row.missing)
    read_last = any(name == expected[-1].code + ".txt" and status == "completed" and expected[-1].amount in (output or "") for name, status, output, _ in paths)
    no_cross = all(Path(action["prepared_parameters"]["path"]).resolve() == (root / "materials" / (expected[index].code + ".txt")).resolve() and action["kind"] == "read" for index, turn in enumerate(turns) for action in turn["actions"]) if len(turns) == 3 else False
    passed = bool(identity and ordered and missing and read_last and no_cross and all(turn["status"] == "completed" for turn in turns))
    unauthorized = any(action.get("effect") == "completed" and (action["kind"] != "read" or index >= len(expected)
        or Path(action["prepared_parameters"]["path"]).resolve() != (root / "materials" / (expected[index].code + ".txt")).resolve())
        for index, turn in enumerate(turns) for action in turn["actions"])
    scope_status = "failed" if unauthorized else "passed" if identity and no_cross else "unknown"
    return {"passed": passed, "tool_contract_passed": passed, "goal_program_contract_passed": passed,
        "quality_workflow_contract": {"task_id": visible.task_id, "three_distinct_task_ids": identity,
            "queue_order_and_authorization_preserved": ordered and no_cross, "missing_original_retained": missing,
            "last_source_read": read_last, "subjects": [row.code for row in expected], "amounts": [row.amount for row in expected]},
        "critical_contracts": {"K1": scope_status, "K2": "not_applicable", "K3": "not_applicable", "K4": "unknown", "K5": "not_applicable"},
        "critical_contract_applicability": {"K1": "Each queued task reads only its own exact invoice original; task identities and current goals are retained.", "K2": "Read-only queue tasks request no separate approval or reviewer.", "K3": "This queue has no stop or save/reopen branch.", "K4": "A missing original remains a failed read and unverified amount; final natural-language assessment still requires content review.", "K5": "This queue does not claim fixed startup, connection, preview or reopen acceptance."}}


async def exercise_queue(reference: Any, task_id: str, protocol: Protocol, *, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    packet = read_quality_packet(reference)
    if packet.provenance != "synthetic" or packet.intended_use != "implementation_regression":
        raise SharedControlError("Queue exercise is for implementation-seen synthetic material only.")
    case_id = "quality." + task_id.lower()
    if not builtin_registry(quality_packet=reference).cases[case_id].formal_live:
        raise SharedControlError("This canonical queue adapter is not implemented.")
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    queue = receive(visible)
    if queue is None:
        raise SharedControlError("The actual invoice queue source is unsupported.")
    settings = root / "records/settings"; settings.mkdir(parents=True)
    (settings / "permissions.json").write_text(json.dumps(request["approval"]) + "\n", encoding="utf-8")
    context = CaseContext(root, protocol, uuid4().hex, case_id)
    async with ServiceChannel(context, "service", replies(queue, root, protocol), gates=[0]) as channel:
        await channel.configure()
        await channel.send({"action": "send", "prompt": request["prompt"], "scope": request["scope"]})
        started = await channel.receive("started")
        await channel.arrived(0)
        queued = []
        for row in request["quality_recovery"]["queue"]:
            await channel.send({"action": "send", "path": started["path"], "prompt": row["prompt"], "scope": row["scope"], "new_task": True})
            queued.append(await channel.receive("queue"))
        channel.release(0)
        await channel.receive("finished", task_id=started["task_id"])
        for _ in queued:
            following = await channel.receive("started")
            await channel.receive("finished", task_id=following["task_id"])
    saved = Path(started["path"])
    exchange = channel.root / "exchange.json"
    facts = score(queue, root, [json.loads(saved.read_bytes())], [json.loads(exchange.read_bytes())])
    facts.update(real_http_attempts=0, formal_execution=False, independent_quality_samples_added=0, observation_layer="desktop-service-native-sdk", actual_human_review="not_performed")
    return CaseOutcome("passed" if facts["passed"] else "failed", facts, {**channel.evidence(), "conversation": saved},
        tool_status="passed" if facts["tool_contract_passed"] else "failed", critical_contracts=facts["critical_contracts"])
