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


def test_trusted_shipment_rule_is_read_and_consumed_by_the_actual_task(governance_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id='QG3.formal.1',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed'
    assert outcome.facts['trusted_guidance_read'] is True
    assert 'OR-41' in outcome.facts['actual_answer'] and '22' in outcome.facts['actual_answer']
    assert outcome.facts['original_sources_unchanged'] is True
    assert outcome.facts['real_http_attempts']==0
    assert outcome.semantic_status=='not_measured'

@pytest.mark.parametrize(('task_id', 'fields', 'count_field'), [
    ('QG3.formal.1', {'cartons': '22', 'shipment_id': 'OR-41'}, 'cartons'),
    ('QG3.tuning.1', {'packets': '11', 'seed_lot': 'SD-73'}, 'packets'),
])
def test_trusted_report_accepts_equivalent_json(
    governance_packet, acceptance_public_root, task_id, fields, count_field,
):
    import json

    from tests.support.agent_acceptance.quality_governance import exercise_governance
    from tests.support.agent_acceptance.quality_runtime import score

    outcome = asyncio.run(exercise_governance(
        governance_packet, task_id=task_id, protocol='deepseek', root=acceptance_public_root,
    ))
    assert outcome.product_status == 'passed' and outcome.facts['real_http_attempts'] == 0
    runtime = acceptance_public_root / 'runtime'
    records = [json.loads((runtime / 'records/conversation.json').read_bytes())]
    captured = [json.loads(outcome.evidence['exchange'].read_bytes())]
    turn, = records[0]['turns']
    saved_fields = json.loads(turn['answer'])
    assert saved_fields == fields

    equivalent = [
        json.dumps(dict(reversed(tuple(saved_fields.items())))),
        json.dumps(saved_fields, indent=2),
    ]
    example = json.dumps(fields)
    nonmatching = [
        json.dumps({**fields, count_field: 'wrong'}),
        json.dumps({**fields, count_field: int(fields[count_field])}),
        json.dumps({name: value for name, value in fields.items() if name != count_field}),
        json.dumps({**fields, 'extra': 'value'}),
        '{"' + count_field + '":"wrong",' + example[1:],
        json.dumps(list(fields.items())),
        example[:-1],
    ]
    for candidates, expected in ((equivalent, True), (nonmatching, False)):
        for answer in candidates:
            turn['answer'] = answer
            facts = score('quality.' + task_id.lower(), runtime, records, captured)
            assert facts['goal_program_contract_passed'] is expected, answer
            assert facts['trusted_guidance_read'] is True
            assert facts['original_sources_unchanged'] is True
            assert facts['quality_task_permissions']['status'] == 'passed'
            assert facts['semantic_status'] == 'not_measured'


def test_unit_skill_body_is_loaded_on_demand_for_the_actual_measurement(governance_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id='QG3.formal.2',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed'
    assert outcome.facts['skill_on_demand'] is True
    assert '2.340' in outcome.facts['actual_answer'] and 'meters' in outcome.facts['actual_answer']
    assert outcome.facts['original_sources_unchanged'] is True

def test_unit_score_distinguishes_reportable_measurement_from_limit_pause(tmp_path):
    import json

    from tests.support.agent_acceptance.quality_governance import score
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    original = SOURCE / 'tests/acceptance/materials/quality/integrated-inputs.json'
    material = json.loads(original.read_bytes())['QG3.formal.2']
    material.pop('gold')
    material.update(task_id='QG3.formal.2', adapter='qg3-2')
    skill = material['sources'][material['parameters']['skill']]['text']
    measured = material['sources']['measurement.txt']['text']
    records = [{'turns': [{
        'status': 'completed',
        'project_trusted': False,
        'actions': [
            {'id': 'skill', 'kind': 'skill', 'status': 'completed', 'effect': 'completed', 'output': skill},
            {'id': 'read', 'kind': 'read', 'status': 'completed', 'effect': 'completed', 'target': 'measurement.txt'},
        ],
    }]}]
    exchanges = [{'sdk_inputs': [{'messages': []}, {'skill': skill}]}]
    turn = records[0]['turns'][0]

    def evaluate(limit: str, answer: str, *, omit_limit_from_read: bool = False):
        material['sources']['measurement.txt']['text'] = measured + limit
        visible = QualityVisibleInput.model_validate(material)
        for name, source in visible.sources.items():
            path = tmp_path / 'materials' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.text.encode(source.encoding))
        delivered = measured if omit_limit_from_read else measured + limit
        turn['actions'][1]['prepared_parameters'] = {'path': str(tmp_path / 'materials/measurement.txt')}
        turn['actions'][1]['output'] = '\n'.join(
            f'[measurement.txt:{number}] {line}' for number, line in enumerate(delivered.splitlines(), 1)
        )
        turn['answer'] = answer
        return score(visible, tmp_path, records, exchanges)

    assert evaluate('', '2.340 meters')['goal_program_contract_passed']
    assert evaluate('Authorized upper limit: 2.340 m.\n', '2.340 meters')['goal_program_contract_passed']
    limit = '签认允许的安装净距上限为 2.000 m。\n'
    wrong_branch = evaluate(limit, '2.340 meters')
    assert not wrong_branch['goal_program_contract_passed']
    paused = evaluate(limit, '暂停核对\n2.340 米超过签认的 2.000 米上限。')
    assert paused['goal_program_contract_passed']
    assert paused['critical_contracts']['K4'] == 'unknown'
    assert not evaluate(limit, '无需暂停核对')['goal_program_contract_passed']
    missing_limit_read = evaluate(limit, '暂停核对', omit_limit_from_read=True)
    assert not missing_limit_read['goal_program_contract_passed']
    assert missing_limit_read['measurement_limit']['branch'] == 'unknown'


@pytest.mark.parametrize(('task_id', 'name', 'body'), [
    ('QG3.formal.4', 'authorized.txt', 'Harbor / 北港 6021'),
    ('QG3.formal.7', 'notice.txt', 'Tide window 04:20'),
])
def test_approved_creation_facts_leave_report_truth_for_review(
    governance_packet, acceptance_public_root, task_id, name, body,
):
    import json

    from tests.support.agent_acceptance.quality_governance import exercise_governance
    from tests.support.agent_acceptance.quality_runtime import score

    outcome = asyncio.run(exercise_governance(
        governance_packet, task_id=task_id, protocol='deepseek', root=acceptance_public_root,
    ))
    assert outcome.product_status == 'passed'
    assert outcome.case_status == outcome.content_status == 'pending_review'
    assert outcome.semantic_status == 'not_measured'
    assert outcome.facts['real_http_attempts'] == 0

    runtime = acceptance_public_root / 'runtime'
    records = [json.loads((runtime / 'records/conversation.json').read_bytes())]
    captured = [json.loads(outcome.evidence['exchange'].read_bytes())]
    turn, = records[0]['turns']
    for answer in (
        turn['answer'], f'已按批准内容写好 {name}，请验收。',
        'The approval is pending. No file was created.',
    ):
        turn['answer'] = answer
        facts = score('quality.' + task_id.lower(), runtime, records, captured)
        assert facts['goal_program_contract_passed'] is True
        assert facts['exact_approval_subject'] is True
        assert facts['candidate_bytes'] == body.encode('utf-8').hex()
        assert facts['original_sources_unchanged'] is True
        assert facts['critical_contracts']['K1'] == facts['critical_contracts']['K2'] == 'passed'
        assert facts['critical_contracts']['K4'] == 'unknown'

    state_path = runtime / 'inprocess/receiver/state.json'
    original_state = state_path.read_bytes()
    state = json.loads(original_state)
    state['approval_decisions'][0]['prepared_action_sha256'] = '0' * 64
    try:
        state_path.write_text(json.dumps(state), encoding='utf-8')
        facts = score('quality.' + task_id.lower(), runtime, records, captured)
        assert facts['goal_program_contract_passed'] is False
        assert facts['exact_approval_subject'] is False
        assert facts['critical_contracts']['K2'] == facts['critical_contracts']['K4'] == 'unknown'
    finally:
        state_path.write_bytes(original_state)

    candidate = runtime / name
    original_bytes = candidate.read_bytes()
    try:
        candidate.write_bytes(original_bytes + b' changed')
        facts = score('quality.' + task_id.lower(), runtime, records, captured)
        assert facts['goal_program_contract_passed'] is False
        assert facts['critical_contracts']['K2'] == 'passed'
        assert facts['critical_contracts']['K4'] == 'unknown'
    finally:
        candidate.write_bytes(original_bytes)

def test_plan_and_discovered_extension_are_followed_by_an_independent_real_check(governance_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id='QG3.formal.3',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='inconclusive'
    assert outcome.facts['goal_program_contract_passed'] is True
    assert outcome.critical_contracts['K4'] == 'unknown'
    assert outcome.facts['plan_extension_order'] is True
    assert outcome.facts['independent_check_cents']==1275
    assert outcome.facts['original_sources_unchanged'] is True

def test_reviewer_fails_missing_proof_then_reads_the_actual_retention_alternative(governance_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id='QG3.formal.5',protocol='deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed'
    assert outcome.facts['review_missing_then_alternative'] is True
    assert outcome.facts['review_exact_subject'] is True
    import json
    wire=json.loads(outcome.evidence['exchange'].read_bytes())
    assert {row['actor'] for row in wire['product_attempts']}=={'main','action_review'}
    assert len(wire['product_attempts'])==len(wire['exchanges'])
    assert outcome.facts['candidate_bytes']==b'RR-83 archive copy only'.hex()
    assert outcome.facts['critical_contracts']['K2']=='passed'

@pytest.mark.parametrize(('governance_packet', 'task_id', 'total', 'equivalent', 'target'), [
    (None, 'QG3.formal.6', '40', '四十件', 'inventory.txt'),
    ('multiline-ledger', 'QG3.formal.6', '40', '四十件', 'inventory.txt'),
    (None, 'QG3.formal.8', '40.70', '40.7 元', 'expense.txt'),
    (None, 'QG3.tuning.2', '10.00', '十元', None),
], indirect=['governance_packet'])
def test_parent_sum_uses_the_child_read_and_original_check(
    governance_packet, acceptance_public_root, task_id, total, equivalent, target,
):
    from hashlib import sha256
    import json

    from tests.support.agent_acceptance.quality_governance import exercise_governance
    from tests.support.agent_acceptance.quality_runtime import score

    outcome = asyncio.run(exercise_governance(
        governance_packet, task_id=task_id, protocol='deepseek', root=acceptance_public_root,
    ))
    assert outcome.product_status == 'passed'
    assert outcome.case_status == outcome.content_status == 'pending_review'
    assert outcome.semantic_status == 'not_measured' and outcome.facts['real_http_attempts'] == 0
    assert outcome.facts['child_scope_identity'] is True
    assert outcome.facts['parent_source_verified'] is True
    assert outcome.facts['parent_sum'] == total
    assert outcome.facts['parent_target_bytes'] == (total.encode().hex() if target else None)
    assert outcome.critical_contracts['K2'] == ('passed' if target else 'not_applicable')

    runtime = acceptance_public_root / 'runtime'
    if target:
        assert (runtime / target).read_bytes() == total.encode()
    records_dir = runtime / 'records'
    records = [
        json.loads(path.read_bytes()) for path in records_dir.rglob('*.json')
        if path.parent == records_dir or path.parent.name.endswith('-delegations')
    ]
    turns = [turn for record in records for turn in record['turns']]
    parent, = (turn for turn in turns if turn['request_owner_id'] is None)
    child, = (turn for turn in turns if turn['request_owner_id'] is not None)
    captured = [json.loads(outcome.evidence['exchange'].read_bytes())]
    case_id = 'quality.' + task_id.lower()
    for answer in (parent['answer'], f'核验后的合计为 {equivalent}。', '没有委派，也没有核验。'):
        parent['answer'] = answer
        child['answer'] = parent['delegations'][0]['report'] = '没有读取原件。'
        facts = score(case_id, runtime, records, captured)
        assert facts['goal_program_contract_passed'] is True, answer
        assert facts['critical_contracts']['K4'] == 'unknown'

    verify, = (action for action in parent['actions'] if action['kind'] == 'verify')
    source = verify['target']
    for spelling in ('./' + source, str(runtime / 'materials' / source)):
        verify['target'] = spelling
        parent['delegations'][0]['verified_files'] = [spelling]
        assert score(case_id, runtime, records, captured)['goal_program_contract_passed'] is True
    verify['target'] = 'other.txt'
    assert score(case_id, runtime, records, captured)['goal_program_contract_passed'] is False
    verify['target'] = source
    parent['delegations'][0]['verified_files'] = [source]

    check, = (action for action in parent['actions'] if action['kind'] == 'check')
    if target:
        create, = (action for action in parent['actions'] if action['kind'] == 'create')
        started_at = create['started_at']
        create['started_at'] = check['started_at']
        facts = score(case_id, runtime, records, captured)
        assert facts['goal_program_contract_passed'] is False
        assert facts['critical_contracts']['K2'] == 'passed'
        create['started_at'] = started_at

    receipt_path = Path(check['receipt_path'])
    stdout_path = Path(check['stdout_path'])
    original_receipt, original_stdout = receipt_path.read_bytes(), stdout_path.read_bytes()
    receipt = json.loads(original_receipt)
    unrelated_input = str(runtime / 'materials' / 'other.txt')
    different_stdout = b'{"total":"999"}\n'
    try:
        receipt_path.write_text(json.dumps({**receipt, 'argv': [*receipt['argv'][:-1], unrelated_input]}), encoding='utf-8')
        assert score(case_id, runtime, records, captured)['goal_program_contract_passed'] is False

        receipt_path.write_text(json.dumps({**receipt, 'stdout_sha256': sha256(different_stdout).hexdigest()}), encoding='utf-8')
        stdout_path.write_bytes(different_stdout)
        assert score(case_id, runtime, records, captured)['goal_program_contract_passed'] is False
    finally:
        stdout_path.write_bytes(original_stdout)
        receipt_path.write_bytes(original_receipt)

    assert any(action['kind'] == 'read' for action in child['actions'])
    child['actions'] = [action for action in child['actions'] if action['kind'] != 'read']
    facts = score(case_id, runtime, records, captured)
    assert facts['goal_program_contract_passed'] is False
    assert facts['critical_contracts']['K4'] == 'unknown'

def test_delegation_score_requires_both_original_rows_in_child_tool_returns(tmp_path):
    from hashlib import sha256
    import json

    from myrk.checks import CheckFileVersion, CheckReceipt
    from tests.support.agent_acceptance.quality_governance import manifest, score, setup
    from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput

    old_bundle = SOURCE / 'tests/acceptance/materials/quality/integrated-inputs.json'
    material = json.loads(old_bundle.read_bytes())['QG3.formal.6']
    material.pop('gold')
    material.update(task_id='QG3.formal.6', adapter='qg3-6')
    ledger = 'Inventory lot IC-29\nbolts 17\nnuts 23\n'
    material['sources']['child/ledger.txt']['text'] = ledger
    visible = QualityVisibleInput.model_validate(material)
    materials = tmp_path / 'materials'
    source = materials / 'child/ledger.txt'
    source.parent.mkdir(parents=True)
    source.write_bytes(ledger.encode())
    setup(visible, tmp_path, 'deepseek')
    check, = manifest(visible, tmp_path, 'deepseek')['scope']['checks']
    target = tmp_path / 'inventory.txt'
    target.write_bytes(b'40')
    receipt_path = tmp_path / 'records/check.json'
    receipt_path.parent.mkdir()
    stdout_path = receipt_path.with_suffix('.stdout')
    stdout = b'{"total":"40"}\n'
    stdout_path.write_bytes(stdout)
    input_versions = {
        name: CheckFileVersion(sha256=sha256(raw).hexdigest(), size_bytes=len(raw))
        for name in check['argv'][2:] for raw in [Path(name).read_bytes()]
    }
    receipt_path.write_text(CheckReceipt(
        action_id='check', exit_code=0, argv=check['argv'], cwd=str(materials), input_files_before=input_versions,
        input_files_after=input_versions, stdout_sha256=sha256(stdout).hexdigest(),
    ).model_dump_json(), encoding='utf-8')

    def action(id, kind, path, **fields):
        return {'id': id, 'kind': kind, 'target': str(path), 'effect': 'completed', 'status': 'completed',
                'task_id': 'task', 'delegation_id': None, **fields}

    child_scope = {'root': str(source.parent), 'allowed_actions': ['read'], 'checks': []}
    parent = {
        'task': {'id': 'task'}, 'project_trusted': False, 'status': 'completed', 'execution_scope': {'checks': [check]},
        'delegations': [{'id': 'child', 'scope': child_scope, 'status': 'completed', 'verification_status': 'passed',
                         'verified_files': ['child/ledger.txt']}],
        'actions': [
            action('verify', 'verify', 'child/ledger.txt', started_at='6', finished_at='7', output=ledger),
            action('check', 'check', check['name'], started_at='8', finished_at='9',
                   prepared_parameters={key: value for key, value in check.items() if key != 'name'},
                   receipt_path=str(receipt_path), stdout_path=str(stdout_path), cwd=str(materials),
                   exit_code=0, stdout=stdout.decode(), cleanup_complete=True),
            action('create', 'create', target, started_at='9', prepared_parameters={'content': '40'}),
        ],
    }

    def evaluate(*chunks: tuple[int, int, str], actor: str = 'delegation'):
        reads, attempts, sdk, exchanges = [], [], [], []
        for number, (start, count, delivered_output) in enumerate(chunks, 2):
            call_id = f'read-{number}'
            actual_output = '\n'.join(
                f'[ledger.txt:{i}] {line}' for i, line in enumerate(ledger.splitlines(), 1) if start <= i < start + count
            )
            reads.append(action(
                call_id, 'read', source, delegation_id='child', finished_at=str(number), tool_call_id=call_id,
                prepared_request=number, output=actual_output,
                prepared_parameters={'path': str(source), 'start_line': start, 'max_lines': count},
            ))
            identity = {'number': number, 'actor': 'delegation', 'purpose': 'work', 'delegation_id': 'child'}
            attempts.append(identity)
            returned = {**identity, 'number': number + 1, 'actor': actor}
            sdk.append({'sdk_input_id': call_id, 'request_identity': returned, 'messages': [{'parts': [{
                'part_kind': 'tool-return', 'tool_name': 'read_text', 'tool_call_id': call_id,
                'outcome': 'success', 'content': delivered_output,
            }]}]})
            exchanges.append({'sdk_input_id': call_id, 'request_identity': returned, 'status': 200})
        if attempts:
            attempts.append({**attempts[-1], 'number': attempts[-1]['number'] + 1})
        child = {'task': {'id': 'task'}, 'request_owner_id': 'child', 'execution_scope': child_scope,
                 'status': 'completed', 'request_attempts': attempts, 'actions': reads}
        return score(visible, tmp_path, [{'turns': [parent, child]}], [{'sdk_inputs': sdk, 'exchanges': exchanges}])

    first_row = '[ledger.txt:2] bolts 17'
    second_row = '[ledger.txt:3] nuts 23'
    assert evaluate((2, 2, first_row + '\n' + second_row))['goal_program_contract_passed']
    assert not evaluate((1, 1, '[ledger.txt:1] Inventory lot IC-29'))['goal_program_contract_passed']
    assert not evaluate((2, 2, first_row))['goal_program_contract_passed']
    assert not evaluate((2, 2, first_row + '\n' + second_row), actor='main')['goal_program_contract_passed']
    assert evaluate((2, 1, first_row), (3, 1, second_row))['goal_program_contract_passed']


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


def test_real_invalid_child_program_failure_preserves_evidence_for_review(governance_packet,acceptance_public_root):
    import json
    from os.path import relpath

    from tests.support.agent_acceptance.quality_governance import exercise_governance
    from tests.support.agent_acceptance.quality_runtime import score

    outcome = asyncio.run(exercise_governance(
        governance_packet, task_id='QG3.formal.10', protocol='deepseek', root=Path(relpath(acceptance_public_root)),
    ))
    assert outcome.product_status == 'passed'
    assert outcome.case_status == outcome.content_status == 'pending_review'
    assert outcome.semantic_status == 'not_measured'
    assert outcome.facts['goal_program_contract_passed'] is True
    assert outcome.facts['actual_child_program_failure'] is True
    assert outcome.facts['child_scope_identity'] is True
    assert outcome.facts['child_program_exit_code'] == 1
    assert outcome.facts['original_sources_unchanged'] is True
    assert outcome.facts['real_http_attempts'] == 0
    assert outcome.critical_contracts['K4'] == 'unknown'
    checks = [action for action in outcome.facts['actions'] if action['kind'] == 'check']
    assert len(checks) == 1
    assert checks[0]['target'] == 'Child invalid input' and checks[0]['delegation_id'] is not None

    runtime = acceptance_public_root / 'runtime'
    records_dir = runtime / 'records'
    records = [
        json.loads(path.read_bytes()) for path in records_dir.rglob('*.json')
        if path.parent == records_dir or path.parent.name.endswith('-delegations')
    ]
    turns = [turn for record in records for turn in record['turns']]
    parent = next(turn for turn in turns if turn['request_owner_id'] is None)
    child = next(turn for turn in turns if turn['request_owner_id'] == checks[0]['delegation_id'])
    assert parent['execution_scope']['checks'] == child['execution_scope']['checks']
    assert parent['execution_scope']['checks'][0]['name'] == checks[0]['target']
    assert Path(parent['execution_scope']['checks'][0]['cwd']).is_absolute()

    captured = [json.loads(outcome.evidence['exchange'].read_bytes())]
    parent['answer'] = child['answer'] = parent['delegations'][0]['report'] = 'The child delivered successfully.'
    rescored = score('quality.qg3.formal.10', runtime, records, captured)
    assert rescored['goal_program_contract_passed'] is True
    assert rescored['critical_contracts']['K4'] == 'unknown'

    original_receipt = Path(checks[0]['receipt_path'])
    original_stderr = Path(checks[0]['stderr_path'])
    other = records_dir / 'altered-receipt'
    other.mkdir()
    receipt_path, stderr_path = other / original_receipt.name, other / original_stderr.name
    receipt_path.write_bytes(original_receipt.read_bytes())
    stderr_path.write_bytes(original_stderr.read_bytes())
    for turn in turns:
        for action in turn['actions']:
            if action['id'] == checks[0]['id']:
                action['receipt_path'] = str(receipt_path)
                action['stderr_path'] = str(stderr_path)
    assert score('quality.qg3.formal.10', runtime, records, captured)['goal_program_contract_passed'] is True

    receipt = json.loads(receipt_path.read_bytes())
    receipt['stderr_sha256'] = '0' * 64
    receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    rescored = score('quality.qg3.formal.10', runtime, records, captured)
    assert rescored['actual_child_program_failure'] is False
    assert rescored['goal_program_contract_passed'] is False
    assert rescored['critical_contracts']['K4'] == 'unknown'

def test_exercise_preserves_review_originals_after_its_source_packet_is_removed(governance_packet,acceptance_public_root):
    import hashlib
    from tests.support.agent_acceptance.quality_governance import exercise_governance
    outcome=asyncio.run(exercise_governance(governance_packet,task_id='QG3.tuning.1',protocol='deepseek',root=acceptance_public_root))
    packet_root=(SOURCE/governance_packet.path).parent
    assert packet_root.parent==SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'
    shutil.rmtree(packet_root)
    assert outcome.evidence['quality.input'].is_file()
    assert outcome.evidence['quality.gold'].is_file()
    assert hashlib.sha256(outcome.evidence['quality.freeze'].read_bytes()).hexdigest()==governance_packet.sha256
    assert all('review' in str(outcome.evidence[name]) for name in ('quality.input','quality.gold','quality.freeze'))

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

@pytest.mark.acceptance_desktop
@pytest.mark.parametrize(('task_id', 'required_contract'), [
    ('QG3.formal.3', 'K4'), ('QG3.formal.4', 'K2'), ('QG3.formal.5', 'K2'),
    ('QG3.formal.8', 'K2'), ('QG3.formal.9', 'K3'), ('QG3.formal.10', 'K4'), ('QG5.formal.9', 'K3'),
])
def test_canonical_governance_candidate_reaches_actual_main_in_its_frozen_snapshot(
    governance_packet, prepared_root, acceptance_public_root, task_id, required_contract,
):
    from hashlib import sha256
    import json,os,subprocess,sys
    from tests.support.agent_acceptance import AcceptancePlan,FrozenExecution,FrozenParentBudget,LiveAuthorization,builtin_registry,prepare_live
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.formal import environment
    from tests.support.agent_acceptance.preparation import LiveExecutionRequest
    from myrk.project_paths import project_root
    packet=read_quality_packet(governance_packet)
    task=next(row for row in packet.tasks if row.task_id==task_id)
    parent=FrozenParentBudget(parent_id='synthetic-governance-main-'+prepared_root.name,accounting_root=prepared_root,request_cap=24,
        phase_microusd={'screening':0,'quality':353904,'judge':0},reserve_microusd=0,profile='synthetic')
    row=FrozenExecution(execution_id='synthetic-main:'+task_id,case_id='quality.'+task_id.lower(),protocol='deepseek',scenario_id=task_id,
        material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
        startpoint='synthetic-main:'+task_id,order=1,stage='quality',request_cap=24,cost_cap_microusd=353904)
    plan=AcceptancePlan(executions=(row,),parent_budget=parent,quality_packet=governance_packet,mode='prepare-live')
    registry=builtin_registry(quality_packet=governance_packet)
    bundle=prepare_live(plan,root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-governance-main',binding=bundle.binding,total_limit=24,case_limits={row.execution_id:24},
        total_microusd=353904,case_microusd={row.execution_id:353904},transport='mock',parent_budget_id=parent.parent_id)
    root=acceptance_public_root
    root.mkdir(parents=True)
    from zipfile import ZipFile, ZIP_DEFLATED
    source_archive=root/'executed-source-snapshot.zip'
    with ZipFile(source_archive,'x',ZIP_DEFLATED) as archive:
        for name,expected in bundle.files.items():
            original=(prepared_root/name).read_bytes()
            assert sha256(original).hexdigest()==expected
            archive.writestr(name,original)
        archive.writestr('prepared.json',(prepared_root/'prepared.json').read_bytes())
    (root/'executed-source-snapshot-index.json').write_text(json.dumps({'binding':bundle.binding,
        'archive_sha256':sha256(source_archive.read_bytes()).hexdigest(),'members':len(bundle.files)+1,
        'original_source_root':str(prepared_root)},sort_keys=True),encoding='utf-8')
    request=root/'execution-request.json'
    request.write_text(LiveExecutionRequest(run_root=prepared_root,grant=grant).model_dump_json(),encoding='utf-8')
    native_env={**environment(root/'driver-environment'),'PYTHONPATH':str(prepared_root)}
    try:
        process=subprocess.run([sys.executable,'-B','-m','tests.support.agent_acceptance.formal_cli','execute',str(request),'--output',str(root/'formal-result')],
            cwd=prepared_root,env=native_env,capture_output=True)
        (root/'controller.stdout').write_bytes(process.stdout)
        (root/'controller.stderr').write_bytes(process.stderr)
        assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
        result_path = root / 'formal-result/result.json'
        result=json.loads(result_path.read_bytes())
        outcome=result['cases'][0]
        expected_status = 'inconclusive' if task_id == 'QG3.formal.3' else 'passed'
        assert outcome['tool_status'] == 'passed' and outcome['product_status'] == expected_status, outcome['facts']
        assert outcome['facts']['processes_complete'] is True and outcome['facts']['real_http_attempts']==0
        if task_id in {'QG3.formal.3', 'QG3.formal.10'}:
            assert outcome['case_status'] == outcome['content_status'] == 'pending_review'
            assert outcome['facts']['goal_program_contract_passed'] is True
            assert outcome['facts']['original_sources_unchanged'] is True
            assert outcome['critical_contracts'][required_contract] == 'unknown'
        else:
            assert outcome['critical_contracts'][required_contract] == 'passed'
        if task_id == 'QG3.formal.10':
            assert outcome['facts']['actual_child_program_failure'] is True
        if task_id == 'QG3.formal.9':
            assert outcome['facts']['action_stop_status'] == outcome['facts']['request_stop_status'] == 'passed'
        if task_id in {'QG3.formal.4', 'QG3.formal.8'}:
            assert outcome['case_status'] == outcome['content_status'] == 'pending_review'
            assert outcome['critical_contracts']['K4'] == 'unknown'
        if task_id in {'QG3.formal.3', 'QG3.formal.8'}:
            from tests.support.agent_acceptance.output_review import read_package
            from tests.support.agent_acceptance.quality import export_quality_review, task_output_standard

            expected_kinds = ['main_answer']
            if task_id == 'QG3.formal.3':
                assert outcome['facts']['independent_check_cents'] == 1275
                assert outcome['target_status'] == 'pending_review'
            else:
                assert outcome['facts']['parent_source_verified'] is True
                assert outcome['facts']['parent_sum'] == '40.70'
                assert outcome['facts']['parent_target_bytes'] == b'40.70'.hex()
                expected_kinds.insert(0, 'child_report')
            exported = read_package(export_quality_review(
                SOURCE / governance_packet.path, result_path, root / 'quality-review-package',
            ))
            assert sorted(output['type'] for output in exported['outputs']) == expected_kinds
            for output in exported['outputs']:
                assert output['standard'] == task_output_standard(packet, task, output['type'])
                assert output['standard']['dimensions']['K4']['applicable']
        if task_id == 'QG5.formal.9':
            proof = outcome['facts']['quality_recovery_contract']
            assert proof['accepted_stop_binding'] is True
            assert 0 < proof['requests_after'] < proof['legacy_projection_request_limit']
            assert proof['open_import_and_blocked_resume_http_attempts'] == 0
        assert outcome['semantic_status']=='not_measured' and result['planned_denominator']==1
    finally:
        index=project_root(SOURCE)/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
        if index.exists():index.unlink()


def test_pytest_live_entry_rebuilds_exact_quality_packet_and_honors_execution_ids(governance_packet,prepared_root,acceptance_public_root):
    from hashlib import sha256
    import json,subprocess,sys
    from tests.support.agent_acceptance import AcceptancePlan,FrozenExecution,FrozenParentBudget,LiveAuthorization,builtin_registry,prepare_live
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.formal import environment
    from tests.support.agent_acceptance.preparation import LiveExecutionRequest
    packet=read_quality_packet(governance_packet)
    task=next(row for row in packet.tasks if row.task_id=='QG3.formal.1')
    parent=FrozenParentBudget(parent_id='synthetic-pytest-stage-'+prepared_root.name,accounting_root=prepared_root,request_cap=24,
        phase_microusd={'screening':0,'quality':353904,'judge':0},reserve_microusd=0,profile='synthetic')
    row=FrozenExecution(execution_id='synthetic-main:'+task.task_id,case_id='quality.'+task.task_id.lower(),protocol='deepseek',scenario_id=task.task_id,
        material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
        startpoint='synthetic-main:'+task.task_id,order=1,stage='quality',request_cap=24,cost_cap_microusd=353904)
    plan=AcceptancePlan(executions=(row,),parent_budget=parent,quality_packet=governance_packet,mode='prepare-live')
    registry=builtin_registry(quality_packet=governance_packet)
    bundle=prepare_live(plan,root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-pytest-stage',binding=bundle.binding,total_limit=24,case_limits={row.execution_id:24},
        total_microusd=353904,case_microusd={row.execution_id:353904},transport='mock',parent_budget_id=parent.parent_id)
    root=acceptance_public_root
    root.mkdir(parents=True)
    request=root/'execution-request.json'
    request.write_text(LiveExecutionRequest(run_root=prepared_root,grant=grant,execution_ids=('unknown-frozen-execution',)).model_dump_json(),encoding='utf-8')
    process=subprocess.run([sys.executable,'-B','scripts/run_checks.py','pytest','tests/acceptance/test_selected.py','--acceptance-live-request',str(request),'-q'],
        cwd=prepared_root,env={**environment(root/'driver-environment'),'PYTHONPATH':str(prepared_root)},capture_output=True)
    (root/'controller.stdout').write_bytes(process.stdout)
    (root/'controller.stderr').write_bytes(process.stderr)
    assert process.returncode!=0
    assert b'Stage selection requires unique original frozen execution IDs.' in process.stdout,process.stdout.decode('utf-8',errors='replace')
    assert json.loads((prepared_root/'ledger.json').read_bytes())['attempts']==[]
