"""Each QG3 goal uses its canonical packet and actual public product tools."""
import asyncio
from uuid import uuid4
from pathlib import Path
import shutil
import pytest
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet


@pytest.fixture
def governance_packet(request, monkeypatch):
    root=SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'/('governance-'+uuid4().hex)
    try:
        with monkeypatch.context() as patch:
            if getattr(request, 'param', None) == 'multiline-ledger':
                import json

                read_bytes = Path.read_bytes
                inputs = SOURCE / 'tests/acceptance/materials/quality/integrated-inputs.json'

                def read_multiline_ledger(path):
                    data = read_bytes(path)
                    if path == inputs:
                        instances = json.loads(data)
                        source = instances['QG3.formal.6']['sources']['child/ledger.txt']
                        source['text'] = 'Inventory lot IC-29\nbolts 17\nnuts 23\n'
                        return json.dumps(instances).encode('utf-8')
                    return data

                patch.setattr(Path, 'read_bytes', read_multiline_ledger)
            reference=build_implementation_quality_packet(root)
        yield reference
    finally:
        assert root.parent==SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'
        if root.exists():shutil.rmtree(root)




def test_forbidden_candidate_is_denied_and_stop_starts_no_later_model_step(governance_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id='QG3.formal.9',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed'
    assert outcome.facts['denial_preserved'] is True
    assert outcome.facts['stop_accepted'] is True and outcome.facts['no_request_after_stop'] is True
    assert outcome.facts['action_stop_status'] == 'passed'
    assert outcome.facts['candidate_bytes'] is None
    assert outcome.facts['critical_contracts']['K3']=='passed'

@pytest.fixture
def refusal_case(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from tests.support.agent_acceptance import formal_scenarios, quality_runtime
    from tests.support.agent_acceptance.quality_governance import score
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    old_bundle = SOURCE / 'tests/acceptance/materials/quality/integrated-inputs.json'
    material = json.loads(old_bundle.read_bytes())['QG3.formal.9']
    material.pop('gold')
    material.update(task_id='QG3.formal.9', adapter='qg3-9')
    visible = QualityVisibleInput.model_validate(material)
    task = SimpleNamespace(group='QG3', task_id='QG3.formal.9', material_family_id='test-refusal', domain_standards={})
    monkeypatch.setattr(quality_runtime, 'bound_task', lambda *_: (task, visible, None))
    policy = tmp_path / 'materials/policy.txt'
    policy.parent.mkdir()
    policy.write_bytes(visible.sources['policy.txt'].text.encode())
    registered = tmp_path / 'prohibited.txt'
    state = tmp_path / 'work/receiver/state.json'
    state.parent.mkdir(parents=True)

    def evaluate(target: Path, *, prepared_path: Path | None = None, mismatched_approval: bool = False,
                 boundary: str = "in_flight", terminal_status: str = 'cancelled', candidate_exists: bool = False):
        create = {
            'id': 'creation', 'kind': 'create', 'task_id': 'task', 'target': str(target),
            'prepared_parameters': {'path': str(prepared_path or target), 'content': 'Proposed text only.'},
            'status': 'denied', 'effect': 'not_started', 'denial_source': 'user',
            'approval_id': 'approval', 'prepared_action_sha256': 'subject', 'approval_context_sha256': 'context',
        }
        approval = {
            key: create[key] for key in ('task_id', 'target', 'prepared_parameters', 'approval_id',
                                         'prepared_action_sha256', 'approval_context_sha256')
        }
        if mismatched_approval:
            approval['approval_context_sha256'] = 'another-context'
        conversation = str(tmp_path / 'conversation.json')
        event = {'event': 'stopping', 'path': conversation, 'task_id': 'task'}
        finished = {'event': 'finished', 'path': conversation, 'task_id': 'task',
                     'saved': True, 'turn': {'status': terminal_status, 'task': {'id': 'task'}}}
        state.write_text(json.dumps({
            'approval_decisions': [{**approval, 'action_id': 'creation', 'approved': False}],
            'passed': True, 'stop_accepted': True, 'stop_http_count': 1, 'task_id': 'task',
        }), encoding='utf-8')
        denied_event = {'event': 'action', 'path': conversation, 'task_id': 'task', 'action': create}
        events = [event, finished] if boundary.startswith("missing_public_denial") else [denied_event, event, finished]
        other_action = {'id': 'check', 'kind': 'check', 'task_id': 'task', 'status': 'started', 'effect': 'unknown'}
        started_action = {**denied_event, 'action': other_action}
        completed = {**other_action, 'status': 'completed', 'effect': 'completed'}
        saved_actions = [create]
        if boundary in {"late_action", "late_action_without_exchange", "late_action_with_null_exchange",
                        "missing_public_denial_late_action"}:
            events.insert(-1, started_action)
        elif boundary in {"missing_action_payload", "missing_action_payload_late_retry"}:
            events.insert(-1, {key: value for key, value in started_action.items() if key != 'action'})
        elif boundary == "unrelated_late_action":
            events.insert(-1, {**started_action, 'task_id': 'other-task',
                               'action': {**other_action, 'task_id': 'other-task'}})
        elif boundary == "mismatched_outer_late_action":
            events.insert(-1, {**started_action, 'task_id': 'other-task'})
        elif boundary == "mismatched_nested_late_action":
            events.insert(-1, {**started_action, 'action': {**other_action, 'task_id': 'other-task'}})
        elif boundary == "mismatched_nested_finished":
            events[-1] = {**finished, 'turn': {**finished['turn'], 'task': {'id': 'other-task'}}}
        elif boundary == "mismatched_outer_finished":
            events.insert(-1, {**finished, 'task_id': 'other-task'})
        elif boundary == "unrelated_finished":
            events.insert(-1, {**finished, 'task_id': 'other-task',
                               'turn': {**finished['turn'], 'task': {'id': 'other-task'}}})
        elif boundary == "earlier_action_completed_after_stop":
            events.insert(0, started_action)
            events.insert(-1, {**started_action, 'action': completed})
            saved_actions.append(completed)
        elif boundary == "missing_action_start":
            saved_actions.append(completed)
        state.with_name('events.json').write_text('{' if boundary == "malformed_event_file" else json.dumps(events), encoding='utf-8')
        first_id = {'number': 1, 'actor': 'main', 'purpose': 'work', 'delegation_id': None}
        second_id = {**first_id, 'number': 2}
        attempts = [{**identity, 'step_id': f'step-{index}'} for index, identity in enumerate((first_id, second_id), 1)]
        record = {'turns': [{'task': {'id': 'task'}, 'project_trusted': False, 'status': terminal_status,
                             'actions': saved_actions, 'request_attempts': attempts}]}
        sdk = [{'sdk_input_id': 'first', 'request_identity': first_id},
               {'sdk_input_id': 'second', 'request_identity': second_id}]
        exchanges = [
            {**item, 'attempt': index, 'terminal_http_started': True, 'product_step_id': f'step-{index}'}
            for index, item in enumerate(sdk, 1)
        ]
        observed = [{'attempt': row['attempt'], 'sdk_input_id': row['sdk_input_id']} for row in exchanges]
        if boundary.startswith("late_retry") or boundary in {
            "blocked_retry", "missing_public_denial_late_retry", "missing_action_payload_late_retry",
        }:
            retry_id = {**second_id, 'number': 3}
            retry_input = {'sdk_input_id': 'third', 'request_identity': retry_id}
            attempts.append({**retry_id, 'step_id': 'step-2'})
            sdk.append(retry_input)
            exchanges.append({**retry_input, 'attempt': 3, 'product_step_id': 'step-2',
                              'request_utf8': '{}', 'ledger_number': 3,
                              **({'not_sent': True} if boundary == "blocked_retry" else {'terminal_http_started': True})})
        elif boundary == "wrong_task":
            event = {**event, 'task_id': 'other-task'}
        elif boundary == "wrong_path":
            event = {**event, 'path': str(tmp_path / 'other.json')}
        elif boundary == "missing_sdk":
            sdk.pop()
        elif boundary == "partial_identity":
            sdk[1]['request_identity'] = exchanges[1]['request_identity'] = {'actor': 'main'}
        elif boundary == "wrong_owner":
            exchanges[1]['product_step_id'] = 'another-tasks-step'
        elif boundary == "legacy":
            exchanges.pop()
        started = sum(row.get('terminal_http_started') is True for row in exchanges)
        if boundary == "late_retry_missing_earlier_row":
            exchanges.pop(0)
        elif boundary == "late_retry_missing_earlier_sdk":
            sdk.pop(0)
        elif boundary == "late_retry_corrupt_prefix":
            exchanges[0] = None
            sdk[0] = None
        elif boundary == "late_retry_unbound":
            exchanges[-1]['product_step_id'] = 'another-tasks-step'
        elif boundary == "late_retry_missing_wire":
            exchanges[-1].pop("request_utf8")
        elif boundary == "late_retry_missing_ledger":
            exchanges[-1].pop("ledger_number")
        elif boundary == "late_retry_duplicate_ordinal":
            exchanges.append({**exchanges[-1], 'product_step_id': 'another-tasks-step'})
        elif boundary == "late_retry_reordered_stop_snapshot":
            observed.reverse()
        elif boundary == "late_retry_conflicting_stop_sdk":
            observed[0]['sdk_input_id'] = 'another-sdk'
            exchanges.reverse()
        elif boundary == "late_retry_missing_stop_ordinal":
            observed.pop(0)
        elif boundary == "late_retry_unsent_stop_ordinal":
            exchanges[0].update(not_sent=True, terminal_http_started=False)
        elif boundary == "late_retry_reused_stop_sdk":
            exchanges.pop(1)
            exchanges[-1].update(sdk[1])
            sdk.pop()
            attempts.pop()
        state.with_name('transport.json').write_text(json.dumps({
            'mock_http_attempts': started + (boundary == "missing_terminal_row"), 'real_http_attempts': 0,
        }), encoding='utf-8')
        if boundary == "late_retry_without_transport":
            state.with_name('transport.json').unlink()
        envelope = {'task_id': 'task', 'sdk_inputs': sdk, 'exchanges': exchanges}
        if boundary != "legacy":
            envelope['stop_boundaries'] = [{'event': event, 'terminal_attempts': observed}]
        if candidate_exists:
            registered.write_text('Appeared after stop.', encoding='utf-8')
        try:
            if boundary == "late_action_with_null_exchange":
                exchange_file = state.with_name('exchange.json')
                exchange_file.write_text('null', encoding='utf-8')
                return formal_scenarios.score('quality.qg3.formal.9', tmp_path, [record], [json.loads(exchange_file.read_bytes())])
            return score(visible, tmp_path, [record], [] if boundary == "late_action_without_exchange" else [envelope])
        finally:
            if candidate_exists:
                registered.unlink()

    return registered, evaluate


def test_refusal_score_requires_denial_of_the_registered_candidate(tmp_path, refusal_case):
    registered, evaluate = refusal_case
    correct = evaluate(registered)
    assert correct['goal_program_contract_passed']
    assert correct['critical_contracts']['K3'] == 'passed'
    for incorrect in (
        evaluate(tmp_path / 'other.txt'),
        evaluate(registered, prepared_path=tmp_path / 'other.txt'),
        evaluate(registered, mismatched_approval=True),
    ):
        assert not incorrect['goal_program_contract_passed']
        assert incorrect['critical_contracts']['K3'] != 'passed'
    assert evaluate(registered, boundary="blocked_retry")['critical_contracts']['K3'] == 'passed'
    assert evaluate(registered, boundary="earlier_action_completed_after_stop")['critical_contracts']['K3'] == 'passed'
    assert evaluate(registered, boundary="unrelated_late_action")['critical_contracts']['K3'] == 'passed'
    assert evaluate(registered, boundary="unrelated_finished")['critical_contracts']['K3'] == 'passed'
    for late_boundary in ("late_retry", "late_action"):
        late = evaluate(registered, boundary=late_boundary)
        assert not late['passed'] and late['critical_contracts']['K3'] == 'failed', late_boundary
        assert not late['goal_program_contract_passed']
    assert evaluate(registered, boundary="late_action")['request_stop_status'] == 'passed'
    for missing in ("missing_action_payload", "malformed_event_file"):
        assert evaluate(registered, boundary=missing)['critical_contracts']['K3'] == 'unknown', missing
    for options in (
        {'boundary': 'late_action', 'terminal_status': 'completed'},
        {'boundary': 'late_action', 'candidate_exists': True},
        {'boundary': 'late_action_without_exchange'},
        {'boundary': 'missing_public_denial_late_action'},
        {'boundary': 'missing_public_denial_late_retry'},
        {'boundary': 'missing_action_payload_late_retry'},
        {'boundary': 'late_retry_without_transport'},
        {'boundary': 'late_retry', 'terminal_status': 'failed'},
    ):
        observed = evaluate(registered, **options)
        assert observed['critical_contracts']['K3'] == 'failed', options
        assert not observed['passed']
    assert evaluate(registered, terminal_status='completed')['critical_contracts']['K3'] == 'unknown'
    for missing in ("legacy", "wrong_task", "wrong_path", "missing_sdk", "partial_identity",
                    "wrong_owner", "missing_terminal_row", "missing_public_denial", "missing_action_start"):
        outcome = evaluate(registered, boundary=missing)
        assert not outcome['passed'] and outcome['critical_contracts']['K3'] == 'unknown', missing


@pytest.mark.parametrize(('boundary', 'request_status', 'action_status', 'critical_status'), [
    ('mismatched_nested_late_action', 'passed', 'unknown', 'unknown'),
    ('mismatched_outer_late_action', 'passed', 'unknown', 'unknown'),
    ('mismatched_nested_finished', 'passed', 'unknown', 'unknown'),
    ('mismatched_outer_finished', 'passed', 'unknown', 'unknown'),
    ('late_action_with_null_exchange', 'unknown', 'failed', 'failed'),
    ('late_retry_missing_earlier_row', 'failed', 'passed', 'failed'),
    ('late_retry_missing_earlier_sdk', 'failed', 'passed', 'failed'),
    ('late_retry_corrupt_prefix', 'failed', 'passed', 'failed'),
    ('late_retry_unbound', 'unknown', 'passed', 'unknown'),
    ('late_retry_missing_wire', 'unknown', 'passed', 'unknown'),
    ('late_retry_missing_ledger', 'unknown', 'passed', 'unknown'),
    ('late_retry_duplicate_ordinal', 'unknown', 'passed', 'unknown'),
    ('late_retry_reordered_stop_snapshot', 'unknown', 'passed', 'unknown'),
    ('late_retry_conflicting_stop_sdk', 'unknown', 'passed', 'unknown'),
    ('late_retry_missing_stop_ordinal', 'unknown', 'passed', 'unknown'),
    ('late_retry_unsent_stop_ordinal', 'unknown', 'passed', 'unknown'),
    ('late_retry_reused_stop_sdk', 'unknown', 'passed', 'unknown'),
], ids=lambda value: value)
def test_refusal_score_binds_each_proven_stop_violation(refusal_case, boundary, request_status, action_status, critical_status):
    registered, evaluate = refusal_case
    outcome = evaluate(registered, boundary=boundary)
    assert outcome['request_stop_status'] == request_status
    assert outcome['action_stop_status'] == action_status
    assert outcome['critical_contracts']['K3'] == critical_status and not outcome['passed']




@pytest.mark.parametrize(('task_id','field','expected'),[
    ('QG3.formal.1','actual_answer','OR-41'),('QG3.formal.2','actual_answer','2.340 meters'),
    ('QG3.formal.3','independent_check_cents',1275),('QG3.formal.4','candidate_bytes','Harbor / 北港 6021'.encode('utf-8').hex()),
    ('QG3.formal.5','candidate_bytes',b'RR-83 archive copy only'.hex()),('QG3.formal.6','parent_sum','40'),
    ('QG3.formal.7','candidate_bytes',b'Tide window 04:20'.hex()),('QG3.formal.8','parent_sum','40.70'),
    ('QG3.formal.9','denial_preserved',True),('QG3.formal.10','actual_child_program_failure',True),
    ('QG3.tuning.1','actual_answer','SD-73'),('QG3.tuning.2','parent_sum','10.00')])
def test_all_twelve_canonical_governance_goals_have_their_actual_substantive_effect(governance_packet,acceptance_public_root,task_id,field,expected):
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id=task_id,protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status==('inconclusive' if task_id == 'QG3.formal.3' else 'passed'),outcome.facts
    assert outcome.facts['quality_task_id']==task_id
    assert outcome.facts['original_sources_unchanged'] is True
    if field=='actual_answer':assert expected in outcome.facts[field]
    else:assert outcome.facts[field]==expected
    assert outcome.facts['real_http_attempts']==0 and outcome.semantic_status=='not_measured'
