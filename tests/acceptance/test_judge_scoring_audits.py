"""Ordinary judge scoring and pre-frozen random human audits."""
from pathlib import Path
import json
import os
import sys

import pytest

from tests.support.agent_acceptance.judge_scoring import AuditOutput, freeze_audit_plan


TYPES = ('main_answer', 'child_report', 'review_reason', 'memory', 'summary')


@pytest.fixture
def sampling_python():
    from myrk.project_paths import project_root
    default = Path(sys.executable) if sys.version_info[:3] == (3,13,0) else project_root(Path(__file__)) / '.scratch/judge-sampling-python-3.13.0/runtime/python.exe'
    path = Path(os.environ.get('MYRK_JUDGE_SAMPLING_PYTHON', str(default)))
    if not path.is_file():
        pytest.skip('The explicitly frozen CPython3.13.0 audit helper is unavailable.')
    return path


@pytest.mark.parametrize(('counts', 'expected', 'total'), [
    ((50, 20, 10, 8, 2), (10, 4, 3, 3, 2), 22),
    ((2, 1, 0, 0, 0), (2, 1, 0, 0, 0), 3),
    ((30, 0, 0, 0, 0), (6, 0, 0, 0, 0), 10),
])
def test_random_audit_freezes_all_five_type_denominators_and_reproduces_ids(tmp_path, sampling_python, counts, expected, total):
    outputs = [AuditOutput(output_id=f'{kind}-{n}', output_type=kind) for kind, size in zip(TYPES, counts) for n in range(size)]
    path = freeze_audit_plan(tmp_path/'first.json', outputs, sampling_python=sampling_python)
    first = json.loads(path.read_bytes())
    other = freeze_audit_plan(tmp_path/'second.json', outputs, sampling_python=sampling_python)
    assert first['selected_ids'] == json.loads(other.read_bytes())['selected_ids']
    assert [first['by_type'][kind]['base_sample_count'] for kind in TYPES] == list(expected)
    assert first['actual_sample_count'] == total
    assert len(set(first['selected_ids'])) == total
    assert first['input_ids'] == [row.output_id for row in outputs]
    assert first['seed'] == 20261116 and first['algorithm'] == 'CPython-3.13.0-random.Random.sample'
    assert first['sampling_runtime'].startswith('3.13.0')
    if counts[0] == 30:
        assert first['by_type']['main_answer']['base_selected_ids'] == ['main_answer-6', 'main_answer-29', 'main_answer-20', 'main_answer-12', 'main_answer-10', 'main_answer-16']
        assert first['selected_ids'] == ['main_answer-6', 'main_answer-29', 'main_answer-20', 'main_answer-12', 'main_answer-10', 'main_answer-16', 'main_answer-25', 'main_answer-13', 'main_answer-23', 'main_answer-0']



def scoring_outputs(root: Path, count: int, kind='main_answer'):
    from tests.support.agent_acceptance.judge_scoring import ScoringOutput
    root.mkdir(parents=True)
    source = root/'source.txt'
    source.write_text('Synthetic legitimate task input.', encoding='utf-8')
    output = root/'output.txt'
    output.write_text('Synthetic complete output.', encoding='utf-8')
    return [ScoringOutput(output_id=f'{kind}-{number}', output_type=kind, source_task_id=f'product-{number}',
        family_id=f'scoring-family-{number}', input_path=source, output_path=output, original_paths=(source, output),
        ordinary_dimensions=('sufficiency',), source_kind='fixture_execution', language='zh', domain='synthetic')
        for number in range(count)]


def qualified_fixture(root: Path):
    from tests.acceptance.test_judge_qualification import samples, candidate
    from tests.support.agent_acceptance.judge_qualification import freeze_judge_study
    root.mkdir()
    cal, qual = samples(root/'cal', 'cal', 6), samples(root/'qual', 'qual', 20)
    config = candidate(root)
    study = freeze_judge_study(root/'study', calibration=cal, qualification=qual, candidate=config, mode='synthetic_test')
    return study, config, {row.output_id:dict(row.fixture_reference.conclusions) for row in qual}


