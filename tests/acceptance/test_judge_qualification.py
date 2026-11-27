from pathlib import Path
import os

import pytest

from tests.support.agent_acceptance.judge_qualification import (
    FixtureReference, JudgeCandidate, JudgeSample, freeze_judge_study, qualification_report,
)


TYPES = ('main_answer', 'child_report', 'review_reason', 'memory', 'summary')


def samples(root: Path, purpose: str, per_type: int):
    root.mkdir(parents=True)
    result = []
    for kind in TYPES:
        for number in range(per_type):
            identity = f'{purpose}-{kind}-{number}'
            source = root / (identity + '.source.txt')
            output = root / (identity + '.output.txt')
            source.write_text('Synthetic material: ' + chr(0x4e00 + len(result) + (1000 if purpose == 'qual' else 0)) * 40, encoding='utf-8')
            output.write_text('Synthetic output: ' + chr(0x5000 + len(result)) * 20, encoding='utf-8')
            result.append(JudgeSample(output_id=identity, output_type=kind, source_task_id=identity,
                family_id=identity, input_path=source, output_path=output, original_paths=(source, output),
                ordinary_dimensions=('sufficiency',), source_kind='fixture_execution',
                fixture_reference=FixtureReference(conclusions={'sufficiency':'passed' if number < per_type // 2 else 'failed'},
                    reasons={'sufficiency':'Synthetic expected ordinary sufficiency only.'})))
    return result


def candidate(root: Path):
    prompt, criteria = root / 'prompt.txt', root / 'criteria.txt'
    prompt.write_text('Judge only the frozen ordinary dimensions. Return JSON conclusions and reasons.', encoding='utf-8')
    criteria.write_text('Ordinary sufficiency: the requested information is complete.', encoding='utf-8')
    return JudgeCandidate(version='candidate-v1', model='gemini-2.5-pro', prompt_path=prompt,
        criteria_path=criteria, input_processing='complete-originals-json-v1', languages=('zh',), domains=('synthetic',),
        output_types=TYPES)


def test_freeze_keeps_synthetic_reference_separate_and_requires_the_full_30_100(tmp_path):
    calibration = samples(tmp_path / 'calibration', 'cal', 6)
    qualification = samples(tmp_path / 'qualification', 'qual', 20)
    config = candidate(tmp_path)
    with pytest.raises(ValueError, match='100|quota'):
        freeze_judge_study(tmp_path / 'short', calibration=calibration, qualification=qualification[:-1],
            candidate=config, mode='synthetic_test')
    study = freeze_judge_study(tmp_path / 'study', calibration=calibration, qualification=qualification,
        candidate=config, mode='synthetic_test')
    report = qualification_report(study)
    assert report['actual_qualification'] == 'not-started'
    assert report['qualification']['planned'] == report['qualification']['unknown'] == 100
    assert report['qualification']['human_negative_denominator'] == 50
    assert report['qualification']['human_positive_denominator'] == 50
    assert report['qualification']['actual_original_outputs'] == 0
    assert report['qualification']['fixture_reference_outputs'] == 100


def test_fixture_thresholds_keep_unknown_in_the_original_denominator(tmp_path):
    from tests.support.agent_acceptance.judge_qualification import fixture_qualification_report
    calibration = samples(tmp_path / 'calibration', 'cal', 6)
    qualification = samples(tmp_path / 'qualification', 'qual', 20)
    study = freeze_judge_study(tmp_path / 'study', calibration=calibration, qualification=qualification,
        candidate=candidate(tmp_path), mode='synthetic_test')
    predictions = {row.output_id:dict(row.fixture_reference.conclusions) for row in qualification}
    for identity in ('qual-main_answer-10', 'qual-main_answer-0', 'qual-child_report-0', 'qual-memory-0', 'qual-summary-0'):
        predictions[identity]['sufficiency'] = 'failed' if identity.endswith('-0') else 'passed'
    report = fixture_qualification_report(study, predictions)
    assert report['fixture_status'] == 'qualified'
    assert report['actual_qualification'] == 'not-started'
    assert report['qualification']['false_accepts'] == 1
    assert report['qualification']['false_rejects'] == 4
    assert report['qualification']['errors'] == 5
    assert report['qualification']['actual_original_subset']['planned'] == 0
    predictions.pop('qual-review_reason-0')
    report = fixture_qualification_report(study, predictions)
    assert report['fixture_status'] == 'incomplete'
    assert report['qualification']['planned'] == 100
    assert report['qualification']['unknown'] == 1
    assert report['route'] == 'human'


def test_critical_failures_cannot_fill_ordinary_negatives_or_fake_new_families(tmp_path):
    calibration = samples(tmp_path / 'calibration', 'cal', 6)
    qualification = samples(tmp_path / 'qualification', 'qual', 20)
    config = candidate(tmp_path)
    with pytest.raises(ValueError, match='ordinary'):
        JudgeSample(**{**qualification[0].model_dump(), 'ordinary_dimensions':('K1',)})
    qualification[1].input_path.write_bytes(qualification[0].input_path.read_bytes())
    with pytest.raises(ValueError, match='family|families'):
        freeze_judge_study(tmp_path / 'copied', calibration=calibration, qualification=qualification,
            candidate=config, mode='synthetic_test')


@pytest.mark.parametrize('changed', ['false_accepts','false_rejects','one_type'])
def test_all_qualification_thresholds_apply_together(tmp_path, changed):
    from tests.support.agent_acceptance.judge_qualification import fixture_qualification_report
    cal = samples(tmp_path/'cal', 'cal', 6)
    qual = samples(tmp_path/'qual', 'qual', 20)
    study = freeze_judge_study(tmp_path/'study', calibration=cal, qualification=qual, candidate=candidate(tmp_path), mode='synthetic_test')
    predictions = {row.output_id:dict(row.fixture_reference.conclusions) for row in qual}
    selected = {'false_accepts':['qual-main_answer-10','qual-child_report-10'],
        'false_rejects':['qual-main_answer-0','qual-child_report-0','qual-review_reason-0','qual-memory-0','qual-summary-0'],
        'one_type':['qual-main_answer-0','qual-main_answer-1','qual-main_answer-2']}[changed]
    for identity in selected:
        predictions[identity]['sufficiency'] = 'passed' if changed=='false_accepts' else 'failed'
    report = fixture_qualification_report(study,predictions)
    assert report['fixture_status']=='failed' and report['route']=='human'
    assert report['qualification']['planned']==100


@pytest.mark.parametrize('defect', ['too_many_controlled','positive_originals','family_count','family_limit','calibration_overlap','tuning_copy','dimensions','third_round','actual_fixture'])
def test_material_defects_require_human_review_without_new_product_execution(tmp_path, defect):
    cal = samples(tmp_path/'cal', 'cal', 6)
    qual = samples(tmp_path/'qual', 'qual', 20)
    config = candidate(tmp_path)
    options = {'mode':'synthetic_test'}
    if defect=='too_many_controlled':
        qual = [row.model_copy(update={'source_kind':'fixture_controlled'}) if number<41 else row for number,row in enumerate(qual)]
    if defect=='positive_originals':
        qual = [row.model_copy(update={'source_kind':'fixture_controlled'}) if row.fixture_reference.conclusions['sufficiency']=='passed' else row for row in qual]
    if defect=='family_count':
        qual = [row.model_copy(update={'family_id':f'family-{number//3}'}) for number,row in enumerate(qual)]
    if defect=='family_limit':
        qual = [row.model_copy(update={'family_id':'three-members'}) if number<3 else row for number,row in enumerate(qual)]
    if defect=='calibration_overlap':
        qual[0] = qual[0].model_copy(update={'family_id':cal[0].family_id})
    if defect=='tuning_copy':
        options['tuning_inputs'] = (qual[0].input_path,)
    if defect=='dimensions':
        qual[0] = qual[0].model_copy(update={'ordinary_dimensions':('readability',),
            'fixture_reference':FixtureReference(conclusions={'readability':'passed'},reasons={})})
    if defect=='third_round':
        options['calibration_rounds'] = 3
    if defect=='actual_fixture':
        options['mode'] = 'actual'
    with pytest.raises(ValueError):
        freeze_judge_study(tmp_path/'study',calibration=cal,qualification=qual,candidate=config,**options)


def test_scope_uses_actual_candidate_bytes_and_only_affected_scope_expires(tmp_path):
    from tests.support.agent_acceptance.judge_qualification import fixture_qualification_report, qualification_applicability
    cal, qual = samples(tmp_path/'cal','cal',6), samples(tmp_path/'qual','qual',20)
    config = candidate(tmp_path)
    study = freeze_judge_study(tmp_path/'study',calibration=cal,qualification=qual,candidate=config,mode='synthetic_test')
    report = fixture_qualification_report(study,{row.output_id:dict(row.fixture_reference.conclusions) for row in qual})
    renamed = tmp_path/'renamed-prompt.txt'
    renamed.write_bytes(config.prompt_path.read_bytes())
    same = config.model_copy(update={'prompt_path':renamed,'languages':('zh','en')})
    assert qualification_applicability(report,same,output_type='summary',language='zh',domain='synthetic')['scope_valid']
    assert not qualification_applicability(report,same,output_type='summary',language='en',domain='synthetic')['scope_valid']
    renamed.write_text('Substantively changed prompt',encoding='utf-8')
    invalid = qualification_applicability(report,same,output_type='summary',language='zh',domain='synthetic')
    assert invalid['reason']=='candidate_refreeze_required' and not invalid['scope_valid']


@pytest.mark.parametrize('mode', ['passed','oversized','malformed','missing_usage','no_authorization','source_drift','timeout'])
def test_native_judge_controls_keep_complete_input_and_zero_send_boundaries(prepared_root, acceptance_public_root, request, mode):
    import asyncio
    from base64 import b64encode
    from dataclasses import replace
    from hashlib import sha256
    import json
    import shutil
    from uuid import uuid4
    from tests.support.agent_acceptance import AcceptancePlan, CaseRegistry, FrozenParentBudget, LiveAuthorization, LiveGate, Reply, SharedControlError, prepare_live, run_plan
    from tests.support.agent_acceptance.judge_qualification import judge_executions, register_judge_study
    source = Path(__file__).resolve().parents[2]
    local = source / '.scratch/retro-agent-live-acceptance-20261116/implementation/review-fixes-01/check-tmp' / Path(os.environ['MYRK_CHECK_RUN_DIRECTORY']).name / uuid4().hex
    local.mkdir(parents=True, exist_ok=False)
    cal, qual = samples(local/'cal','cal',6), samples(local/'qual','qual',20)
    gold = local/'HUMAN_FAIL_REFERENCE_SECRET.json'
    gold.write_text('HUMAN_REFERENCE_LABEL_AND_REASON_SENTINEL',encoding='utf-8')
    cal[0] = cal[0].model_copy(update={'output_id':'HUMAN_FAIL_SECRET_INTERNAL_ID',
        'original_paths':(*cal[0].original_paths,gold),'review_only_paths':(gold,)})
    if mode=='oversized':
        cal[0].input_path.write_text('Complete lawful original: '+('long evidence '*4000),encoding='utf-8')
    study = freeze_judge_study(local/'study',calibration=cal,qualification=qual,candidate=candidate(local),mode='synthetic_test')
    rows = judge_executions(study)[:1]
    reply = Reply(text='not a decision' if mode=='malformed' else '{"conclusions":{"sufficiency":"passed"},"reasons":{"sufficiency":"Synthetic response"}}',usage=mode!='missing_usage',timeout=mode=='timeout')
    registry = CaseRegistry()
    register_judge_study(registry,study,fixed_replies={cal[0].output_id:reply})
    parent = FrozenParentBudget(parent_id='synthetic-control-'+prepared_root.name,accounting_root=prepared_root,
        request_cap=1,phase_microusd={'screening':0,'quality':0,'judge':122880},reserve_microusd=0,profile='synthetic')
    index = Path('E:/Myrk/.scratch/acceptance-parent-budgets')/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    def cleanup():
        if index.exists():
            assert index.parent==Path('E:/Myrk/.scratch/acceptance-parent-budgets') and not index.is_symlink()
            index.unlink()
        assert local.resolve().is_relative_to((source / '.scratch/retro-agent-live-acceptance-20261116/implementation/review-fixes-01/check-tmp' / Path(os.environ['MYRK_CHECK_RUN_DIRECTORY']).name).resolve())
        if local.exists():
            shutil.rmtree(local)
    request.addfinalizer(cleanup)
    plan = AcceptancePlan(executions=rows,parent_budget=parent)
    bundle = prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
    grant = LiveAuthorization(authorization_id='synthetic-control',binding=bundle.binding,total_limit=1,
        case_limits={rows[0].execution_id:1},case_microusd={rows[0].execution_id:122880},
        total_microusd=122880,transport='mock',parent_budget_id=parent.parent_id)
    gate = LiveGate(prepared_root,registry,None if mode=='no_authorization' else grant)
    if mode=='source_drift':
        (source/rows[0].material_path).write_text('Changed between public calls',encoding='utf-8')
    if mode in {'source_drift','no_authorization'}:
        with pytest.raises((SharedControlError,ValueError)):
            asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
        assert json.loads((prepared_root/'ledger.json').read_bytes())['attempts']==[]
        return
    result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    report = gate.accounting_report()
    assert report['actual_http']==(0 if mode=='oversized' else 1)
    assert result.cases[0].product_status==('inconclusive' if mode in {'oversized','malformed','timeout'} else 'passed')
    if mode=='oversized':
        assert len(result.cases[0].evidence['frozen_material'].read_bytes())>32768
        assert cal[0].input_path.read_text(encoding='utf-8') in result.cases[0].evidence['frozen_material'].read_text(encoding='utf-8')
        return
    partial=qualification_report(study,result_path=result.result_path)
    assert partial['qualification']['planned']==partial['qualification']['unknown']==100
    exchange = json.loads(result.cases[0].evidence['exchange'].read_bytes())
    wire = exchange['exchanges'][0]['request_utf8']
    assert 'HUMAN_FAIL_SECRET_INTERNAL_ID' not in wire and gold.name not in wire
    assert 'HUMAN_REFERENCE_LABEL_AND_REASON_SENTINEL' not in wire
    assert b64encode(gold.read_bytes()).decode() not in wire
    assert 'fixture_reference' not in wire
    native = json.loads(wire)
    payload = json.loads(native['contents'][0]['parts'][0]['text'])
    assert payload['actual_input']==cal[0].input_path.read_text(encoding='utf-8')
    assert payload['output']==cal[0].output_path.read_text(encoding='utf-8')
    assert len(payload['original_evidence'])==2
    if mode in {'missing_usage','timeout'}:
        assert report['occupied_microusd']==122880
    snapshot=result.cases[0].evidence['judge_study']
    shutil.rmtree(local)
    assert qualification_report(snapshot,result_path=result.result_path)['qualification']['unknown']==100




def test_two_rounds_are_frozen_before_the_only_qualification_and_never_exceed_160(tmp_path):
    import json
    cal,qual = samples(tmp_path/'cal','cal',6),samples(tmp_path/'qual','qual',20)
    study = freeze_judge_study(tmp_path/'study',calibration=cal,qualification=qual,candidate=candidate(tmp_path),
        mode='synthetic_test',calibration_rounds=2)
    body=json.loads(study.read_bytes())
    assert len(body['judge_inputs'])==160
    assert [row['purpose'] for row in body['judge_inputs'][:60]]==['calibration']*60
    assert [row['purpose'] for row in body['judge_inputs'][60:]]==['qualification']*100
    assert len({row['execution_id'] for row in body['judge_inputs']})==160
    assert qualification_report(study)['actual_qualification']=='not-started'


def test_human_material_manifest_binds_candidate_output_originals_and_exclusions(tmp_path):
    from tests.support.agent_acceptance.judge_qualification import judge_material_manifest
    rows=samples(tmp_path/'cal','cal',6)
    config=candidate(tmp_path)
    first=rows[0]
    original=judge_material_manifest([first],config)
    renamed=tmp_path/'same-prompt-new-file.txt'
    renamed.write_bytes(config.prompt_path.read_bytes())
    assert judge_material_manifest([first],config.model_copy(update={'prompt_path':renamed}))==original
    body=first.output_path.read_bytes()
    first.output_path.write_text('Changed output, same legitimate input',encoding='utf-8')
    assert judge_material_manifest([first],config)!=original
    first.output_path.write_bytes(body)
    hidden=first.model_copy(update={'review_only_paths':(first.original_paths[0],)})
    assert judge_material_manifest([hidden],config)!=original
    config.criteria_path.write_text('Changed ordinary criterion, same input',encoding='utf-8')
    assert judge_material_manifest([first],config)!=original
