from pathlib import Path

from tests.support.agent_acceptance.frozen import SOURCE


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