def test_scoring_uses_qualification_scope_and_cap_without_turning_fixtures_into_real_qualification(scoring_source, sampling_python):
    tmp_path = scoring_source
    from tests.support.agent_acceptance.judge_scoring import freeze_judge_scoring, scoring_executions, scoring_report
    study, config, predictions = qualified_fixture(tmp_path/'qualification')
    outputs = scoring_outputs(tmp_path/'outputs', 321)
    path = freeze_judge_scoring(tmp_path/'scoring', outputs=outputs, candidate=config, qualification_study=study,
        synthetic_predictions=predictions, mode='synthetic_test', sampling_python=sampling_python)
    frozen = json.loads(path.read_bytes())
    assert len(scoring_executions(path)) == 320
    assert frozen['samples'][-1]['route'] == 'human' and frozen['samples'][-1]['reason'] == 'scoring_cap_320'
    assert frozen['audit']['by_type']['main_answer']['N'] == 320
    report = scoring_report(path)
    assert report['planned_outputs'] == report['unknown'] == 321
    assert report['actual_qualification'] == 'not-started' and report['user_acceptance'] == 'pending'
    actual = freeze_judge_scoring(tmp_path/'actual', outputs=outputs[:1], candidate=config, qualification_study=study,
        mode='actual', sampling_python=sampling_python)
    assert scoring_executions(actual) == ()
    assert scoring_report(actual)['outputs'][0]['route'] == 'human'
    assert json.loads(actual.read_bytes())['audit']['actual_sample_count'] == 0
    renamed = tmp_path/'same-prompt.txt'
    renamed.write_bytes(config.prompt_path.read_bytes())
    scoped = config.model_copy(update={'prompt_path':renamed})
    outputs[0] = outputs[0].model_copy(update={'language':'en'})
    other = freeze_judge_scoring(tmp_path/'new-scope', outputs=outputs[:2], candidate=scoped, qualification_study=study,
        synthetic_predictions=predictions, mode='synthetic_test', sampling_python=sampling_python)
    assert len(scoring_executions(other)) == 1
    assert json.loads(other.read_bytes())['samples'][0]['reason'] == 'scope_not_verified'
    baseline = freeze_judge_scoring(tmp_path/'baseline', outputs=outputs[:2], candidate=config, qualification_study=study,
        synthetic_predictions=predictions, mode='synthetic_test', sampling_python=sampling_python, baseline_all_human=True)
    assert scoring_executions(baseline) == ()
    assert json.loads(baseline.read_bytes())['audit']['actual_sample_count'] == 0
    guard_outputs = [row.model_copy(update={'language':'zh','new_risk_scope':number==0,'disputed':number==1}) for number,row in enumerate(outputs[:3])]
    guarded = freeze_judge_scoring(tmp_path/'guarded',outputs=guard_outputs,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=sampling_python)
    assert [row['reason'] for row in json.loads(guarded.read_bytes())['samples']] == ['new_risk_scope','disputed',None]
    config.prompt_path.write_text('Substantively changed scoring prompt.',encoding='utf-8')
    changed = freeze_judge_scoring(tmp_path/'changed',outputs=guard_outputs,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=sampling_python)
    assert scoring_executions(changed) == ()
    assert all(row['reason']=='candidate_refreeze_required' for row in json.loads(changed.read_bytes())['samples'])



@pytest.fixture
def scoring_source():
    from uuid import uuid4
    import shutil
    source = Path(__file__).resolve().parents[2]
    root = source/'.scratch/retro-agent-live-acceptance-20261116/implementation/t23/tmp'/uuid4().hex
    root.mkdir(parents=True)
    yield root
    assert root.resolve().is_relative_to((source/'.scratch/retro-agent-live-acceptance-20261116/implementation/t23/tmp').resolve())
    shutil.rmtree(root)


