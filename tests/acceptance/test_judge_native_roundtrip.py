import asyncio
from base64 import b64decode, b64encode
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path

from tests.acceptance.judge_test_material import judge_test_material
from tests.acceptance.test_judge_qualification import candidate, samples
from tests.support.agent_acceptance import (
    AcceptancePlan,
    CaseRegistry,
    FrozenParentBudget,
    LiveAuthorization,
    LiveGate,
    Reply,
    prepare_live,
    run_plan,
)
from tests.support.agent_acceptance.core import CaseOutcome
from tests.support.agent_acceptance.judge_qualification import (
    freeze_judge_study,
    judge_executions,
    qualification_report,
    register_judge_study,
)


def test_native_judge_sends_one_calibration_and_one_qualification(
    prepared_root, acceptance_public_root, request,
):
    source = Path(__file__).resolve().parents[2]
    stack = ExitStack()
    request.addfinalizer(stack.close)
    root = stack.enter_context(judge_test_material(source))
    calibration = samples(root / 'cal', 'cal', 6)
    qualification = samples(root / 'qual', 'qual', 20)
    reference = root / 'human-reference-only.txt'
    reference_text = 'HUMAN_REFERENCE_LABEL_AND_REASON_SENTINEL'
    reference.write_text(reference_text, encoding='utf-8')
    selected_samples = (calibration, qualification)
    for group in selected_samples:
        group[0] = group[0].model_copy(update={
            'original_paths': (*group[0].original_paths, reference),
            'review_only_paths': (reference,),
        })
    study = freeze_judge_study(
        root / 'study', calibration=calibration, qualification=qualification,
        candidate=candidate(root), mode='synthetic_test',
    )
    executions = judge_executions(study)
    calibration_execution, qualification_execution = executions[0], executions[30]
    selected = (calibration_execution.execution_id, qualification_execution.execution_id)
    prerequisites = executions[1:30]
    assert len(executions) == 130
    assert qualification_execution.dependencies == tuple(row.execution_id for row in executions[:30])

    samples_sent = (calibration[0], qualification[0])
    decisions = {sample.output_id: json.dumps({
        'conclusions': sample.fixture_reference.conclusions,
        'reasons': {'sufficiency': 'Synthetic terminal response.'},
    }) for sample in samples_sent}
    registry = CaseRegistry()
    register_judge_study(registry, study, fixed_replies={
        output_id: Reply(text=decision) for output_id, decision in decisions.items()
    })
    per_call_microusd = 122880
    parent = FrozenParentBudget(
        parent_id='synthetic-judge-native-' + prepared_root.name, accounting_root=prepared_root,
        request_cap=130, phase_microusd={'screening': 0, 'quality': 0, 'judge': 130 * per_call_microusd},
        reserve_microusd=0, profile='synthetic',
    )
    plan = AcceptancePlan(executions=executions, parent_budget=parent)
    bundle = prepare_live(plan.model_copy(update={'mode': 'prepare-live'}), root=prepared_root, registry=registry)
    grant = LiveAuthorization(
        authorization_id='synthetic-judge-native', binding=bundle.binding, total_limit=130,
        case_limits={row.execution_id: 1 for row in executions}, total_microusd=parent.total_microusd,
        case_microusd={row.execution_id: per_call_microusd for row in executions},
        transport='mock', parent_budget_id=parent.parent_id,
    )
    gate = LiveGate(prepared_root, registry, grant)
    parent_index = gate.parent_binding_path(parent.parent_id)
    assert not parent_index.exists()

    def release_parent_index():
        if parent_index.exists():
            assert parent_index.resolve(strict=True) == parent_index
            record = json.loads(parent_index.read_bytes())
            assert record['parent_id'] == parent.parent_id
            assert Path(record['accounting_root']) == prepared_root
            parent_index.unlink()

    request.addfinalizer(release_parent_index)
    for row in prerequisites:
        gate.for_execution(row.execution_id).finish_execution(CaseOutcome(
            'passed', {'provenance': 'synthetic prerequisite only; no SDK evidence'},
            case_id=row.case_id, protocol=row.protocol, execution_id=row.execution_id,
        ))
    assert gate.accounting_report()['reserved_requests'] == 0

    result = asyncio.run(run_plan(
        plan, root=acceptance_public_root, registry=registry, live_gate=gate, execution_ids=selected,
    ))
    accounting = gate.accounting_report()
    assert accounting['parent_budget_id'] == parent.parent_id
    assert accounting['actual_http'] == accounting['reserved_requests'] == accounting['completed_responses'] == 2
    assert set(accounting['completed_executions']) == {*selected, *(row.execution_id for row in prerequisites)}
    assert [attempt['execution_id'] for attempt in accounting['attempts']] == list(selected)
    outcomes = {case.execution_id: case for case in result.cases if case.execution_id in selected}
    for attempt, sample, purpose in zip(accounting['attempts'], samples_sent, ('calibration', 'qualification'), strict=True):
        outcome = outcomes[attempt['execution_id']]
        assert outcome.product_status == 'passed'
        assert attempt['state'] == 'response_complete' and attempt['role'] == 'judge'
        exchange = json.loads(outcome.evidence['exchange'].read_bytes())
        assert exchange['task_id'] == attempt['execution_id'] and exchange['protocol'] == 'google'
        sdk_input, = exchange['sdk_inputs']
        wire, = exchange['exchanges']
        identity = sdk_input['request_identity']
        assert identity == wire['request_identity']
        assert identity['actor'] == 'judge' and identity['purpose'] == purpose
        assert identity['judge_call_id'] == attempt['judge_call_id']
        assert sdk_input['sdk_input_id'] == wire['sdk_input_id'] == attempt['sdk_input_id']
        assert wire['ledger_number'] == attempt['number'] and wire['transport_provenance'] == 'fixed_fixture'
        assert wire['status'] == 200 and 'gemini-2.5-pro' in wire['url']
        sent = wire['request_utf8']
        assert sha256(sent.encode('utf-8')).hexdigest() == attempt['request_sha256']
        sdk_messages = json.dumps(sdk_input['messages'], ensure_ascii=False)
        for input_text in (sent, sdk_messages):
            assert 'fixture_reference' not in input_text and reference.name not in input_text
            assert reference_text not in input_text and b64encode(reference.read_bytes()).decode() not in input_text
            assert sample.fixture_reference.reasons['sufficiency'] not in input_text
        native_request = json.loads(sent)
        payload = json.loads(native_request['contents'][0]['parts'][0]['text'])
        assert payload['actual_input'] == sample.input_path.read_text(encoding='utf-8')
        assert payload['output'] == sample.output_path.read_text(encoding='utf-8')
        assert len(payload['original_evidence']) == 2
        response = b64decode(wire['received_base64'], validate=True)
        assert response == wire['received_utf8'].encode('utf-8')
        assert json.loads(response)['candidates'][0]['content']['parts'][0]['text'] == decisions[sample.output_id]

    report = qualification_report(study, result_path=result.result_path)
    assert report['actual_qualification'] == 'not-started'
    assert report['calibration']['completed_rounds'] == 0
    assert report['qualification']['planned'] == 100
    assert report['qualification']['agreements'] == 1 and report['qualification']['unknown'] == 99
    assert {row['execution_id'] for row in report['judge_call_results'] if row['issue'] is None} == set(selected)

    reopened = LiveGate(prepared_root, registry, grant)
    asyncio.run(run_plan(plan, root=root / 'reopen', registry=registry, live_gate=reopened, execution_ids=selected))
    assert reopened.accounting_report() == accounting
