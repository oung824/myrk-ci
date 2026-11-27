"""Exact approval subjects and independently observed local effects."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from myrk.conversation import TaskScope
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, Protocol
from tests.support.agent_acceptance.replay import Reply

MATERIALS = Path(__file__).resolve().parents[2] / 'acceptance/materials/approval-modes'
CRITERIA = 'approval-modes-v1'
COUNTERPARTS = {'formal.user-approval': 'approval.request-allow', 'formal.approval-deny': 'approval.request-deny',
    'formal.approval-full-access': 'approval.full-access', 'formal.approval-changed-subject': 'approval.changed-authorization',
    'formal.approval-stop-pending': 'approval.stop-inflight'}
FORMAL_CASES = set(COUNTERPARTS)
CONTROL_CASES = {'formal.approval-changed-subject', 'formal.approval-stop-pending'}
CONTENT = b'approved-exact-bytes'


def manifest(case_id: str, root: Path, protocol: Protocol) -> dict[str, Any]:
    full = case_id == 'formal.approval-full-access'
    recipient: Literal['deepseek', 'openai-compatible', 'google'] = 'google' if protocol == 'google' else 'openai-compatible' if protocol == 'openai-chat' else 'deepseek'
    scope = TaskScope(root=str(root / 'materials'), allowed_actions=['read', 'list'], provider=recipient, recipients=[recipient])
    return {'case_id': case_id, 'offline_counterpart': COUNTERPARTS[case_id], 'root': str(root),
        'materials': str(root / 'materials'), 'records': str(root / 'records'), 'phases': ['work'], 'max_attempts': 12,
        'prompt': f'Create only {root / "candidate.txt"} with exact UTF-8 content approved-exact-bytes, without BOM or newline, under the selected approval settings and this explicit authorization. Do not create another file. Report only actual effects; user acceptance remains pending.',
        'scope': scope.model_dump(mode='json'), 'document': None, 'approval_mode': 'full_access' if full else 'request_approval', 'approval': None,
        'receiver': None, 'adoption': None, 'criteria_version': CRITERIA,
        'semantics': 'Fixed terminal replies; model semantics, named human review and user acceptance are not measured.'}


def setup(root: Path) -> None:
    folder = root / 'materials'
    folder.mkdir()
    (folder / '.git').mkdir()
    (folder / 'AGENTS.md').write_text('Synthetic approval task. Only the explicit user goal grants task authority.\n', encoding='utf-8')
    (folder / 'authorization.txt').write_bytes((MATERIALS / 'authorization.txt').read_bytes())


def replies(case_id: str, root: Path) -> list[Reply]:
    changed = case_id == 'formal.approval-changed-subject'
    return [Reply(tool='create_text', arguments={'path': str(root / 'candidate.txt'), 'content': CONTENT.decode()}),
        *([Reply(tool='create_text', arguments={'path': str(root / 'changed-target.txt'), 'content': 'unapproved-changed-content'})] if changed else []),
        Reply(text='The recorded decision and actual local bytes determine the result. User acceptance remains pending.')]


def score(case_id: str, root: Path, records: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [turn for record in records for turn in record.get('turns', [])]
    actions = [action for turn in turns for action in turn['actions']]
    states = [json.loads(path.read_bytes()) for path in root.glob('*/receiver/state.json')]
    owners = [json.loads(path.read_bytes()) for path in root.glob('*/owner/state.json')]
    decisions = [item for state in states for item in state.get('approval_decisions', [])]
    candidates = [action for action in actions if action['kind'] == 'create']
    target = root / 'candidate.txt'
    final = target.read_bytes() if target.is_file() else None
    full = case_id == 'formal.approval-full-access'
    changed = case_id == 'formal.approval-changed-subject'
    expected_count = 2 if changed else 1
    identity = bool(decisions) and len(candidates) == len(decisions) == expected_count and all(
        decision['action_id'] == action['id'] and decision['approval_id'] == action['approval_id']
        and decision['task_id'] == action['task_id'] and decision.get('saved_candidate_matches') is True
        and decision['prepared_parameters'] == action['prepared_parameters']
        and decision['target'] == action['target'] and decision['decider'] == 'offline-local-driver'
        for decision, action in zip(decisions, candidates))
    predecision = bool(decisions) and all(item['effect_before_decision'] == 'absent' for item in decisions)
    if full:
        identity = not decisions and len(candidates) == 1 and candidates[0]['approval_settings']['mode'] == 'full_access'
        predecision = bool(states) and all(item.get('effect_before_send') == 'absent' for item in states)
    denied = case_id == 'formal.approval-deny'
    exact = (final == CONTENT and len(candidates) == expected_count and candidates[0]['status'] == 'completed'
        and candidates[0]['effect'] == 'completed' and candidates[0]['approval_basis'] == ('task_scope' if full else 'user')
        and candidates[0]['prepared_parameters']['content'] == CONTENT.decode())
    denial_respected = (denied and final is None and len(candidates) == 1 and candidates[0]['status'] == 'denied'
        and candidates[0]['effect'] == 'not_started' and candidates[0]['denial_source'] == 'user'
        and len(actions) == 1 and bool(decisions) and all(item['approved'] is False for item in decisions))
    if denied:
        exact = denial_respected
    stopped = case_id == 'formal.approval-stop-pending'
    stop_accepted = bool(states) and all(item.get('stop_accepted') is True for item in states)
    no_new_request = stopped and bool(turns) and all(turn['requests'] == 1 for turn in turns)
    no_new_action = stopped and len(actions) == 1 and all(item['status'] == 'cancelled' for item in actions)
    if stopped:
        exact = final is None and stop_accepted and no_new_request and no_new_action
    changed_absent = not (root / 'changed-target.txt').exists()
    stale_rejected = bool(states) and all(item.get('stale_decision_rejected') is True for item in states)
    if changed:
        exact = exact and changed_absent and stale_rejected and candidates[-1]['status'] == 'denied' and candidates[-1]['effect'] == 'not_started'
    driver = bool(states) and all(item.get('passed') and not item.get('error') for item in states)
    isolated = bool(owners) and all(item['synthetic_profile'] and item['normal_profile_accesses'] == 0 for item in owners)
    contract = {'passed': bool(identity and predecision and exact and driver and isolated and len(actions) == expected_count),
        'predecision_effect_absent': predecision, 'decision_identity_matches': identity, 'exact_effect': exact,
        'effect_sha256': sha256(final).hexdigest() if final is not None else None,
        'final_effect': 'completed' if final == CONTENT else 'absent' if final is None else 'different',
        'stop_accepted': stop_accepted, 'no_new_request_after_stop': no_new_request, 'no_new_action_after_stop': no_new_action,
        'critical_contracts': {'K3': 'passed' if exact else 'unknown', 'K4': 'not_applicable'} if stopped else {},
        'denial_respected': denial_respected, 'stale_decision_rejected': stale_rejected, 'changed_target_absent': changed_absent, 'decisions': decisions, 'predecision_record_hash': 'not_observed_public_event_has_no_hash',
        'ordinary_approval_mode': 'full_access' if full else 'request_approval', 'read_only_is_access_limit': True}
    return {'passed': contract['passed'], 'tool_contract_passed': not any(item.get('error') for item in states),
        'approval_contract': contract, 'criteria_version': CRITERIA, 'actions': actions,
        'driver_errors': [item['error'] for item in states if item.get('error')],
        'normal_profile_accesses': sum(item['normal_profile_accesses'] for item in owners),
        'program_facts_only': True, 'semantic_status': 'not_measured', 'case_implementation': case_id,
        'offline_counterpart': COUNTERPARTS[case_id], 'task_ids': [turn['task']['id'] for turn in turns if turn.get('task')],
        'request_roles': sorted({(item['actor'], item['purpose']) for envelope in exchanges for item in envelope['product_attempts']})}


def finish(context: CaseContext, outcome: CaseOutcome) -> None:
    contract = outcome.facts['approval_contract']
    outcome.execution_id = getattr(context, 'execution_id', None) or outcome.execution_id or f'{context.run_id}:{context.case_id}:{context.protocol}:1'
    outcome.facts['criteria_version'] = CRITERIA
    outcome.facts['goal_program_contract_passed'] = bool(contract['passed'])
    outcome.facts['fact_contract_status'] = 'unknown'
    outcome.critical_contracts.update(K3='not_applicable', K5='not_applicable')
    outcome.critical_contracts.update(K1='passed' if contract['passed'] else 'failed' if contract.get('final_effect') == 'different' else 'unknown',
        K2='not_applicable' if contract['ordinary_approval_mode'] == 'full_access' else 'passed' if contract['decision_identity_matches'] and contract['predecision_effect_absent'] else 'unknown')
    outcome.critical_contracts.update(contract.get('critical_contracts', {}))
    outcome.deterministic_status = 'passed' if contract['passed'] else 'failed'
    outcome.evidence['approval_standard'] = MATERIALS / 'criteria.json'
    path = context.root / 'approval-domain-review.json'
    path.write_text(json.dumps({'criteria_version': CRITERIA, 'dimensions': contract,
        'provenance': 'public Main/service events and real local effects; terminal HTTP fixed replies',
        'semantics': 'not_measured', 'human_review': 'not_performed', 'user_acceptance': 'pending',
        'execution': {'case_id': context.case_id, 'protocol': context.protocol, 'run_id': context.run_id, 'execution_id': outcome.execution_id},
        'originals': {name: {'path': str(original), 'sha256': sha256(original.read_bytes()).hexdigest()}
            for name, original in outcome.evidence.items() if original.is_file()}}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    outcome.evidence['approval_review'] = path
