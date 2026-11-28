"""Native governance execution and stop evidence from public observations."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx2
from pydantic_ai import CancellationToken

from myrk.actions import Action
from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from tests.support.agent_acceptance.core import CaseOutcome, Protocol, SharedControlError, StopStatus
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_governance_inputs import DELEGATED, source_sum
from tests.support.agent_acceptance.quality_governance_stop_target import matches_governance_stop_target
from tests.support.agent_acceptance.quality_packet import QualityPacketRef, read_quality_packet
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import OfflineSession
from tests.support.agent_acceptance.stop_request_evidence import (
    IDENTITY_FIELDS,
    TERMINAL_STATES,
    StopExchange,
    StopSavedRequest,
    positive_number,
)


async def terminal_response_barrier(visible: QualityVisibleInput, root: Path, index: int,
                                    sdk: dict[str, Any], response: httpx2.Response) -> httpx2.Response:
    if visible.adapter != "qg3-9" or not matches_governance_stop_target(root, root / visible.parameters["candidate"], sdk):
        return response
    arrived = root / "governance-stop-arrived.json"
    if arrived.exists():
        raise SharedControlError("Governance stop target evidence is ambiguous: the target was already selected.")
    pending = arrived.with_suffix(".tmp")
    pending.write_text(json.dumps({"sdk_input_id": sdk["sdk_input_id"]}), encoding="utf-8")
    pending.replace(arrived)
    release = root / "governance-stop-release.txt"
    async with asyncio.timeout(30):
        while not release.is_file():
            await asyncio.sleep(.025)
    return response


def read_stop_original(path: Path) -> object | None:
    try:
        original: object = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return original


def read_stop_state(path: Path) -> dict[str, Any]:
    state = read_stop_original(path)
    if not isinstance(state, dict):
        return {}
    decisions = state.get("approval_decisions", [])
    fields = {"action_id", "approval_id", "task_id", "target", "prepared_parameters", "approved"}
    if not isinstance(decisions, list) or not all(
        isinstance(item, dict) and fields <= item.keys() and isinstance(item["action_id"], str)
        for item in decisions
    ):
        return {**state, "approval_decisions": []}
    return state


def refusal_stop_status(root: Path, state: dict[str, Any], exchanges: list[dict[str, Any]], task_id: str,
                        turn: dict[str, Any], actions: list[dict[str, Any]],
                        denial: dict[str, Any] | None, *, stop_exchange: StopExchange | None = None) -> tuple[StopStatus, StopStatus]:
    inprocess = state.get("entry") == "public_conversation"
    event_kind = "conversation_cancellation" if inprocess else "stopping"
    public = state.get("action_events") if inprocess else read_stop_original(root / "work/receiver/events.json")
    if not isinstance(public, list):
        return "unknown", "unknown"
    stops = [(index, item) for index, item in enumerate(public)
             if isinstance(item, dict) and item.get("event") == event_kind and item.get("task_id") == task_id]
    if len(stops) != 1:
        return "unknown", "unknown"
    stop_index, event = stops[0]
    path = event.get("path")
    if not isinstance(path, str) or not path or (inprocess and event != state.get("stop_event")):
        return "unknown", "unknown"

    request_status = refusal_request_status(root, exchanges, event, turn, inprocess, stop_exchange=stop_exchange)
    started: set[str] = set()
    effects: set[str] = set()
    complete = True
    denied_before_stop = False
    cancellation_saved = inprocess
    for index, item in enumerate(public):
        if not isinstance(item, dict) or not isinstance(kind := item.get("event"), str):
            complete = False
            continue
        if kind not in {"action", "finished"}:
            continue
        if not isinstance(item.get("path"), str) or not isinstance(item.get("task_id"), str):
            complete = False
            continue
        if item["path"] != path:
            continue
        event_for_task = item["task_id"] == task_id
        if kind == "finished":
            finished_turn = item.get("turn")
            if not isinstance(finished_turn, dict):
                complete &= not event_for_task
                continue
            finished_task = finished_turn.get("task")
            turn_for_task = isinstance(finished_task, dict) and finished_task.get("id") == task_id
            if event_for_task != turn_for_task:
                complete = False
                continue
            if turn_for_task and index > stop_index and item.get("saved") is True:
                cancellation_saved |= finished_turn.get("status") == "cancelled"
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            complete &= not event_for_task
            continue
        action_for_task = action.get("task_id") == task_id
        if event_for_task != action_for_task:
            complete = False
            continue
        if not action_for_task:
            continue
        if not isinstance(action_id := action.get("id"), str) or not action_id:
            complete = False
            continue
        status = action.get("status")
        if status == "started":
            if index > stop_index:
                return request_status, "failed"
            started.add(action_id)
        elif status not in ("awaiting_approval", "completed", "denied", "failed", "conflict", "cancelled"):
            complete = False
        effect = action.get("effect")
        if effect in ("unknown", "completed"):
            effects.add(action_id)
        elif effect != "not_started":
            complete = False
        if index < stop_index and status == "denied" and denial is not None:
            denied_before_stop |= action_id == denial["action_id"] and action.get("approval_id") == denial["approval_id"]

    saved = {action["id"] for action in actions if action.get("task_id") == task_id
             and (action["status"] == "started" or action["effect"] != "not_started")}
    if complete and denied_before_stop and cancellation_saved and effects | saved <= started:
        return request_status, "passed"
    return request_status, "unknown"


def refusal_request_status(root: Path, exchanges: list[dict[str, Any]], stop_event: dict[str, Any],
                           turn: dict[str, Any], inprocess: bool, *, stop_exchange: StopExchange | None = None) -> StopStatus:
    if len(exchanges) != 1 or not isinstance(envelope := exchanges[0], dict):
        return "unknown"
    parsed = stop_exchange if stop_exchange is not None else StopExchange.from_original(envelope)
    snapshot = parsed.stop_snapshot
    if parsed.task_id != stop_event["task_id"] or snapshot is None or snapshot.event != stop_event:
        return "unknown"
    attempts = turn.get("request_attempts", [])
    if not isinstance(attempts, list):
        return "unknown"
    sdk_counts = Counter(item.sdk_id for item in parsed.sdk_inputs if item.sdk_id is not None)
    identities = {item.sdk_id: item.identity for item in parsed.sdk_inputs if item.sdk_id is not None}
    row_counts = Counter(row.ordinal for row in parsed.requests)
    saved_by_number: dict[int, list[StopSavedRequest]] = {}
    for original in attempts:
        if not isinstance(original, dict):
            continue
        saved = StopSavedRequest.from_original(original)
        request_number = positive_number(saved.identity.get("number"))
        if request_number is not None:
            saved_by_number.setdefault(request_number, []).append(saved)
    complete = not parsed.missing_sections and len(identities) == len(parsed.sdk_inputs)
    terminal: list[tuple[int, str]] = []
    late_send = False
    for row in parsed.requests:
        number, sdk_id, identity = row.ordinal, row.sdk_id, row.identity
        reservation = (parsed.reservations.get(row.ledger_number)
                       if parsed.reservations is not None and row.ledger_number is not None else None)
        sdk_identity = identities.get(sdk_id) if sdk_id is not None and sdk_counts[sdk_id] == 1 else None
        if reservation is not None and row.conflicts_with(reservation, sdk_identity):
            raise SharedControlError("Gated request identity or terminal state changed.")
        if snapshot.conflicts_with(row, reservation.sdk_input_id if reservation is not None else None):
            if parsed.reservations is not None:
                raise SharedControlError("HTTP evidence conflicts with the stop snapshot.")
            return "unknown"
        if number is not None:
            expected_sdk = snapshot.sdk_by_ordinal.get(number)
            if expected_sdk is not None and reservation is not None and (
                reservation.state not in TERMINAL_STATES
                or reservation.sdk_input_id is not None and reservation.sdk_input_id != expected_sdk
            ):
                raise SharedControlError("Gated request identity or terminal state changed against the stop snapshot.")
        if row.not_sent and row.terminal_started is not True:
            continue
        available_identity = {**(sdk_identity or {}), **identity}
        request_number = positive_number(available_identity.get("number"))
        saved_attempts = saved_by_number.get(request_number, []) if request_number is not None else []
        if parsed.reservations is not None:
            for original_saved in saved_attempts:
                conflict = original_saved.conflict(available_identity, row.step_id, row.body_sha256)
                if conflict is not None:
                    raise SharedControlError(f"HTTP evidence product request {conflict} changed in the saved conversation.")
        counterparts = [item for item in saved_attempts if item.step_id == row.step_id]
        if (row.terminal_started is not True or row.not_sent or row.malformed_not_sent
                or number is None or row_counts[number] != 1 or row.step_id is None
                or sdk_id is None or sdk_counts[sdk_id] != 1 or identity.get("actor") != "main"
                or any(field not in identity for field in IDENTITY_FIELDS)
                or sdk_identity != identity or len(counterparts) != 1 or counterparts[0].identity != identity):
            complete = False
            continue
        if parsed.reservations is not None:
            if reservation is None or not row.has_matching_terminal_reservation(reservation) or counterparts[0].body_sha256 != row.body_sha256:
                complete = False
                continue
        if number > snapshot.last_ordinal:
            if row.body_sha256 is None or (not inprocess and row.ledger_number is None):
                complete = False
                continue
            late_send = True
        terminal.append((number, sdk_id))
    if late_send:
        return "failed"
    if not complete or terminal != list(snapshot.sdk_by_ordinal.items()):
        return "unknown"
    if not inprocess:
        transport = read_stop_original(root / "work/receiver/transport.json")
        if not isinstance(transport, dict):
            return "unknown"
        counts = (transport.get("mock_http_attempts"), transport.get("real_http_attempts"))
        if not all(type(count) is int and count >= 0 for count in counts) or sum(cast(tuple[int, int], counts)) != len(terminal):
            return "unknown"
    return "passed"


async def exercise_governance(reference: QualityPacketRef, *, task_id: str, protocol: Protocol, root: Path) -> CaseOutcome:
    """Exercise the canonical implementation packet with actual product tools and terminal Mock only."""
    from tests.support.agent_acceptance import AcceptancePlan, builtin_registry
    from tests.support.agent_acceptance import quality_runtime
    from tests.support.agent_acceptance.formal import provider_config
    packet=read_quality_packet(reference)
    if packet.intended_use!='implementation_regression' or packet.provenance!='synthetic' or not task_id.startswith('QG3.'):
        raise SharedControlError('In-process exercise is only for explicit implementation-seen synthetic QG3 tasks.')
    registry=builtin_registry(quality_packet=reference)
    case_id='quality.'+task_id.lower()
    registry.select(AcceptancePlan(cases=[case_id],protocols=[protocol],quality_packet=reference))
    root = root.resolve()
    root.mkdir(parents=True,exist_ok=False)
    runtime=root/'runtime'
    visible=quality_runtime.bind_packet(reference,task_id,runtime,evidence_root=root/'evidence',protocol=protocol)
    request=quality_runtime.manifest(case_id,runtime,protocol)
    config=ProviderConfig.model_validate(packet.models[protocol]['configuration']) if protocol in packet.models else provider_config(protocol)
    session=OfflineSession(protocol,quality_runtime.replies(case_id,runtime,protocol),config=config)
    records_root=runtime/'records';records_root.mkdir(exist_ok=True)
    conversation=Conversation(records_root/'conversation.json')
    scope=TaskScope.model_validate(request['scope'])
    task=Task(goal=request['prompt'],scope=scope,approval=ApprovalSettings.model_validate(request['approval']),provider_config=config)
    decisions=[]
    token=CancellationToken()
    stop_event: dict[str, str] | None = None
    terminal_at_stop: list[dict[str, Any]] = []
    action_events: list[dict[str, Any]] = []
    def updated(event:Any)->None:
        nonlocal stop_event
        if visible.adapter != 'qg3-9' or event.action is None:
            return
        identity = {"path": str(conversation.path), "task_id": task.id}
        action_events.append({"event": "action", **identity, "action": event.action.model_dump(
            mode='json', include={'id', 'task_id', 'status', 'effect', 'approval_id'})})
        if stop_event is None and event.action.kind == 'create' and event.action.status == 'denied':
            stop_event = {"event": "conversation_cancellation", **identity}
            action_events.append(stop_event)
            terminal_at_stop.extend({"attempt": row["attempt"], "sdk_input_id": row["sdk_input_id"]}
                                    for row in session.exchanges)
            token.cancel()
    async def approve(action:Action)->bool:
        candidate=runtime/visible.parameters.get('candidate',visible.parameters.get('parent_target','no-candidate-authorized'))
        content=visible.parameters.get('content',source_sum(visible) if visible.adapter in DELEGATED else None)
        allowed=action.kind=='create' and Path(action.target)==candidate and action.prepared_parameters.get('content')==content
        decisions.append({'action_id':action.id,'approval_id':action.approval_id,'task_id':action.task_id,'target':action.target,'prepared_parameters':action.prepared_parameters,'prepared_action_sha256':action.prepared_action_sha256,'approval_context_sha256':action.approval_context_sha256,'before_exists':Path(action.target).exists(),'approved':allowed,'origin':'public_conversation_approval_callback'})
        return allowed
    async with session.model() as model:
        turn=await conversation.ask(task.goal,document=None,scope=scope,task=task,model=model,review_model=model,approve_action=approve,cancellation_token=token,on_update=updated,project_trusted=request.get('project_trusted',False))
    state=runtime/'inprocess/receiver/state.json'
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'approval_decisions':decisions,'passed':turn.status in {'completed','cancelled'},
        'entry':'public_conversation','task_id':task.id,'stop_accepted':stop_event is not None,'stop_event':stop_event,
        **({'action_events': action_events} if visible.adapter == 'qg3-9' else {})},
        ensure_ascii=False),encoding='utf-8')
    records=[json.loads(path.read_bytes()) for path in records_root.rglob('*.json') if path.parent==records_root or path.parent.name.endswith('-delegations')]
    observed_attempts={}
    for raw_record in records:
        for saved in ConversationRecord.model_validate(raw_record).turns:
            for attempt in [*saved.request_attempts,*(request for action in saved.actions if action.review is not None for request in action.review.request_attempts)]:
                observed_attempts[(attempt.number,attempt.actor,attempt.purpose,attempt.delegation_id)]=attempt.model_dump(mode='json')
    if stop_event is not None:
        for row in session.exchanges:
            identity = row["request_identity"]
            owner = observed_attempts.get(tuple(identity[key] for key in ("number", "actor", "purpose", "delegation_id")))
            row["terminal_http_started"] = True
            row["product_step_id"] = owner["step_id"] if owner is not None else None
    exchange=session.save(root/'exchange.json',attempts=sorted(observed_attempts.values(),key=lambda item:item['number']),run_id=uuid4().hex,case_id=case_id,task_id=task.id)
    captured=json.loads(exchange.read_bytes())
    if stop_event is not None:
        captured["stop_boundaries"] = [{"event": stop_event, "terminal_attempts": terminal_at_stop}]
        exchange.write_text(json.dumps(captured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    facts=quality_runtime.score(case_id,runtime,records,[captured])
    facts.update(real_http_attempts=0,mock_http_attempts=len(session.exchanges),entry='canonical_inprocess_public_conversation',
        output_standard=packet.tasks[[row.task_id for row in packet.tasks].index(task_id)].domain_standards['main_answer'].model_dump(mode='json'))
    review=root/'evidence/review-context'
    review.mkdir(parents=True)
    frozen_task=next(row for row in packet.tasks if row.task_id==task_id)
    originals={}
    for label,path in (('input',frozen_task.input_path),('gold',frozen_task.gold_path),('freeze',reference.path)):
        target=review/(label+'.original.json')
        target.write_bytes((SOURCE/path).read_bytes())
        originals['quality.'+label]=target
    (review/'purpose.json').write_text(json.dumps({'purpose':'review_context','product_output':False,'human_review':'not_performed','packet_sha256':reference.sha256}),encoding='utf-8')
    evidence={'exchange':exchange,**originals,**{path.relative_to(root).as_posix():path for path in root.rglob('*') if path.is_file()}}
    if visible.adapter == 'qg3-3' and facts['passed'] and facts['fact_contract_status'] == 'unknown':
        return CaseOutcome('inconclusive', facts, evidence, critical_contracts=facts['critical_contracts'])
    return CaseOutcome('passed' if facts['passed'] else 'failed',facts,evidence,critical_contracts=facts['critical_contracts'])
