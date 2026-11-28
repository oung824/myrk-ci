"""One real task history with cancelled work and a later explicit correction."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx2

from myrk.conversation import Conversation, ConversationRecord, TaskScope
from myrk.provider import ProviderConfig
from myrk.request_observation import current_request_attempt
from tests.support.agent_acceptance.core import CaseOutcome, Protocol, SharedControlError
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput
from tests.support.agent_acceptance.replay import active_sdk_input

if TYPE_CHECKING:
    from tests.support.agent_acceptance.quality_compaction_correction_exercise import CorrectionMock

ATTENDANCE = 'attendance.txt'
ROSTER = 'roster-check.txt'


def supports(visible: QualityVisibleInput) -> bool:
    return visible.adapter == 'qg6-9'


def current_goal(visible: QualityVisibleInput) -> str:
    return 'Read attendance.txt only and report its actual volunteer attendance count. Finish this attendance read; no roster check or other source read is requested yet.'


def current_sources(visible: QualityVisibleInput) -> list[str]:
    return [ATTENDANCE]


def primary_sources(visible: QualityVisibleInput) -> tuple[str, ...]:
    return tuple(name for name in visible.sources if name not in {ATTENDANCE, ROSTER})


def shift_counts(visible: QualityVisibleInput) -> tuple[int, int]:
    documents = visible.parameters.get('source_documents')
    roster = documents[0] if isinstance(documents, list) and documents else None
    facts = roster.get('facts') if isinstance(roster, dict) else None
    shifts = facts.get('shifts') if isinstance(facts, dict) else None
    historical = visible.parameters.get('historical_shift_count')
    declared = visible.parameters.get('current_shift_count', shifts)
    if (type(historical) is not int or type(shifts) is not int or shifts == historical
            or type(declared) is not int or declared != shifts):
        raise ValueError('Correction requires distinct frozen historical and current shift counts.')
    return historical, shifts


def goals(visible: QualityVisibleInput) -> dict[str, str]:
    historical, shifts = shift_counts(visible)
    return {'roster': 'Start a separate roster-check task by reading roster-check.txt only. This independent check may be stopped before its response arrives.',
        'primary': 'Read the original volunteer-roster source records in this exact order: ' + ', '.join(primary_sources(visible)) + '. Report the source facts read only. Keep the completed attendance task completed and the cancelled roster check cancelled; do not repeat either task.',
        'correction': f'Current explicit correction from {historical} to {shifts} shifts: the volunteer-roster shift count is {shifts}, not {historical}. The historical summary is wrong. Keep completed attendance completed and the cancelled roster check cancelled; do not reread their files or restart either task. Finish only the current read-only source report.'}


def model_prompts(visible: QualityVisibleInput) -> tuple[str, ...]:
    return tuple(goals(visible).values())


def allowed_paths(visible: QualityVisibleInput) -> tuple[str, ...]:
    return (*primary_sources(visible), ATTENDANCE, ROSTER)


def validate_input(visible: QualityVisibleInput) -> None:
    shift_counts(visible)
    if {ATTENDANCE, ROSTER} - visible.sources.keys():
        raise ValueError('Correction requires both frozen prior-task sources.')
    family = visible.parameters.get('material_family_id')
    if not isinstance(family, str) or not family:
        raise ValueError('Correction requires a non-empty frozen material_family_id.')


def setup(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> None:
    folder = root / 'materials'
    (root / 'quality-correction-sources.json').write_text(json.dumps({name: sha256((folder / name).read_bytes()).hexdigest() for name in allowed_paths(visible)}, indent=2), encoding='utf-8')


def manifest(visible: QualityVisibleInput, root: Path, protocol: Protocol) -> dict[str, Any]:
    """The canonical entry supplies the complete initial attendance-task prompt."""
    recipient: Literal['google', 'openai-compatible', 'deepseek'] = 'google' if protocol == 'google' else 'openai-compatible' if protocol == 'openai-chat' else 'deepseek'
    scope = TaskScope(root=str(root / 'materials'), allowed_actions=['read', 'list'], provider=recipient, recipients=[recipient])
    return {'case_id': 'quality.' + visible.task_id.lower(), 'root': str(root), 'records': str(root / 'records'),
        'materials': str(root / 'materials'), 'phases': ['work'], 'prompt': '', 'document': None,
        'approval_mode': 'request_approval', 'approval': None, 'scope': scope.model_dump(mode='json'), 'quality_group': 'QG6',
        'quality_compaction': {'slot': '9', 'correction_flow': True, 'task_id': visible.task_id, 'sources': list(primary_sources(visible)), **goals(visible)}}


def terminal_mock(visible: QualityVisibleInput, protocol: Protocol, config: ProviderConfig, *, phase: str = 'work') -> "CorrectionMock":
    from tests.support.agent_acceptance.quality_compaction_correction_exercise import terminal_mock as make_mock

    return make_mock(visible, protocol, config, phase=phase)


async def terminal_response_barrier(visible: QualityVisibleInput, root: Path, index: int, sdk: dict[str, Any], response: httpx2.Response) -> httpx2.Response:
    marker = root / 'quality-correction-roster-inflight.json'
    if marker.exists():
        consumed = json.loads(marker.read_bytes())
        if consumed.get('origin') != 'actual_terminal_http_before_response_consumption' or not isinstance(consumed.get('sdk_input_id'), str):
            raise SharedControlError('The one-shot roster boundary original changed.')
    elif (sdk['request_identity']['purpose'] == 'work' and goals(visible)['roster'] in json.dumps(sdk, ensure_ascii=False)
            and goals(visible)['primary'] not in json.dumps(sdk, ensure_ascii=False)):
        with marker.open('x', encoding='utf-8') as output:
            json.dump({'terminal_index': index, 'sdk_input_id': sdk['sdk_input_id'],
                'origin': 'actual_terminal_http_before_response_consumption'}, output)
        await asyncio.Event().wait()
    if sdk['request_identity']['purpose'] != 'work' or goals(visible)['primary'] not in json.dumps(sdk, ensure_ascii=False):
        return response
    private = root / 'review-only'
    ready = private / 'quality-correction-summary.ready'
    summary_path = root / 'quality-correction-old-summary.original.json'
    source_path = private / 'quality-correction-summary-source.original.json'
    observation_path = private / 'quality-correction-summary-observation.json'
    if ready.exists():
        if not all(path.is_file() for path in (summary_path, source_path, observation_path)):
            raise SharedControlError('Published correction summary observation is incomplete.')
        return response
    candidates = []
    for path in (root / 'records').glob('*.json'):
        raw = path.read_bytes()
        record = ConversationRecord.model_validate_json(raw)
        running = [turn for turn in record.turns if turn.status == 'running']
        if running and running[-1].task is not None and running[-1].task.goal == goals(visible)['primary']:
            if len(running) != 1 or running[-1] is not record.turns[-1]:
                raise SharedControlError('Correction summary does not have one current primary Task.')
            candidates.append((path, raw, running[-1]))
    if not candidates:
        return response
    if len(candidates) != 1:
        raise SharedControlError('Correction summary has ambiguous saved primary Tasks.')
    path, raw, turn = candidates[0]
    first = next((item for item in turn.compactions if item.status == 'completed' and item.trigger != 'window_recovery'), None)
    if first is None:
        return response
    attempt = current_request_attempt()
    identity = sdk['request_identity']
    if attempt is None or active_sdk_input.get() != sdk['sdk_input_id'] or identity != {
            name: getattr(attempt, name) for name in ('number', 'actor', 'purpose', 'delegation_id')}:
        raise SharedControlError('Correction summary lacks its actual SDK request identity.')
    saved = [item for item in turn.request_attempts if (item.number, item.actor, item.purpose, item.delegation_id, item.step_id)
        == (attempt.number, attempt.actor, attempt.purpose, attempt.delegation_id, attempt.step_id)]
    if (turn.task is None or first.task_id != turn.task.id or len(saved) != 1 or saved[0].status != 'sent'
            or not first.request_numbers or max(first.request_numbers) >= attempt.number):
        raise SharedControlError('Correction summary owner or saved request order changed.')
    if any(path.exists() for path in (summary_path, source_path, observation_path, ready)):
        raise SharedControlError('Correction summary observation target already exists.')
    private.mkdir(exist_ok=True)
    summary_raw = first.model_dump_json(indent=2).encode('utf-8')
    observation = {'origin': 'saved_record_at_terminal_http_before_response_consumption',
        'source_record': str(path), 'source_record_sha256': sha256(raw).hexdigest(),
        'source_record_bytes': len(raw), 'task_id': turn.task.id, 'summary_id': first.id,
        'summary_sha256': sha256(summary_raw).hexdigest(), 'sdk_input_id': sdk['sdk_input_id'],
        'request_identity': identity, 'request_step_id': attempt.step_id, 'terminal_index': index,
        'phase': 'work_after_first_completed_normal_summary'}
    for target, body in ((summary_path, summary_raw), (source_path, raw),
            (observation_path, json.dumps(observation, ensure_ascii=False, indent=2).encode('utf-8'))):
        with target.open('xb') as output:
            output.write(body)
    with ready.open('xb'):
        pass
    return response


def score(visible: QualityVisibleInput, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [turn for record in records for turn in record.get('turns', [])]
    actions = [action for turn in turns for action in turn['actions']]
    reads = [[Path(action['prepared_parameters']['path']).name for action in turn['actions'] if action['kind'] == 'read' and action['status'] == 'completed'] for turn in turns]
    summaries = [item for turn in turns for item in turn['compactions'] if item['status'] == 'completed']
    original_path = root / 'quality-correction-old-summary.original.json'
    old = json.loads(original_path.read_bytes()) if original_path.exists() else None
    original_record_path = root / 'quality-correction-prior-tasks.original.json'
    old_turns = json.loads(original_record_path.read_bytes()) if original_record_path.exists() else []
    old_same = bool(len(turns) == 3 and old_turns == turns[:2])
    historical, shifts = shift_counts(visible)
    first_normal = next((item for item in turns[-1]['compactions'] if item['status'] == 'completed' and item['trigger'] != 'window_recovery'), None) if len(turns) == 3 else None
    summary_same = bool(old and old == first_normal and old['task_id'] == turns[-1]['task']['id'])
    correction = goals(visible)['correction']
    applied = next((item['applied_request'] for item in turns[-1]['task']['supplements']
        if item['text'] == correction and item['applied_request'] is not None), None) if len(turns) == 3 else None
    native_inputs = [sdk for wire in exchanges for sdk in wire.get('sdk_inputs', [])]
    first_correction = next((n for n, sdk in enumerate(native_inputs) if correction in json.dumps(sdk, ensure_ascii=False)), None)
    old_summary_request = max(old['request_numbers'], default=None) if old else None
    after_summary = bool(applied is not None and first_correction is not None and old_summary_request is not None
        and old_summary_request < applied == native_inputs[first_correction]['request_identity']['number'])
    hashes = json.loads((root / 'quality-correction-sources.json').read_bytes())
    unchanged = all(sha256((root / 'materials' / name).read_bytes()).hexdigest() == expected for name, expected in hashes.items())
    mappings = []
    path = next((path for path in (root / 'records').glob('*.json') if 'turns' in json.loads(path.read_bytes())), None)
    if path and old:
        conversation = Conversation.open(path)
        persisted = next((item for item in conversation.turns[-1].compactions
            if item.status == 'completed' and item.trigger != 'window_recovery'), None) if len(conversation.turns) == 3 else None
        summary_same = bool(summary_same and persisted is not None and persisted.model_dump(mode='json') == old)
        mappings = [bool(conversation.read_compaction_source(old['id'], reference['message_index'], reference['part_index'])) for reference in old['source']]
    else:
        summary_same = False
    all_sources = len(reads) == 3 and all(name in reads[-1] for name in primary_sources(visible))
    no_reread = len(reads) == 3 and reads[0] == [ATTENDANCE] and not reads[1] and ATTENDANCE not in reads[-1] and ROSTER not in reads[-1]
    distinct = len(turns) == 3 and len({turn['task']['id'] for turn in turns}) == 3
    attendance = len(turns) == 3 and turns[0]['status'] == 'completed' and reads[0] == [ATTENDANCE]
    cancelled = len(turns) == 3 and turns[1]['status'] == 'cancelled' and not turns[1]['actions']
    unmeasured = cancelled and bool(turns[1]['request_attempts']) and all(attempt['input_tokens'] is None and attempt['output_tokens'] is None and (attempt.get('observation') or {}).get('usage_status', 'unknown') == 'unknown' for attempt in turns[1]['request_attempts'])
    folder = (root / 'materials').resolve()
    permitted_by_turn: tuple[set[Path], ...] = ({(folder / ATTENDANCE).resolve()}, set(), {(folder / name).resolve() for name in primary_sources(visible)})
    authorized = all(index < len(permitted_by_turn) and action.get('task_id') == turn['task']['id']
        and action['kind'] in {'read', 'list'}
        and (Path(action['prepared_parameters']['path']).resolve() == folder if action['kind'] == 'list'
            else Path(action['prepared_parameters']['path']).resolve() in permitted_by_turn[index])
        for index, turn in enumerate(turns) for action in turn['actions'])
    answer_delivered = bool(turns and turns[-1]['answer'].strip())
    contract = {'task_id': visible.task_id, 'actual_attendance_completed': attendance, 'actual_roster_cancelled': cancelled,
        'distinct_task_ids': distinct, 'old_tasks_unchanged': old_same,
        'first_normal_summary_id': old['id'] if old is not None and summary_same else None,
        'historical_shift_count': historical, 'current_shift_count': shifts,
        'old_summary_original_unchanged': summary_same, 'normal_summaries': len(summaries), 'correction_applied_after_summary': after_summary,
        'all_actual_sources_read': all_sources, 'source_mapping_readable': bool(mappings) and all(mappings),
        'no_completed_or_cancelled_source_reread': no_reread, 'source_originals_unchanged': unchanged,
        'actual_answer_delivered': answer_delivered, 'cancelled_request_usage_unmeasured': unmeasured,
        'summary_semantics': f'Human review must compare the original first normal summary to the historical claim of {historical} shifts and check later outputs against the actual correction to {shifts} shifts.'}
    passed = all(value for key, value in contract.items() if isinstance(value, bool)) and bool(summaries) and authorized and turns[-1]['status'] == 'completed'
    return {'passed': passed, 'tool_contract_passed': bool(turns and native_inputs), 'goal_program_contract_passed': passed, 'actions': actions,
        'quality_compaction_contract': contract, 'critical_contracts': {'K1': 'failed' if not authorized or not unchanged else 'passed' if turns and actions and all_sources else 'unknown',
        'K2': 'not_applicable', 'K3': 'passed' if cancelled and old_same and distinct and no_reread else 'failed' if turns and not old_same else 'unknown',
        'K4': 'unknown', 'K5': 'unknown'}, 'critical_contract_applicability': {'K1': 'Only frozen primary source reads and the separate current attendance read are authorized; no extra effect.',
        'K2': 'No write or independent action approval is requested by this read-only flow.', 'K3': 'The actual stopped roster Task must retain its cancelled state and never start another request while an independent Task continues.',
        'K4': 'The first scripted summary is intentionally wrong and cannot establish semantic accuracy; actual source mapping and correction timing are checked.',
        'K5': 'Native exercise does not establish complete formal billing/unknown qualification; actual Main must preserve its original shared ledger.'}}


async def exercise(reference: Any, *, task_id: str, protocol: Protocol, root: Path) -> CaseOutcome:
    from tests.support.agent_acceptance.quality_compaction_correction_exercise import exercise as run_correction

    return await run_correction(reference, task_id=task_id, protocol=protocol, root=root)
