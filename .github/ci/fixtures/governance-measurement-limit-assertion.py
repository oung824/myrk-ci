from tests.support.agent_acceptance.frozen import SOURCE


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