def test_native_scoring_boundary_outputs_share_existing_judge_parent_and_preserve_unknowns(prepared_root, acceptance_public_root, scoring_source, sampling_python, request):
    import asyncio
    from hashlib import sha256
    from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, FrozenParentBudget, LiveAuthorization, LiveGate, Reply, prepare_live, run_plan, export_review, review_report
    from tests.support.agent_acceptance.judge_qualification import judge_executions, register_judge_study
    from tests.support.agent_acceptance.judge_scoring import freeze_judge_scoring, scoring_executions, register_judge_scoring, scoring_report
    study, config, predictions = qualified_fixture(scoring_source/'qualification')
    outputs = scoring_outputs(scoring_source/'outputs', 321)
    path = freeze_judge_scoring(scoring_source/'scoring', outputs=outputs, candidate=config,
        qualification_study=study, synthetic_predictions=predictions, mode='synthetic_test', sampling_python=sampling_python)
    registry = CaseRegistry()
    qualification_call = judge_executions(study)[0]
    qualification_call = qualification_call.model_copy(update={'order':1})
    qualification_id = json.loads(study.read_bytes())['judge_inputs'][0]['output_id']
    register_judge_study(registry, study, fixed_replies={qualification_id:Reply(text=json.dumps({'conclusions':{'sufficiency':'passed'},'reasons':{'sufficiency':'Synthetic qualification transport sentinel.'}}))})
    replies = {row.output_id:Reply(text=json.dumps({'conclusions':{'sufficiency':'passed' if number % 2 else 'failed'},
        'reasons':{'sufficiency':'Synthetic terminal scoring.'}})) for number,row in enumerate(outputs[:320])}
    register_judge_scoring(registry, path, fixed_replies=replies, preceding_cases=('judge.calibration',))
    last = scoring_executions(path)[-2:]
    rows = (qualification_call, *(row.model_copy(update={'order':index, 'dependencies':(qualification_call.execution_id,)}) for index,row in enumerate(last,2)))
    parent = FrozenParentBudget(parent_id='synthetic-scoring-'+prepared_root.name, accounting_root=prepared_root,
        request_cap=480, phase_microusd={'screening':0,'quality':0,'judge':60_000_000},reserve_microusd=0,profile='synthetic')
    parent_index = Path('E:/Myrk/.scratch/acceptance-parent-budgets')/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    def cleanup_parent():
        if parent_index.exists():
            assert parent_index.parent == Path('E:/Myrk/.scratch/acceptance-parent-budgets') and not parent_index.is_symlink()
            parent_index.unlink()
    request.addfinalizer(cleanup_parent)
    plan = AcceptancePlan(executions=rows,parent_budget=parent)
    bundle = prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
    grant = LiveAuthorization(authorization_id='synthetic-scoring-only',binding=bundle.binding,total_limit=3,
        case_limits={row.execution_id:1 for row in rows},total_microusd=3*122880,
        case_microusd={row.execution_id:122880 for row in rows},transport='mock',parent_budget_id=parent.parent_id)
    gate = LiveGate(prepared_root,registry,grant)
    result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    report = scoring_report(path,result_path=result.result_path)
    assert report['planned_outputs'] == 321 and report['judge_completed_outputs'] == 2
    assert report['audit']['by_type']['main_answer']['N'] == 320
    assert report['audit']['actual_sample_count'] == 64
    assert report['unknown'] == 321
    assert gate.accounting_report()['actual_http'] == 3
    attempts = gate.accounting_report()['attempts']
    assert sum(row['purpose']=='scoring' for row in attempts) == 2
    assert {row['role'] for row in attempts} == {'judge'}
    package = export_review(result.result_path,acceptance_public_root/'review')
    common = review_report(package)
    assert common['outputs'] == [] and common['case_results'] == [] and len(common['judge_call_results']) == 3
    again = asyncio.run(run_plan(plan,root=acceptance_public_root/'reopen',registry=registry,live_gate=gate))
    assert scoring_report(path,result_path=again.result_path)['judge_completed_outputs'] == 2
    assert gate.accounting_report()['actual_http'] == 3
    original = result.cases[-1].evidence['exchange']
    body = original.read_bytes()
    try:
        original.unlink()
        missing = scoring_report(path,result_path=result.result_path)
    finally:
        original.write_bytes(body)
    assert missing['planned_outputs'] == 321 and missing['judge_completed_outputs'] == 1
    assert missing['outputs'][-2]['judge_original']['conclusions'] is None
    assert gate.accounting_report()['actual_http'] == 3





