"""Explicit diagnostic for all 130 native judge exchanges in one parent."""

from contextlib import ExitStack
from pathlib import Path

from tests.acceptance.judge_test_material import judge_test_material, register_judge_parent_cleanup
from tests.acceptance.test_judge_qualification import candidate, samples
from tests.support.agent_acceptance.judge_qualification import freeze_judge_study, qualification_report

def test_native_judge_package_runs_full_30_then_100_with_one_parent(prepared_root, acceptance_public_root, request):
    import asyncio
    import json
    from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, FrozenParentBudget, LiveAuthorization, LiveGate, Reply, prepare_live, run_plan, export_review, review_report
    from tests.support.agent_acceptance.judge_qualification import judge_executions, register_judge_study
    source = Path(__file__).resolve().parents[2]
    stack = ExitStack()
    request.addfinalizer(stack.close)
    local = stack.enter_context(judge_test_material(source))
    calibration = samples(local / 'calibration', 'cal', 6)
    qualification = samples(local / 'qualification', 'qual', 20)
    study = freeze_judge_study(local / 'study', calibration=calibration, qualification=qualification,
        candidate=candidate(local), mode='synthetic_test')
    rows = judge_executions(study)
    fixed_replies = {row.output_id:Reply(text=json.dumps({'conclusions':row.fixture_reference.conclusions,
        'reasons':{'sufficiency':'Synthetic terminal response.'}})) for row in [*calibration,*qualification]}
    registry = CaseRegistry()
    register_judge_study(registry, study, fixed_replies=fixed_replies)
    parent = FrozenParentBudget(parent_id='synthetic-judge-'+prepared_root.name, accounting_root=prepared_root,
        request_cap=130, phase_microusd={'screening':0,'quality':0,'judge':130*122880}, reserve_microusd=0, profile='synthetic')
    plan = AcceptancePlan(executions=rows, parent_budget=parent)
    bundle = prepare_live(plan.model_copy(update={'mode':'prepare-live'}), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id='synthetic-judge-only', binding=bundle.binding, total_limit=130,
        case_limits={row.execution_id:1 for row in rows}, total_microusd=parent.total_microusd,
        case_microusd={row.execution_id:122880 for row in rows}, transport='mock', parent_budget_id=parent.parent_id)
    gate = LiveGate(prepared_root, registry, grant)
    register_judge_parent_cleanup(request, gate.parent_binding_path(parent.parent_id),
                                  parent_id=parent.parent_id, accounting_root=prepared_root)
    result = asyncio.run(run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate))
    report = qualification_report(study, result_path=result.result_path)
    assert report['fixture_status'] == 'qualified'
    assert report['actual_qualification'] == 'not-started'
    assert report['calibration']['completed_rounds'] == 1
    assert report['qualification']['agreements'] == 100
    assert gate.accounting_report()['actual_http'] == 130
    assert {row['role'] for row in gate.accounting_report()['attempts']} == {'judge'}
    exchange = json.loads(result.cases[-1].evidence['exchange'].read_bytes())
    identity = exchange['sdk_inputs'][0]['request_identity']
    assert identity['actor'] == 'judge' and identity['purpose'] == 'qualification'
    assert identity['judge_call_id'] == gate.accounting_report()['attempts'][-1]['judge_call_id']
    wire = exchange['exchanges'][0]['request_utf8']
    assert 'gemini-2.5-pro' in exchange['exchanges'][0]['url']
    assert 'fixture_reference' not in wire and 'Synthetic expected ordinary sufficiency only.' not in wire
    payload=json.loads(json.loads(wire)['contents'][0]['parts'][0]['text'])
    assert payload['actual_input']==qualification[-1].input_path.read_text(encoding='utf-8')
    assert payload['output']==qualification[-1].output_path.read_text(encoding='utf-8')
    package = export_review(result.result_path, acceptance_public_root / 'judge-review')
    review = review_report(package)
    assert review['outputs'] == [] and review['case_results'] == []
    assert len(review['judge_call_results']) == 130
    again = asyncio.run(run_plan(plan, root=acceptance_public_root / 'reopen', registry=registry, live_gate=gate))
    assert qualification_report(study, result_path=again.result_path)['qualification']['agreements'] == 100
    assert gate.accounting_report()['actual_http'] == 130
    original=result.cases[-1].evidence['exchange']
    body=original.read_bytes()
    try:
        original.unlink()
        missing=qualification_report(study,result_path=result.result_path)
    finally:
        original.write_bytes(body)
    assert missing['qualification']['planned']==100 and missing['qualification']['unknown']==1
    assert missing['qualification']['agreements']==99 and missing['fixture_status']=='incomplete'
    assert gate.accounting_report()['actual_http']==130
