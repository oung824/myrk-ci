"""Offline execution of a real completed and cancelled correction history."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
from pydantic_ai import CancellationToken
from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart, UserPromptPart

from myrk.conversation import Conversation, ConversationRecord, Supplement, Task, TaskScope
from myrk.permissions import ApprovalSettings
from myrk.provider import ProviderConfig
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_compaction_correction import (
    ATTENDANCE, ROSTER, goals, primary_sources, score, shift_counts,
)
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import OfflineSession, Reply


class CorrectionMock:
    def __init__(self, visible: QualityVisibleInput, protocol: Protocol, config: ProviderConfig) -> None:
        self.visible = visible
        self.terminal = OfflineSession(protocol, [], config=config)
        self.stage = 'attendance'
        self.completed: set[str] = set()
        self.calls: dict[str, str] = {}
        self.summaries = 0

    def respond(self, request: httpx2.Request, sdk: dict[str, Any], purpose: str) -> httpx2.Response:
        messages = ModelMessagesTypeAdapter.validate_python(sdk['messages'])
        current = goals(self.visible)
        facts = self.visible.parameters['source_documents'][0]['facts']
        historical, shifts = shift_counts(self.visible)
        for message in messages:
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    if current['primary'] in part.content: self.stage = 'primary'
                    elif current['roster'] in part.content: self.stage = 'roster'
                if isinstance(part, ToolCallPart) and part.tool_name == 'read_text':
                    name = part.args_as_dict().get('path')
                    if isinstance(name, str): self.calls[part.tool_call_id] = name
        for message in messages:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == 'read_text' and part.outcome == 'success' and part.tool_call_id in self.calls:
                    self.completed.add(self.calls[part.tool_call_id])
        if purpose == 'compaction':
            self.summaries += 1
            summary = (f'The historical volunteer roster had {historical} shifts. This intentionally wrong old source summary remains evidence, not authority. '
                'Attendance read completed; roster-check cancelled. Preserve original source mapping and the distinct task states. '
                'Only the primary source report is unfinished; no reread of attendance or cancelled roster check is authorized.') if self.summaries == 1 else (
                f"Current volunteer-roster source record: shifts={shifts}, volunteers={facts['volunteers']}. Original wrong shifts={historical} summary remains historical and does not authorize work. "
                'Attendance completed and roster-check cancelled; preserve current user correction and actual source evidence.')
            reply = Reply(text=summary)
        elif self.stage == 'roster':
            reply = Reply(tool='read_text', arguments={'path': ROSTER})
        elif self.stage == 'attendance':
            reply = Reply(tool='read_text', arguments={'path': ATTENDANCE}) if ATTENDANCE not in self.completed else Reply(text=f"Attendance read completed: volunteers={facts['volunteers']}. No roster check was performed.")
        else:
            pending = next((name for name in primary_sources(self.visible) if name not in self.completed), None)
            reply = Reply(tool='read_text', arguments={'path': pending}) if pending else Reply(text=(
                f"Current explicit correction controls: shifts={shifts}, volunteers={facts['volunteers']}. The old shifts={historical} summary is wrong and retained unchanged as historical evidence. "
                'Attendance remains completed; the separate roster check remains cancelled. Neither was repeated. No writes or extra work were performed. User acceptance remains pending.'))
        self.terminal.replies.append(reply)
        return self.terminal.respond(request)


def terminal_mock(visible: QualityVisibleInput, protocol: Protocol, config: ProviderConfig, *, phase: str = 'work') -> CorrectionMock:
    if phase != 'work': raise SharedControlError('Correction flow has one actual Main phase.')
    return CorrectionMock(visible, protocol, config)


async def exercise(reference: Any, *, task_id: str, protocol: Protocol, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest as canonical_manifest
    packet = read_quality_packet(reference)
    if packet.provenance != 'synthetic' or packet.intended_use != 'implementation_regression' or task_id != 'QG6.formal.9':
        raise ValueError('Correction exercise only accepts implementation-seen synthetic slot9.')
    registry = builtin_registry(quality_packet=reference)
    case_id = 'quality.' + task_id.lower()
    if case_id not in registry.cases: raise ValueError('Canonical correction case is missing.')
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root, protocol=protocol)
    request = canonical_manifest(case_id, root, protocol)
    config = provider_config(protocol).model_copy(update={'context_window_override': 26000, 'output_token_reserve': 4096,
        'context_safety_margin': 512, 'compaction_trigger': .7, 'compaction_target': .55, 'compaction_recent': .1})
    mock = CorrectionMock(visible, protocol, config)
    token = CancellationToken()
    class DynamicSession(OfflineSession):
        def respond(self, raw: httpx2.Request) -> httpx2.Response:
            attempt = current_request_attempt()
            if attempt is None: raise SharedControlError('Actual native request purpose missing.')
            pending = root / 'quality-correction-first-summary-pending.original.json'
            if attempt.purpose == 'compaction' and not pending.exists():
                pending.write_bytes(conversation.path.read_bytes())
            response = mock.respond(raw, self.sdk_inputs[-1], attempt.purpose)
            if mock.stage == 'roster':
                token.cancel()
                mock.terminal.exchanges[-1]['received_utf8'] = ''
                raise asyncio.CancelledError('Actual roster request cancelled before consuming its response.')
            return response
    session = DynamicSession(protocol, [], config=config)
    session.exchanges = mock.terminal.exchanges
    conversation = Conversation(root / 'records/conversation.json')
    scope = TaskScope.model_validate(request['scope'])
    approval = ApprovalSettings.model_validate(request['approval'])
    def task(goal: str) -> Task:
        return Task(goal=goal, scope=scope, approval=approval.model_copy(deep=True), request_limit=60, provider_config=config)
    attendance_task = task(request['prompt'])
    roster_task = task(goals(visible)['roster'])
    primary_task = task(goals(visible)['primary'])
    saved = False
    def updated(event: Any) -> None:
        nonlocal saved
        completed = [item for item in conversation.compaction_status() if item['status'] == 'completed']
        if completed and not saved:
            original = next(item for turn in conversation.turns for item in turn.compactions if item.id == completed[0]['id'])
            (root / 'quality-correction-old-summary.original.json').write_text(original.model_dump_json(), encoding='utf-8')
            primary_task.supplements.append(Supplement(text=goals(visible)['correction']))
            saved = True
    async with session.model() as model:
        await conversation.ask(attendance_task.goal, document=None, task=attendance_task, scope=scope, model=model)
        await conversation.ask(roster_task.goal, document=None, task=roster_task, scope=scope, model=model, new_task=True, cancellation_token=token)
        prior = ConversationRecord.model_validate_json(conversation.path.read_bytes()).model_dump(mode='json')['turns']
        (root / 'quality-correction-prior-tasks.original.json').write_text(json.dumps(prior, ensure_ascii=False), encoding='utf-8')
        await conversation.ask(primary_task.goal, document=None, task=primary_task, scope=scope, model=model, new_task=True, on_update=updated)
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes())
    attempts = [{'task_id': turn.task.id, **attempt.model_dump(mode='json')} for turn in record.turns if turn.task for attempt in turn.request_attempts]
    exchange = session.save(root / 'exchange.json', attempts=attempts, run_id=uuid4().hex, case_id=case_id, task_id=primary_task.id)
    facts = score(visible, root, [record.model_dump(mode='json')], [json.loads(exchange.read_bytes())])
    facts.update(real_http_attempts=0, formal_execution=False, observation_layer='conversation-sdk-executor', formal_ledger_unknown_occupancy='not_exercised_here',
        actual_human_review='not_performed', independent_quality_sample_increment=0, correction_entry='typed_Task_supplements_queue; public_Main_send_verified_separately')
    (root / 'correction-program-facts.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
    from tests.support.agent_acceptance.frozen import source_material_path
    selected = next(item for item in packet.tasks if item.task_id == task_id)
    review = root / 'quality-review'
    review.mkdir(exist_ok=False)
    evidence = {'conversation': conversation.path, 'exchange': exchange,
        'old_summary': root / 'quality-correction-old-summary.original.json',
        'prior_tasks': root / 'quality-correction-prior-tasks.original.json'}
    bindings = {}
    for label, original in (('input', selected.input_path), ('gold', selected.gold_path), ('freeze', reference.path)):
        body = source_material_path(original).read_bytes()
        target = review / (label + '.original.json')
        target.write_bytes(body)
        evidence['quality.' + label] = target
        bindings[label] = {'source': original.as_posix(), 'sha256': sha256(body).hexdigest(), 'purpose': 'review_context', 'product_output': False}
    (review / 'originals-index.json').write_text(json.dumps(bindings, indent=2), encoding='utf-8')
    facts['output_domain_criteria'] = {'criteria': {kind: value.model_dump(mode='json') for kind, value in selected.domain_standards.items()},
        'human_review': 'not_performed', 'provenance': 'implementation_regression'}
    return CaseOutcome('passed' if facts['passed'] else 'failed', facts, evidence, critical_contracts=facts['critical_contracts'])
