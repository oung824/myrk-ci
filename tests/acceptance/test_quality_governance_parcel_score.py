"""The plan-and-extension scorer uses the authorized table and recorded check."""

from hashlib import sha256
import subprocess

import pytest

from myrk.checks import CheckFileVersion, CheckReceipt
from tests.support.agent_acceptance import quality_governance
from tests.support.agent_acceptance.quality import named_answer_review_resolved
from tests.support.agent_acceptance.quality_runtime import QualityVisibleInput


@pytest.fixture
def score_parcel_work(tmp_path):
    def score(*, second_amount: str, answer: str, evidence_change: str | None = None):
        visible = QualityVisibleInput.model_validate({
            'task_id': 'QG3.formal.3', 'adapter': 'qg3-3',
            'goal': 'Discover the extension and independently verify the parcel total.',
            'sources': {'parcels.csv': {'text': f'item,amount\nA,4.60\nB,{second_amount}\n'}},
            'parameters': {'plan': ['Read parcel records', 'Check their total'], 'extension': 'calculate_table'},
        })
        quality_governance.setup(visible, tmp_path, 'deepseek')
        table = tmp_path / 'materials/parcels.csv'
        table.write_bytes(visible.sources['parcels.csv'].text.encode('utf-8'))
        check = quality_governance.manifest(visible, tmp_path, 'deepseek')['scope']['checks'][0]
        script = table.with_name('independent_check.py')
        if evidence_change == 'different script':
            with script.open('ab') as file:
                file.write(b'\n# An unregistered version.\n')
        inputs = {str(path): CheckFileVersion(sha256=sha256(data).hexdigest(), size_bytes=len(data))
                  for path in (script, table) for data in [path.read_bytes()]}
        completed = subprocess.run(check['argv'], cwd=table.parent, capture_output=True, check=True)
        receipt_path = tmp_path / 'records/check.json'
        receipt_path.parent.mkdir()
        stdout_path = receipt_path.with_suffix('.stdout')
        raw_stdout = (b'{"cents": 1276, "rows": 2}\r\n' if evidence_change == 'inline stdout'
                      else b'{"cents":' if evidence_change == 'malformed stdout' else completed.stdout)
        stdout_path.write_bytes(raw_stdout)
        recorded_stdout = (raw_stdout if evidence_change == 'malformed stdout' else completed.stdout
                           ).decode('utf-8').replace('\r\n', '\n')
        receipt = CheckReceipt(action_id='check', exit_code=completed.returncode, argv=check['argv'],
                               cwd=str(table.parent), stdout_sha256=sha256(raw_stdout).hexdigest(),
                               input_files_before=inputs.copy(), input_files_after=inputs.copy())
        if evidence_change == 'wrong hash':
            receipt.stdout_sha256 = '0' * 64
        if evidence_change == 'old action':
            receipt.action_id = 'different-check'
        if evidence_change == 'different command':
            receipt.argv = [*check['argv'][:-1], str(table.with_name('different.csv'))]
        if evidence_change == 'missing input evidence':
            receipt.input_files_before.clear()
            receipt.input_files_after.clear()
        if evidence_change == 'changed table after check':
            receipt.input_files_after[str(table)] = CheckFileVersion(sha256='0' * 64, size_bytes=table.stat().st_size)
        if evidence_change == 'script missing before check':
            del receipt.input_files_before[str(script)]
        receipt_path.write_text('{' if evidence_change == 'malformed receipt' else receipt.model_dump_json(), encoding='utf-8')
        actions = [
            {'id': 'calculate', 'kind': 'calculate', 'status': 'completed', 'effect': 'completed',
             'tool_source': 'extension', 'finished_at': '01'},
            {'id': 'check', 'kind': 'check', 'status': 'completed', 'effect': 'completed', 'started_at': '02',
             'target': check['name'], 'prepared_parameters': {name: value for name, value in check.items() if name != 'name'},
             'cwd': str(table.parent),
             'receipt_path': str(receipt_path), 'stdout_path': str(stdout_path), 'exit_code': completed.returncode,
             'stdout': recorded_stdout, 'cleanup_complete': True},
        ]
        if evidence_change == 'missing receipt path':
            del actions[-1]['receipt_path']
        if evidence_change == 'invalid stdout path':
            actions[-1]['stdout_path'] = {'path': str(stdout_path)}
        calls = ['update_plan', 'read_text', 'search_extensions', 'describe_extension', 'call_extension', 'run_check']
        record = {'turns': [{'actions': actions, 'answer': answer, 'status': 'completed', 'project_trusted': False,
                             'plan': visible.parameters['plan']}],
                  'messages': [{'parts': [{'part_kind': 'tool-call', 'tool_name': name} for name in calls]}]}
        return quality_governance.score(visible, tmp_path, [record], [])

    return score


def test_scores_the_total_of_the_current_authorized_table(score_parcel_work):
    result = score_parcel_work(second_amount='8.20', answer='The parcel total is 12.80.')

    assert result['goal_program_contract_passed'] is True
    assert result['independent_check_cents'] == 1280
    assert result['fact_contract_status'] == result['critical_contracts']['K4'] == 'unknown'
    assert result['named_answer_review_required'] is True


@pytest.mark.parametrize('answer', [
    'The parcel total is 112.75. The 12.75 figure is explicitly incorrect.',
    'The program check never ran. The total is 12.75.',
    'The two entries come to twelve dollars and seventy-five cents.',
])
def test_leaves_reported_amount_and_check_claims_to_named_review(score_parcel_work, answer):
    result = score_parcel_work(second_amount='8.15', answer=answer)

    assert result['goal_program_contract_passed'] is True
    assert result['independent_check_cents'] == 1275
    assert result['fact_contract_status'] == result['critical_contracts']['K4'] == 'unknown'


@pytest.mark.parametrize('evidence_change', [
    'inline stdout', 'wrong hash', 'old action', 'different command', 'missing input evidence',
    'changed table after check', 'script missing before check', 'different script',
    'missing receipt path', 'invalid stdout path', 'malformed receipt', 'malformed stdout',
])
def test_requires_a_check_receipt_bound_to_the_work_inputs_and_stdout(score_parcel_work, evidence_change):
    result = score_parcel_work(second_amount='8.15', answer='The parcel total is 12.75.',
                               evidence_change=evidence_change)

    assert result['goal_program_contract_passed'] is False
    assert result['independent_check_cents'] is None
    assert result['named_answer_review_required'] is False


def test_completed_named_review_resolves_only_the_answer_left_pending_after_verified_program_facts():
    facts = {'named_answer_review_required': True, 'fact_contract_status': 'unknown',
             'goal_program_contract_passed': True, 'quality_task_permissions': {'status': 'passed'}}
    original = {'product_status': 'inconclusive', 'facts': facts}
    review = {'case_status': 'passed', 'target_basis': 'named_human_review'}

    assert named_answer_review_resolved(original, review)
    assert not named_answer_review_resolved(original, {**review, 'case_status': 'pending_review'})
    assert not named_answer_review_resolved(original, {**review, 'target_basis': 'original_program_result'})
    assert not named_answer_review_resolved({**original, 'product_status': 'failed'}, review)
    for incomplete in ({'quality_task_permissions': {'status': 'unknown'}},
                       {'named_answer_review_required': False}, {'goal_program_contract_passed': False}):
        assert not named_answer_review_resolved({**original, 'facts': {**facts, **incomplete}}, review)
