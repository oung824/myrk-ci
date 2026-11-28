"""Frozen authorization bindings, dependencies and acceptance evidence."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.acceptance.test_frozen_execution_control import (
    MATERIALS,
    execution,
    prepare_synthetic,
    registry_for_product,
    synthetic_parent_index_cleanup as synthetic_parent_index_cleanup,
)
from tests.support.agent_acceptance import AcceptanceCase, AcceptancePlan, CaseOutcome, Reply, run_plan
from tests.support.agent_acceptance.frozen import FrozenExecution, FrozenParentBudget


def test_explicit_repetitions_keep_every_result_and_consume_actual_variant(acceptance_public_root):
    rows = [execution('risk:one', repeat=1, risk_branch='same-risk'),
        execution('risk:two', repeat=2, risk_branch='same-risk'),
        execution('risk:three', material='variant', repeat=3, risk_branch='same-risk')]
    result = asyncio.run(run_plan(AcceptancePlan(executions=rows), root=acceptance_public_root, registry=registry_for_product()))
    saved = json.loads(result.result_path.read_bytes())
    assert [item.execution_id for item in result.cases] == ['risk:one', 'risk:two', 'risk:three']
    assert len({str(item.evidence['answer']) for item in result.cases}) == 3
    assert all(item.facts['actual_saved_answer'] == '42' for item in result.cases)
    requests = [json.loads(item.evidence['exchange'].read_bytes())['exchanges'][0]['request_utf8'] for item in result.cases]
    assert all('Compute six times seven' in request for request in requests[:2])
    assert 'Find the product of 6 and 7' in requests[2] and requests[2] != requests[0]
    assert saved['planned_denominator'] == 3
    assert [item['execution_id'] for item in saved['coverage'][0]['executions']] == ['risk:one', 'risk:two', 'risk:three']
    assert all(item.semantic_status == 'not_measured' for item in result.cases)


def test_labels_cannot_replace_actual_frozen_input(prepared_root,acceptance_public_root):
    plan,registry,gate=prepare_synthetic([execution('material-bound')],prepared_root,
        registry=registry_for_product(consume_material=False))
    with pytest.raises(Exception,match='material'):
        asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert gate.accounting_report()['actual_http'] == 0


def test_judge_uses_the_same_parent_and_terminal_with_actual_sdk_identity(prepared_root,acceptance_public_root):
    from pydantic_ai import Agent
    from tests.support.agent_acceptance.replay import OfflineSession
    registry=registry_for_product()
    async def judge(context):
        session=context.judge_session([Reply(text='ordinary dimension: passed')],purpose='qualification')
        async with session.model() as model:
            answer=await Agent(model).run(context.material_input)
        exchange=session.save(context.root/'judge-exchange.json',attempts=[],run_id=context.run_id,
            case_id=context.case_id,task_id='synthetic-judge-call')
        return CaseOutcome('passed',{'judge_output':answer.output},{'exchange':exchange})
    registry.register(AcceptanceCase('frozen.judge','request-input',('google',),judge,
        bindings=(MATERIALS/'baseline.txt',MATERIALS/'variant.txt',MATERIALS/'criteria.json'),frozen_material_input=True))
    rows=[execution('product'),execution('judge',repeat=2,case_id='frozen.judge',protocol='google',
        role='judge',stage='judge',judge_purpose='qualification').model_copy(update={'cost_cap_microusd':122880})]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry,
        phases={'screening':20071,'quality':0,'judge':122880})
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    report=gate.accounting_report()
    assert [item.product_status for item in result.cases]==['passed','passed']
    assert [item['role'] for item in report['attempts']]==['product','judge']
    assert report['actual_http']==2 and report['occupied_microusd']==137626
    saved=json.loads(result.cases[1].evidence['exchange'].read_bytes())
    assert saved['product_attempts']==[]
    identity=saved['sdk_inputs'][0]['request_identity']
    assert identity['actor']=='judge' and identity['purpose']=='qualification'
    assert identity['judge_call_id']==report['attempts'][1]['judge_call_id']
    assert report['attempts'][1]['product_number'] is None
    assert 'gemini-2.5-pro' in saved['exchanges'][0]['url']
    assert report['attempts'][1]['sdk_input_id']==saved['sdk_inputs'][0]['sdk_input_id']
    from tests.support.agent_acceptance import export_review,review_report
    package=export_review(result.result_path,acceptance_public_root/'mixed-review')
    review=review_report(package)
    assert len(review['outputs'])==1 and review['outputs'][0]['execution_id']=='product'
    assert len(review['case_results'])==1 and len(review['judge_call_results'])==1
    assert review['judge_call_results'][0]['execution_id']=='judge'
    assert len(review['original_product_results'])==1


def test_complete_proposal_counts_are_exact_and_missing_materials_cannot_freeze():
    from tests.support.agent_acceptance.frozen import full_plan_proposal
    proposal=full_plan_proposal()
    assert proposal.totals()=={'screening':{'deepseek':{'executions':96,'requests':3154},
        'google':{'executions':32,'requests':1102}},'quality':{'deepseek':{'executions':72,'requests':2160},
        'google':{'executions':12,'requests':360}},'judge':{'google':{'executions':480,'requests':480}},
        'product_executions':212,'requests':7256,'sending_authorized':False,'ready':False}
    assert len({row.execution_id for row in proposal.executions})==692
    with pytest.raises(ValueError,match='material|binding'):
        proposal.freeze({},registry=registry_for_product(),parent_budget=None)


@pytest.mark.parametrize('change',[{'role':'judge','judge_purpose':'qualification','stage':'screening'},
    {'role':'judge','stage':'judge'},{'role':'product','judge_purpose':'scoring'}])
def test_frozen_role_and_stage_cannot_be_changed_by_a_label(change):
    with pytest.raises(ValueError,match='Judge|judge'):
        FrozenExecution.model_validate({**execution('bound').model_dump(),**change})


def test_product_context_cannot_take_a_judge_grant_by_calling_the_judge_session(prepared_root,acceptance_public_root):
    from dataclasses import replace
    registry=registry_for_product()
    async def impersonation(context):
        context.judge_session([Reply(text='fake')],purpose='qualification')
        return CaseOutcome('passed')
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=impersonation)
    plan,registry,gate=prepare_synthetic([execution('product-only')],prepared_root,registry=registry)
    with pytest.raises(Exception,match='Judge|judge'):
        asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert gate.accounting_report()['actual_http']==0


def test_actual_dependency_failure_skips_only_its_bound_execution(prepared_root,acceptance_public_root):
    registry=registry_for_product(fail_baseline=True)
    registry.register(AcceptanceCase('frozen.dependent','request-input',('deepseek',),registry.cases['frozen.product'].run,
        bindings=(MATERIALS/'baseline.txt',MATERIALS/'variant.txt',MATERIALS/'criteria.json'),frozen_material_input=True,dependencies=('frozen.product',)))
    rows=[execution('source',scenario_id='source'),execution('dependent',repeat=2,case_id='frozen.dependent',
        scenario_id='dependent',dependencies=('source',)),execution('gem-independent',repeat=3,protocol='google')]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert [row.product_status for row in result.cases]==['failed','not_executed','passed']
    assert result.cases[1].facts['dependencies']==['source']
    assert gate.accounting_report()['actual_http']==2
    assert json.loads(result.result_path.read_bytes())['planned_denominator']==3
    from tests.support.agent_acceptance import export_review,review_report
    review=review_report(export_review(result.result_path,acceptance_public_root/'dependency-review'))
    assert [row['case_status'] for row in review['case_results']]==['failed','not_executed','pending_review']
    assert review['planned_denominator']==3


@pytest.mark.parametrize('defect',['duplicate-id','missing-material','unknown-adapter','changed-risk-branch','label-variant','wrong-dependency','different-case-branches','wrong-scenario-binding'])
def test_invalid_freeze_is_rejected_before_any_product_execution(prepared_root,defect):
    from dataclasses import replace
    from tests.support.agent_acceptance import prepare_live
    registry=registry_for_product()
    rows=[execution('risk-1',repeat=1,risk_branch='same'),execution('risk-2',repeat=2,risk_branch='same'),
        execution('risk-3',repeat=3,material='variant',risk_branch='same')]
    if defect=='duplicate-id': rows[2]=rows[2].model_copy(update={'execution_id':'risk-1'})
    if defect=='missing-material': rows[2]=rows[2].model_copy(update={'material_path':MATERIALS/'missing.txt'})
    if defect=='unknown-adapter': registry.cases['frozen.product']=replace(registry.cases['frozen.product'],frozen_material_input=False)
    if defect=='changed-risk-branch': rows[2]=rows[2].model_copy(update={'risk_branch':'different'})
    if defect=='label-variant': rows[2]=rows[2].model_copy(update={'material_path':rows[0].material_path,'material_sha256':rows[0].material_sha256})
    if defect=='wrong-dependency': rows[2]=rows[2].model_copy(update={'dependencies':('risk-1',)})
    if defect=='different-case-branches':
        registry.register(AcceptanceCase('frozen.another','request-input',('deepseek',),registry.cases['frozen.product'].run,
            bindings=(MATERIALS/'baseline.txt',MATERIALS/'variant.txt',MATERIALS/'criteria.json'),frozen_material_input=True))
        rows[2]=rows[2].model_copy(update={'case_id':'frozen.another'})
    if defect=='wrong-scenario-binding':
        registry.cases['frozen.product']=replace(registry.cases['frozen.product'],
            screening={'deepseek':{'scenario_id':'different-scenario','request_cap':1}})

    parent=FrozenParentBudget(parent_id='invalid-freeze',accounting_root=prepared_root,request_cap=3,
        phase_microusd={'screening':60213,'quality':0,'judge':0},reserve_microusd=0,profile='synthetic')
    with pytest.raises(ValueError):
        plan=AcceptancePlan(executions=rows,parent_budget=parent,mode='prepare-live')
        prepare_live(plan,root=prepared_root,registry=registry)
    assert not prepared_root.exists()


def test_frozen_material_bindings_work_from_the_saved_source_copy(prepared_root):
    import os,subprocess,sys
    plan,registry,gate=prepare_synthetic([execution('copied-source')],prepared_root)
    script="from pathlib import Path; from tests.acceptance.test_frozen_execution_control import registry_for_product; from tests.support.agent_acceptance import verify_prepared; print(verify_prepared(Path.cwd(),registry=registry_for_product()).binding)"
    copied=subprocess.run([sys.executable,'-B','-c',script],cwd=prepared_root,
        env={**os.environ,'PYTHONPATH':str(prepared_root),'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
    assert copied.returncode==0,copied.stderr
    assert copied.stdout.strip()==gate.authorization.binding


def test_explicit_repetitions_export_all_originals_and_independent_review_rows(acceptance_public_root):
    from tests.support.agent_acceptance import export_review,review_report
    rows=[execution('export:one',repeat=1,risk_branch='same'),execution('export:two',repeat=2,risk_branch='same'),
        execution('export:three',repeat=3,material='variant',risk_branch='same')]
    result=asyncio.run(run_plan(AcceptancePlan(executions=rows),root=acceptance_public_root,registry=registry_for_product()))
    package_path=export_review(result.result_path,acceptance_public_root/'review-package')
    package=json.loads(package_path.read_bytes())
    assert len(package['outputs'])==3 and len(package['original_cases'])==3
    assert {row['execution_id'] for row in package['outputs']}=={'export:one','export:two','export:three'}
    assert len([name for name in package['originals'] if name.endswith('/conversation')])==3
    assert len({row['path'] for row in package['originals'].values()})==len(package['originals'])
    assert [row['execution_id'] for row in review_report(package_path)['case_results']]==['export:one','export:two','export:three']
    assert all(':' not in Path(row['path']).name for row in package['originals'].values())


@pytest.mark.parametrize('source_passed',[True,False])
def test_exact_cross_protocol_dependency_supplies_its_actual_original_to_judge(prepared_root,acceptance_public_root,source_passed):
    from pydantic_ai import Agent
    registry=registry_for_product(fail_baseline=not source_passed)
    async def judge(context):
        original=context.dependency_evidence['source']['answer']
        actual=original.read_text(encoding='utf-8')
        session=context.judge_session([Reply(text='ordinary fixture review')],purpose='qualification')
        async with session.model() as model:
            await Agent(model).run(context.material_input+' Actual source artifact: '+actual)
        exchange=session.save(context.root/'exchange.json',attempts=[],run_id=context.run_id,case_id=context.case_id,task_id='synthetic-judge')
        return CaseOutcome('passed',{'actual_source_bytes':original.read_bytes().hex()},{'exchange':exchange,'dependency_original':original})
    registry.register(AcceptanceCase('frozen.judge','request-input',('google',),judge,
        bindings=(MATERIALS/'baseline.txt',MATERIALS/'variant.txt',MATERIALS/'criteria.json'),
        frozen_material_input=True,dependencies=('frozen.product',)))
    rows=[execution('source'),execution('judge',repeat=2,case_id='frozen.judge',protocol='google',role='judge',
        stage='judge',judge_purpose='qualification',dependencies=('source',)).model_copy(update={'cost_cap_microusd':122880})]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry,phases={'screening':20071,'quality':0,'judge':122880})
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    if source_passed:
        assert result.cases[1].facts['actual_source_bytes']=='3432' and gate.accounting_report()['actual_http']==2
        exchange=json.loads(result.cases[1].evidence['exchange'].read_bytes())
        assert 'Actual source artifact: 42' in exchange['exchanges'][0]['request_utf8']
    else:
        assert result.cases[1].product_status=='not_executed' and result.cases[1].facts['dependencies']==['source']
        assert gate.accounting_report()['actual_http']==1


def test_quality_execution_reuses_adapter_without_claiming_initial_screening(prepared_root,acceptance_public_root):
    from dataclasses import replace
    registry=registry_for_product()
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],
        screening={'deepseek':{'scenario_id':'D01','request_cap':1}})
    row=execution('quality-task',scenario_id='QG1.formal.1',stage='quality')
    plan,registry,gate=prepare_synthetic([row],prepared_root,registry=registry,
        phases={'screening':0,'quality':20071,'judge':0})
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert result.cases[0].product_status=='passed' and result.cases[0].screening=={}
    coverage=json.loads(result.result_path.read_bytes())['coverage'][0]
    assert coverage['executions'][0]['stage']=='quality'
    assert coverage['executions'][0]['scenario_id']=='QG1.formal.1'


def test_offline_formal_domain_keeps_the_original_gate_and_product_identity(acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry
    result=asyncio.run(run_plan(AcceptancePlan(cases=['formal.user-approval']),root=acceptance_public_root,
        registry=builtin_registry()))
    case=result.cases[0]
    assert case.product_status=='passed' and case.facts['normal_profile_accesses']==case.facts['real_http_attempts']==0
    ledger=json.loads(case.evidence['ledger.json'].read_bytes())
    assert ledger['attempts'] and {row['execution_id'] for row in ledger['attempts']}=={case.execution_id}
    review=json.loads(case.evidence['approval_review'].read_bytes())
    assert review['execution']['execution_id']==case.execution_id
    common=json.loads(case.evidence['semantic_review'].read_bytes())
    assert common['execution_id']==case.execution_id
    index=json.loads(case.evidence['evidence_index'].read_bytes())
    assert index['execution_id']==case.execution_id
    records=[json.loads(path.read_bytes()) for label,path in case.evidence.items()
        if label.startswith('records/') and label.count('/')==1 and path.suffix=='.json']
    assert records
    requests=[request for record in records for turn in record['turns'] for request in turn.get('request_attempts',[])]
    assert requests
    assert all(any((row['product_number'],row['product_step_id'],row['actor'],row['purpose'],row['delegation_id'])==
        (request['number'],request.get('step_id'),request['actor'],request['purpose'],request.get('delegation_id'))
        and row['request_sha256']==(request.get('observation') or {}).get('wire',{}).get('body_sha256')
        for request in requests) for row in ledger['attempts'])