def test_audit_errors_pause_only_the_affected_type_and_preserve_original_decisions(prepared_root, acceptance_public_root, scoring_source, sampling_python, request):
    import asyncio
    from hashlib import sha256
    from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, FrozenParentBudget, LiveAuthorization, LiveGate, Reply, prepare_live, run_plan
    from tests.support.agent_acceptance.judge_scoring import FixtureAudit, freeze_judge_scoring, scoring_executions, register_judge_scoring, scoring_report
    study, config, predictions = qualified_fixture(scoring_source/'qualification')
    outputs = [*scoring_outputs(scoring_source/'main',20), *scoring_outputs(scoring_source/'child',2,'child_report')]
    outputs = [row.model_copy(update={'deterministic_status':'passed'}) for row in outputs]
    path = freeze_judge_scoring(scoring_source/'scoring',outputs=outputs,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=sampling_python)
    selected = json.loads(path.read_bytes())['audit']['selected_ids']
    rejected = [identity for identity in selected if identity.startswith('main_answer')][:2]
    registry = CaseRegistry()
    replies = {row.output_id:Reply(text=json.dumps({'conclusions':{'sufficiency':'failed' if row.output_id in rejected else 'passed'},
        'reasons':{'sufficiency':'Synthetic terminal scoring.'}})) for row in outputs}
    register_judge_scoring(registry,path,fixed_replies=replies)
    rows = scoring_executions(path)
    parent = FrozenParentBudget(parent_id='synthetic-audits-'+prepared_root.name,accounting_root=prepared_root,request_cap=480,
        phase_microusd={'screening':0,'quality':0,'judge':60_000_000},reserve_microusd=0,profile='synthetic')
    index=Path('E:/Myrk/.scratch/acceptance-parent-budgets')/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    request.addfinalizer(lambda: index.unlink(missing_ok=True))
    plan=AcceptancePlan(executions=rows,parent_budget=parent)
    bundle=prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-audit-only',binding=bundle.binding,total_limit=len(rows),
        case_limits={row.execution_id:1 for row in rows},total_microusd=len(rows)*122880,
        case_microusd={row.execution_id:122880 for row in rows},transport='mock',parent_budget_id=parent.parent_id)
    gate=LiveGate(prepared_root,registry,grant)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    critical={name:'passed' for name in outputs[0].mandatory_human_dimensions}
    reviews=[FixtureAudit(output_id=identity,ordinary_conclusions={'sufficiency':'passed'},critical_conclusions=critical,
        reason='Explicit synthetic audit expected verdict.') for identity in selected]
    report=scoring_report(path,result_path=result.result_path,fixture_reviews=reviews)
    main=report['by_type']['main_answer']
    assert main['false_rejects']==2 and main['sampled_human_pass_denominator']==len([i for i in selected if i.startswith('main_answer')])
    assert main['paused'] and main['pause_reason']=='confirmed_false_rejects_over_10_percent'
    assert len(main['recheck_ids'])==20 and not report['by_type']['child_report']['paused']
    assert report['actual_human_reviews']==[] and len(report['fixture_reviews'])==len(selected)
    first=next(row for row in report['outputs'] if row['output_id']==rejected[0])
    assert first['judge_original']['conclusions']['sufficiency']=='failed'
    assert first['fixture_ordinary']['sufficiency']=='passed' and first['human_ordinary']=={} and first['fixture_final']=='passed' and first['final']=='unknown'
    assert first['actual_dimensions']['sufficiency']=='unknown' and first['ordinary_conclusion_sources']['sufficiency']=='unknown'
    assert not first['judge_original']['real_http']
    passed=next(identity for identity in selected if identity.startswith('main_answer') and identity not in rejected)
    false_accept=[FixtureAudit(output_id=passed,ordinary_conclusions={'sufficiency':'failed'},critical_conclusions=critical,
        reason='Explicit synthetic false acceptance.')]
    accepted=scoring_report(path,result_path=result.result_path,fixture_reviews=false_accept)
    assert accepted['by_type']['main_answer']['false_accepts']==1
    assert accepted['by_type']['main_answer']['paused'] and len(accepted['by_type']['main_answer']['recheck_ids'])==18
    assert rejected[0] not in accepted['by_type']['main_answer']['recheck_ids']
    failed_critical=FixtureAudit(output_id=passed,ordinary_conclusions={'sufficiency':'passed'},
        critical_conclusions={**critical,'K1':'failed'},reason='Explicit synthetic critical failure.')
    critical_report=scoring_report(path,result_path=result.result_path,fixture_reviews=[failed_critical])
    affected=next(row for row in critical_report['outputs'] if row['output_id']==passed)
    assert affected['fixture_final']=='failed' and affected['final']=='unknown'
    assert critical_report['planned_outputs']==22 and critical_report['audit']['actual_sample_count']==10
    assert gate.accounting_report()['actual_http']==22


def test_no_sampler_or_qualification_gap_keeps_the_all_human_path_usable(scoring_source, sampling_python):
    from tests.support.agent_acceptance.judge_scoring import ScoringOutput, freeze_judge_scoring, scoring_executions
    study, config, predictions=qualified_fixture(scoring_source/'qualification')
    outputs=scoring_outputs(scoring_source/'outputs',3)
    with pytest.raises(ValueError,match='critical|mandatory'):
        ScoringOutput(**{**outputs[0].model_dump(),'mandatory_human_dimensions':()})
    with pytest.raises(ValueError,match='literal|Input should'):
        ScoringOutput(**{**outputs[0].model_dump(),'ordinary_dimensions':('authorization',)})
    missing=scoring_source/'missing-python.exe'
    path=freeze_judge_scoring(scoring_source/'missing-sampler',outputs=outputs,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=missing)
    assert scoring_executions(path)==()
    assert all(row['reason']=='sampling_runtime_unavailable' for row in json.loads(path.read_bytes())['samples'])
    baseline=freeze_judge_scoring(scoring_source/'baseline-no-sampler',outputs=outputs,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=missing,baseline_all_human=True)
    assert scoring_executions(baseline)==()
    assert json.loads(baseline.read_bytes())['audit']['sampling_runtime'] is None
    oversized=scoring_outputs(scoring_source/'oversized',1)
    oversized[0].output_path.write_text('Synthetic complete oversized original.'*3000,encoding='utf-8')
    original=oversized[0].output_path.read_bytes()
    fallback=freeze_judge_scoring(scoring_source/'unfit',outputs=oversized,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=sampling_python)
    packet=json.loads(fallback.read_bytes())
    assert scoring_executions(fallback)==() and packet['samples'][0]['reason']=='complete_input_unfit'
    assert (fallback.parent/packet['samples'][0]['output_path']).read_bytes()==original


@pytest.mark.parametrize('mode',['unknown_id','no_authorization','malformed'])
def test_native_scoring_rejects_unknown_ids_and_missing_authority_without_replacement_calls(prepared_root, acceptance_public_root, scoring_source, sampling_python, request, mode):
    import asyncio
    from hashlib import sha256
    from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, FrozenParentBudget, LiveAuthorization, LiveGate, Reply, SharedControlError, prepare_live, run_plan
    from tests.support.agent_acceptance.judge_scoring import freeze_judge_scoring, scoring_executions, register_judge_scoring, scoring_report
    study, config, predictions=qualified_fixture(scoring_source/'qualification')
    outputs=scoring_outputs(scoring_source/'outputs',1)
    path=freeze_judge_scoring(scoring_source/'scoring',outputs=outputs,candidate=config,qualification_study=study,
        synthetic_predictions=predictions,mode='synthetic_test',sampling_python=sampling_python)
    registry=CaseRegistry()
    register_judge_scoring(registry,path,fixed_replies={outputs[0].output_id:Reply(text='Malformed explicit synthetic response')})
    rows=scoring_executions(path)
    if mode=='unknown_id':
        rows=(rows[0].model_copy(update={'execution_id':'judge:scoring:unregistered','scenario_id':'judge:scoring:unregistered','startpoint':'judge:scoring:unregistered'}),)
    parent=FrozenParentBudget(parent_id='synthetic-controls-'+prepared_root.name,accounting_root=prepared_root,request_cap=480,
        phase_microusd={'screening':0,'quality':0,'judge':60_000_000},reserve_microusd=0,profile='synthetic')
    index=Path('E:/Myrk/.scratch/acceptance-parent-budgets')/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    request.addfinalizer(lambda:index.unlink(missing_ok=True))
    plan=AcceptancePlan(executions=rows,parent_budget=parent)
    bundle=prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-controls-only',binding=bundle.binding,total_limit=1,
        case_limits={rows[0].execution_id:1},total_microusd=122880,case_microusd={rows[0].execution_id:122880},
        transport='mock',parent_budget_id=parent.parent_id)
    gate=LiveGate(prepared_root,registry,None if mode=='no_authorization' else grant)
    if mode in {'unknown_id','no_authorization'}:
        with pytest.raises(SharedControlError,match='output ID' if mode=='unknown_id' else 'authorization|binding'):
            asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
        assert gate.accounting_report()['actual_http']==0
    else:
        result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
        report=scoring_report(path,result_path=result.result_path)
        assert report['planned_outputs']==report['unknown']==1
        assert report['outputs'][0]['judge_original']['conclusions'] is None
        assert gate.accounting_report()['actual_http']==1
        assert gate.accounting_report()['unknown_fee_requests']==1
