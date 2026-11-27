"""Frozen execution plans use the public runner and real product effects."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path

import pytest

from tests.support.agent_acceptance import AcceptanceCase, AcceptancePlan, CaseOutcome, CaseRegistry, Reply, run_plan
from tests.support.agent_acceptance.frozen import FrozenExecution, FrozenParentBudget

MATERIALS = Path(__file__).parent / 'materials/frozen-execution'


OWNED_PARENT_INDICES: dict[Path, Path] = {}


@pytest.fixture(autouse=True)
def synthetic_parent_index_cleanup(prepared_root):
    yield
    for path, root in list(OWNED_PARENT_INDICES.items()):
        if path.exists():
            expected = Path('E:/Myrk/.scratch/acceptance-parent-budgets').resolve()
            assert path.resolve().parent == expected and not path.is_symlink() and not path.is_junction()
            path.unlink()
        OWNED_PARENT_INDICES.pop(path)


def own_synthetic_index(parent):
    from myrk.project_paths import project_root
    project=project_root(Path(__file__).resolve().parents[2])
    path=project/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    OWNED_PARENT_INDICES[path]=parent.accounting_root


def execution(identity, *, material='baseline', repeat=1, protocol='deepseek', **values):
    path = MATERIALS / (material + '.txt')
    return FrozenExecution(execution_id=identity, case_id=values.pop('case_id', 'frozen.product'), protocol=protocol,
        scenario_id=values.pop('scenario_id', 'synthetic-product'), material_family_id='multiplication', material_variant=material,
        material_path=path, material_sha256=sha256(path.read_bytes()).hexdigest(), startpoint=identity,
        order=repeat, request_cap=1, cost_cap_microusd=20071,
        equivalence_path=MATERIALS/'criteria.json' if values.get('risk_branch') else None,
        equivalence_sha256=sha256((MATERIALS/'criteria.json').read_bytes()).hexdigest() if values.get('risk_branch') else None, **values)


def registry_for_product(*, fail_baseline=False, requests=1, consume_material=True, wrong_execution_ids=()):
    from myrk.conversation import Conversation, ConversationRecord, Task

    async def product(context):
        reply_text='41' if fail_baseline and context.protocol=='deepseek' or context.execution_id in wrong_execution_ids else '42'
        session = context.session([Reply(text=reply_text) for _ in range(requests)])
        conversation = Conversation(context.root / 'conversation.json')
        task = Task(goal=context.material_input if consume_material else "unbound label only", scope=None, provider_config=session.config)
        async with session.model() as model:
            for _ in range(requests):
                turn = await conversation.ask(task.goal, document=None, task=task, model=model, cancellation_token=context.cancellation_token)
        artifact = context.root / 'answer.txt'
        artifact.write_text(turn.answer, encoding='utf-8')
        exchange = session.save(context.root / 'exchange.json', attempts=[a.model_dump(mode='json') for saved in ConversationRecord.model_validate_json(conversation.path.read_bytes()).turns for a in saved.request_attempts],
            run_id=context.run_id, case_id=context.case_id, task_id=task.id)
        return CaseOutcome('passed' if turn.status == 'completed' and turn.answer.strip() == '42' else 'failed',
            {'actual_saved_answer': artifact.read_text(encoding='utf-8')},
            {'conversation': conversation.path, 'exchange': exchange, 'answer': artifact})

    registry = CaseRegistry()
    registry.register(AcceptanceCase('frozen.product', 'request-input', ('deepseek', 'google'), product,
        bindings=(MATERIALS/'baseline.txt', MATERIALS/'variant.txt',MATERIALS/'criteria.json'), frozen_material_input=True))
    return registry


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


def test_one_parent_budget_covers_services_roots_and_persistent_execution_identity(prepared_root, acceptance_public_root):
    from tests.support.agent_acceptance import prepare_live, LiveGate, LiveAuthorization
    rows = [execution('ds:only'), execution('gem:only', repeat=2, protocol='google')]
    parent = FrozenParentBudget(parent_id='synthetic-'+prepared_root.name, accounting_root=prepared_root, request_cap=2,
        phase_microusd={'screening': 34817, 'quality': 0, 'judge': 0}, reserve_microusd=1000, profile='synthetic')
    own_synthetic_index(parent)
    plan = AcceptancePlan(executions=rows, parent_budget=parent)
    registry = registry_for_product(fail_baseline=True)
    bundle = prepare_live(plan.model_copy(update={'mode':'prepare-live'}), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id='synthetic-explicit-parent', binding=bundle.binding, total_limit=2,
        case_limits={'ds:only':1, 'gem:only':1}, total_microusd=35817,
        case_microusd={'ds:only':20071, 'gem:only':20071}, transport='mock', parent_budget_id=parent.parent_id)
    gate = LiveGate(prepared_root, registry, grant)
    first = asyncio.run(run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate))
    assert [item.product_status for item in first.cases] == ['failed','passed']
    assert first.cases[0].facts['actual_saved_answer']=='41'
    assert first.cases[1].facts['actual_saved_answer']=='42'
    assert [item.execution_id for item in first.cases] == ['ds:only','gem:only']
    report = gate.accounting_report()
    assert report['actual_http'] == report['reserved_requests'] == 2
    assert report['occupied_microusd'] == 34817
    assert {row['execution_id'] for row in report['attempts']} == {'ds:only','gem:only'}
    reopened = asyncio.run(run_plan(plan, root=acceptance_public_root/'another-root', registry=registry,
        live_gate=LiveGate(prepared_root, registry, grant)))
    assert [item.product_status for item in reopened.cases] == ['failed','passed']
    assert gate.accounting_report()['actual_http'] == 2
    with pytest.raises(Exception, match='parent|accounting'):
        prepare_live(plan.model_copy(update={'mode':'prepare-live'}), root=prepared_root/'new-parent', registry=registry)


def prepare_synthetic(rows, prepared_root, *, phases=None, reserve=0, total_requests=None, registry=None, inflight_limit=4):
    from tests.support.agent_acceptance import prepare_live, LiveGate, LiveAuthorization
    registry = registry or registry_for_product()
    phases = phases or {'screening':sum(row.cost_cap_microusd for row in rows),'quality':0,'judge':0}
    parent = FrozenParentBudget(parent_id='synthetic-'+prepared_root.name, accounting_root=prepared_root,
        request_cap=total_requests or sum(row.request_cap for row in rows), phase_microusd=phases,
        reserve_microusd=reserve, inflight_limit=inflight_limit, profile='synthetic')
    own_synthetic_index(parent)
    plan = AcceptancePlan(executions=rows, parent_budget=parent)
    bundle = prepare_live(plan.model_copy(update={'mode':'prepare-live'}), root=prepared_root, registry=registry)
    grant = LiveAuthorization(authorization_id='synthetic-parent-grant', binding=bundle.binding,
        total_limit=parent.request_cap, case_limits={row.execution_id:row.request_cap for row in rows},
        total_microusd=parent.total_microusd, case_microusd={row.execution_id:row.cost_cap_microusd for row in rows},
        transport='mock', parent_budget_id=parent.parent_id)
    return plan, registry, LiveGate(prepared_root,registry,grant)


def test_reserve_transfer_is_before_send_conserves_money_and_preserves_basis(prepared_root, acceptance_public_root):
    row = execution('one').model_copy(update={'cost_cap_microusd':14746})
    plan, registry, gate = prepare_synthetic([row], prepared_root,
        phases={'screening':10000,'quality':0,'judge':0}, reserve=4746)
    basis = prepared_root/'verified-rate-bound.txt'
    basis.write_text('fixed_fixture verified difference for the existing sample; no new samples',encoding='utf-8')
    gate.transfer_reserve(stage='screening', delta_microusd=4746, basis=basis)
    result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    report = gate.accounting_report()
    assert result.cases[0].product_status == 'passed'
    assert report['phase_microusd'] == {'screening':14746,'quality':0,'judge':0}
    assert report['reserve_microusd'] == 0 and report['actual_http'] == 1
    assert report['reserve_transfers'][0]['basis_sha256'] == sha256(basis.read_bytes()).hexdigest()
    assert sum(report['phase_microusd'].values()) + report['reserve_microusd'] == 14746
    with pytest.raises(Exception,match='reserve'):
        gate.transfer_reserve(stage='screening',delta_microusd=1,basis=basis)


def test_optional_work_cannot_spend_an_unexecuted_required_envelope(prepared_root, acceptance_public_root):
    rows = [execution('optional').model_copy(update={'required':False,'request_cap':2,'cost_cap_microusd':29492}),
        execution('required',repeat=2,protocol='google')]
    plan, registry, gate = prepare_synthetic(rows,prepared_root,total_requests=3,
        phases={'screening':34817,'quality':0,'judge':0}, registry=registry_for_product(requests=2))
    result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    report = gate.accounting_report()
    assert report['actual_http'] == 2
    assert report['attempts'][0]['execution_id'] == 'optional'
    assert report['attempts'][1]['execution_id'] == 'required'
    assert 'envelope' in report['case_stops']['optional']['reason']
    assert len(result.cases) == 2 and json.loads(result.result_path.read_bytes())['planned_denominator'] == 2


def test_labels_cannot_replace_actual_frozen_input(prepared_root,acceptance_public_root):
    plan,registry,gate=prepare_synthetic([execution('material-bound')],prepared_root,
        registry=registry_for_product(consume_material=False))
    with pytest.raises(Exception,match='material'):
        asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert gate.accounting_report()['actual_http'] == 0



def test_scoped_stop_blocks_new_work_but_keeps_independent_service_and_denominator(prepared_root, acceptance_public_root):
    from dataclasses import replace
    registry = registry_for_product()
    product = registry.cases['frozen.product'].run
    async def stop_after_product(context):
        outcome = await product(context)
        original = context.root/'observed-effect.txt'
        original.write_text('fixed_fixture observed effect for the local stop control test',encoding='utf-8')
        context.live_gate.request_stop(scope='protocol',reason='synthetic local incident',source=original,
            case_id=context.case_id,protocol=context.protocol)
        return outcome
    registry.cases['frozen.product'] = replace(registry.cases['frozen.product'],run=stop_after_product)
    rows = [execution('ds:first',scenario_id='first'),execution('ds:stopped',repeat=2,scenario_id='second'),
        execution('gem:independent',repeat=3,protocol='google')]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert [item.product_status for item in result.cases] == ['passed','not_executed','passed']
    assert gate.accounting_report()['actual_http']==2
    assert result.cases[1].facts['stop_scope']=='protocol'
    assert result.cases[1].expectation_status=='not_executed'
    assert json.loads(result.result_path.read_bytes())['planned_denominator']==3
    assert len(gate.accounting_report()['scope_stops'])==2
    assert json.loads(result.result_path.read_bytes())['coverage'][0]['execution']=='inconclusive'



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




def test_inflight_stop_cancels_product_and_preserves_unknown_fee_and_partial_effect(prepared_root,acceptance_public_root):
    import httpx2
    from dataclasses import replace
    from myrk.conversation import Conversation,Task
    registry=registry_for_product()
    normal=registry.cases['frozen.product'].run
    async def stopped(context):
        if context.execution_id != 'inflight':
            return await normal(context)
        entered=asyncio.Event()
        partial=context.root/'receiver-partial.txt'
        async def receiver(request):
            partial.write_text('receiver accepted a partial effect before cancellation',encoding='utf-8')
            entered.set()
            await asyncio.Event().wait()
        session=context.session([Reply(text='never delivered')],transport=httpx2.MockTransport(receiver))
        conversation=Conversation(context.root/'conversation.json')
        task=Task(goal=context.material_input,scope=None,provider_config=session.config)
        async with session.model() as model:
            ask=asyncio.create_task(conversation.ask(task.goal,document=None,task=task,model=model,
                cancellation_token=context.cancellation_token))
            await asyncio.wait_for(entered.wait(),10)
            source=context.root/'partial-effect-observation.json'
            source.write_text(json.dumps({'actual_partial_text':partial.read_text(encoding='utf-8'),
                'provenance':'fixed_fixture receiver effect; not a real safety incident'}),encoding='utf-8')
            context.live_gate.request_stop(scope='execution',reason='fixed_fixture inflight stop',source=source,
                case_id=context.case_id,protocol=context.protocol)
            turn=await ask
        exchange=session.save(context.root/'exchange.json',attempts=[a.model_dump(mode='json') for a in turn.request_attempts],
            run_id=context.run_id,case_id=context.case_id,task_id=task.id)
        return CaseOutcome('inconclusive',{'turn_status':turn.status,'partial_effect':partial.read_text(encoding='utf-8')},
            {'conversation':conversation.path,'exchange':exchange,'partial_effect':partial,'stop_source':source})
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=stopped)
    rows=[execution('inflight',scenario_id='inflight'),execution('independent',repeat=2,scenario_id='independent')]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    report=gate.accounting_report()
    assert result.cases[0].facts['turn_status']=='cancelled'
    assert result.cases[0].facts['partial_effect']=='receiver accepted a partial effect before cancellation'
    assert result.cases[1].product_status=='passed'
    assert report['actual_http']==2 and report['occupied_microusd']==29492
    assert report['attempts'][0]['state']=='unknown'
    assert report['attempts'][0]['supplier_final_microusd'] is None
    assert report['scope_stops'][0]['inflight_requests']==[1]
    assert report['scope_stops'][0]['partial_effects_check_required'] is True
    assert json.loads(result.result_path.read_bytes())['coverage'][0]['offline']=='inconclusive'
    reopened=asyncio.run(run_plan(plan,root=acceptance_public_root/'reopen',registry=registry,live_gate=gate))
    assert reopened.cases[0].facts['turn_status']=='cancelled' and gate.accounting_report()['actual_http']==2



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


def test_shared_stop_keeps_all_ids_and_old_root_cannot_restart(prepared_root,acceptance_public_root):
    from dataclasses import replace
    from tests.support.agent_acceptance import SharedControlError
    registry=registry_for_product()
    normal=registry.cases['frozen.product'].run
    async def incident(context):
        outcome=await normal(context)
        source=context.root/'control-observation.txt'
        source.write_text('fixed_fixture shared control failure observation',encoding='utf-8')
        context.live_gate.request_stop(scope='shared',reason='synthetic shared control failed',source=source,
            case_id=context.case_id,protocol=context.protocol)
        return outcome
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=incident)
    rows=[execution('before-stop'),execution('stopped-gem',repeat=2,protocol='google')]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry)
    with pytest.raises(SharedControlError,match='shared control'):
        asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    saved=json.loads((acceptance_public_root/'result.json').read_bytes())
    assert [row['execution_id'] for row in saved['cases']]==['before-stop','stopped-gem']
    assert saved['cases'][1]['product_status']=='not_executed' and saved['planned_denominator']==2
    with pytest.raises(SharedControlError,match='stopped'):
        asyncio.run(run_plan(plan,root=acceptance_public_root/'reopen',registry=registry,live_gate=gate))
    assert gate.accounting_report()['actual_http']==1


def test_expected_block_is_recorded_without_stopping_independent_samples(prepared_root,acceptance_public_root):
    from dataclasses import replace
    registry=registry_for_product()
    normal=registry.cases['frozen.product'].run
    async def expected(context):
        outcome=await normal(context)
        source=context.root/'expected-block.txt'
        source.write_text('fixed_fixture correct expected permission block; no actual incident',encoding='utf-8')
        context.live_gate.request_stop(scope='shared',reason='expected negative',source=source,
            case_id=context.case_id,protocol=context.protocol,expected_block=True)
        return outcome
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=expected)
    rows=[execution('expected',scenario_id='first'),execution('independent',repeat=2,scenario_id='second')]
    plan,registry,gate=prepare_synthetic(rows,prepared_root,registry=registry)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert [row.product_status for row in result.cases]==['passed','passed']
    report=gate.accounting_report()
    assert report['actual_http']==2 and report['stopped'] is False and report['case_stops']=={}
    assert all(row['expected_block'] for row in report['scope_stops'])



def test_inflight_limit_is_checked_at_the_existing_terminal(prepared_root,acceptance_public_root):
    import httpx2
    from dataclasses import replace
    from myrk.conversation import Conversation,Task
    from tests.support.agent_acceptance.replay import OfflineSession
    registry=registry_for_product()
    async def concurrent(context):
        entered,release=asyncio.Event(),asyncio.Event()
        fixture=OfflineSession(context.protocol,[Reply(text='42')])
        async def receiver(request):
            entered.set()
            await release.wait()
            return fixture.respond(request)
        first=context.session([Reply(text='42')],transport=httpx2.MockTransport(receiver))
        second=context.session([Reply(text='42')])
        one,two=Conversation(context.root/'conversation.json'),Conversation(context.root/'second.json')
        task_one=Task(goal=context.material_input,scope=None,provider_config=first.config)
        task_two=Task(goal=context.material_input,scope=None,provider_config=second.config)
        async with first.model() as model_one,second.model() as model_two:
            ask=asyncio.create_task(one.ask(task_one.goal,document=None,task=task_one,model=model_one,
                cancellation_token=context.cancellation_token))
            await asyncio.wait_for(entered.wait(),10)
            denied=await two.ask(task_two.goal,document=None,task=task_two,model=model_two,
                cancellation_token=context.cancellation_token)
            release.set()
            done=await ask
        exchange_one=first.save(context.root/'first-exchange.json',attempts=[x.model_dump(mode='json') for x in done.request_attempts],
            run_id=context.run_id,case_id=context.case_id,task_id=task_one.id)
        exchange_two=second.save(context.root/'second-exchange.json',attempts=[x.model_dump(mode='json') for x in denied.request_attempts],
            run_id=context.run_id,case_id=context.case_id,task_id=task_two.id)
        return CaseOutcome('failed',{'statuses':[done.status,denied.status]},
            {'conversation':one.path,'second_conversation':two.path,'first_exchange':exchange_one,'second_exchange':exchange_two})
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=concurrent)
    row=execution('concurrent').model_copy(update={'request_cap':2,'cost_cap_microusd':29492})
    plan,registry,gate=prepare_synthetic([row],prepared_root,registry=registry,inflight_limit=1)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert result.cases[0].facts['statuses']==['completed','failed']
    report=gate.accounting_report()
    assert report['actual_http']==report['reserved_requests']==1
    assert report['case_stops']['concurrent']['reason']=='inflight request limit exhausted'
    with pytest.raises(ValueError):
        FrozenParentBudget.model_validate({**plan.parent_budget.model_dump(),'inflight_limit':5})



def test_same_parent_id_cannot_reissue_budget_at_another_accounting_root(prepared_root,acceptance_public_root):
    import shutil
    from uuid import uuid4
    from tests.support.agent_acceptance import prepare_live,LiveGate,SharedControlError
    plan,registry,gate=prepare_synthetic([execution('unique-execution')],prepared_root)
    asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    second=prepared_root.with_name(uuid4().hex)
    try:
        rerooted=plan.model_copy(update={'parent_budget':plan.parent_budget.model_copy(update={'accounting_root':second})})
        bundle=prepare_live(rerooted.model_copy(update={'mode':'prepare-live'}),root=second,registry=registry)
        reissued=gate.authorization.model_copy(update={'binding':bundle.binding})
        with pytest.raises(SharedControlError,match='parent|Parent'):
            asyncio.run(run_plan(rerooted,root=acceptance_public_root/'wrong-root',registry=registry,
                live_gate=LiveGate(second,registry,reissued)))
        assert gate.accounting_report()['actual_http']==1
        assert LiveGate(second,registry,reissued).accounting_report()['actual_http']==0
    finally:
        if second.exists():
            assert second.resolve().parent == prepared_root.resolve().parent
            shutil.rmtree(second)



def test_run_cannot_replace_the_frozen_execution_cap_even_with_the_original_grant(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance import SharedControlError
    plan,registry,gate=prepare_synthetic([execution('frozen-once')],prepared_root)
    changed=plan.model_copy(update={'executions':(plan.executions[0].model_copy(update={'request_cap':2}),)})
    with pytest.raises(SharedControlError,match='frozen plan|Frozen plan'):
        asyncio.run(run_plan(changed,root=acceptance_public_root,registry=registry,live_gate=gate))
    assert gate.accounting_report()['actual_http']==0
    assert not acceptance_public_root.exists()



def test_shared_execution_is_registered_beforehand_and_generates_and_spends_once(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance.frozen import SharedCoverage
    original=execution('shared-generation')
    criteria=MATERIALS/'criteria.json'
    shared=SharedCoverage(scenario_id='synthetic-quality-sample',material_family_id=original.material_family_id,
        material_sha256=original.material_sha256,request_cap=24,criteria_path=criteria,
        criteria_sha256=sha256(criteria.read_bytes()).hexdigest(),never_tuned=True,
        applicability_reason='fixed_fixture identical source and input satisfies both frozen contracts; one generation')
    row=FrozenExecution.model_validate({**original.model_dump(),'shared_with':(shared,)})
    plan,registry,gate=prepare_synthetic([row],prepared_root)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    saved=json.loads(result.result_path.read_bytes())
    assert len(result.cases)==1 and gate.accounting_report()['actual_http']==1
    assert saved['coverage_denominators']=={'screening':1,'quality':1,'judge':0,'product_executions':1}
    assert saved['planned_executions'][0]['shared_with'][0]['scenario_id']=='synthetic-quality-sample'
    assert result.cases[0].evidence['shared_criteria'].read_bytes()==criteria.read_bytes()
    with pytest.raises(ValueError,match='shared|Shared'):
        AcceptancePlan(executions=[row.model_copy(update={'request_cap':30})])



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



@pytest.mark.parametrize('stop_before_tool',[False,True])
def test_real_executor_file_effect_and_no_new_action_after_accepted_stop(prepared_root,acceptance_public_root,stop_before_tool):
    import httpx2
    from dataclasses import replace
    from myrk.conversation import Conversation,Task,TaskScope
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance.actions import record_outcome
    from tests.support.agent_acceptance.replay import OfflineSession
    registry=registry_for_product()
    async def file_effect(context):
        folder=context.root/'user-work';folder.mkdir()
        target=folder/'answer.txt'
        replies=[Reply(tool='create_text',arguments={'path':'answer.txt','content':'42\n'}),Reply(text='Saved the result.')]
        fixture=OfflineSession(context.protocol,replies)
        source=context.root/'stop-observation.json'
        async def receiver(request):
            if stop_before_tool:
                source.write_text(json.dumps({'target_existed_before_stop':target.exists(),'provenance':'fixed_fixture stop before product file action'}),encoding='utf-8')
                context.live_gate.request_stop(scope='execution',reason='fixed_fixture stop before new action',source=source,
                    case_id=context.case_id,protocol=context.protocol)
            return fixture.respond(request)
        session=context.session(replies,transport=httpx2.MockTransport(receiver))
        conversation=Conversation(context.root/'conversation.json')
        scope=TaskScope(root=str(folder),allowed_actions=['edit'],provider='deepseek',recipients=['deepseek'])
        task=Task(goal=context.material_input,scope=scope,provider_config=session.config,approval=ApprovalSettings(mode='full_access'))
        async with session.model() as model:
            turn=await conversation.ask(task.goal,document=None,scope=scope,task=task,model=model,cancellation_token=context.cancellation_token)
        evidence={'actual_product_file':target} if target.is_file() else {}
        if source.is_file():evidence['stop_source']=source
        return record_outcome(context,session,conversation,task,turn,
            turn.status=='completed' and target.is_file() and target.read_bytes()==b'42\n',
            facts={'actual_effect_bytes':target.read_bytes().hex() if target.is_file() else None},evidence=evidence)
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=file_effect)
    row=execution('real-file').model_copy(update={'request_cap':2,'cost_cap_microusd':29492})
    plan,registry,gate=prepare_synthetic([row],prepared_root,registry=registry)
    result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    case=result.cases[0];report=gate.accounting_report()
    if stop_before_tool:
        assert case.facts['turn_status']=='cancelled' and case.facts['actual_effect_bytes'] is None
        assert not any(action['status']=='completed' for action in case.facts['actions'])
        assert report['actual_http']==1 and report['occupied_microusd']==14746
    else:
        assert case.product_status=='passed' and case.evidence['actual_product_file'].read_bytes()==b'42\n'
        assert case.facts['actions'][0]['status']=='completed' and case.facts['actions'][0]['kind']=='create'
        assert report['actual_http']==2


@pytest.mark.parametrize('damage',['missing','broken-json','different-root'])
def test_activated_parent_binding_missing_or_changed_fails_closed(prepared_root,acceptance_public_root,damage):
    from tests.support.agent_acceptance import SharedControlError
    plan,registry,gate=prepare_synthetic([execution('durable-parent')],prepared_root)
    asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate))
    path=Path(gate.accounting_report()['parent_binding_path'])
    (acceptance_public_root/'original-parent-activation.json').write_bytes(path.read_bytes())
    if damage=='missing':path.unlink()
    elif damage=='broken-json':path.write_bytes(b'{broken')
    else:
        saved=json.loads(path.read_bytes());saved['accounting_root']=str(prepared_root/'another-root')
        path.write_text(json.dumps(saved),encoding='utf-8')
    with pytest.raises(SharedControlError,match='Parent|parent'):
        asyncio.run(run_plan(plan,root=acceptance_public_root/'reopen',registry=registry,live_gate=gate))
    assert gate.accounting_report()['actual_http']==1


def test_preparation_and_validation_do_not_activate_or_claim_a_parent(prepared_root):
    plan,registry,gate=prepare_synthetic([execution('no-send')],prepared_root)
    path=Path(gate.accounting_report()['parent_binding_path'])
    assert not path.exists()
    gate.for_execution('no-send').validate(case_id='frozen.product',protocol='deepseek')
    assert not path.exists() and gate.accounting_report()['reserved_requests']==0



def test_shared_control_stop_cancels_the_actual_inflight_product(prepared_root,acceptance_public_root):
    import httpx2
    from dataclasses import replace
    from myrk.conversation import Conversation,ConversationRecord,Task
    from tests.support.agent_acceptance import SharedControlError
    registry=registry_for_product()
    async def product(context):
        async def receiver(request):
            context.live_gate.stop(reason='synthetic shared control failure during HTTP')
            await asyncio.Event().wait()
        session=context.session([Reply(text='never delivered')],transport=httpx2.MockTransport(receiver))
        conversation=Conversation(context.root/'conversation.json')
        task=Task(goal=context.material_input,scope=None,provider_config=session.config)
        async with session.model() as model:
            turn=await conversation.ask(task.goal,document=None,task=task,model=model,cancellation_token=context.cancellation_token)
        exchange=session.save(context.root/'exchange.json',attempts=[x.model_dump(mode='json') for x in turn.request_attempts],
            run_id=context.run_id,case_id=context.case_id,task_id=task.id)
        return CaseOutcome('inconclusive',{'turn_status':turn.status},{'conversation':conversation.path,'exchange':exchange})
    registry.cases['frozen.product']=replace(registry.cases['frozen.product'],run=product)
    plan,registry,gate=prepare_synthetic([execution('inflight-shared-control')],prepared_root,registry=registry)
    async def bounded():
        return await asyncio.wait_for(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=gate),3)
    with pytest.raises(SharedControlError,match='shared control failure'):
        asyncio.run(bounded())
    report=gate.accounting_report()
    assert report['actual_http']==1 and report['occupied_microusd']==14746 and report['attempts'][0]['state']=='unknown'
    saved=ConversationRecord.model_validate_json(next(acceptance_public_root.rglob('conversation.json')).read_bytes())
    assert saved.turns[-1].status=='cancelled'



def test_accepted_stop_between_reservation_and_terminal_start_cannot_start_http(prepared_root):
    from tests.support.agent_acceptance.preparation import CaseBudgetExceeded
    plan,registry,gate=prepare_synthetic([execution('reserved-before-stop')],prepared_root)
    view=gate.for_execution('reserved-before-stop')
    number=view.reserve(case_id='frozen.product',protocol='deepseek',actor='main',purpose='work')
    source=prepared_root/'stop-before-terminal.txt'
    source.write_text('fixed_fixture accepted stop while reservation has no terminal HTTP',encoding='utf-8')
    view.request_stop(scope='execution',reason='fixed_fixture stop before terminal start',source=source,
        case_id='frozen.product',protocol='deepseek')
    with pytest.raises(CaseBudgetExceeded,match='stop'):
        view.mark_http_started(number)
    assert gate.accounting_report()['reserved_requests']==1 and gate.accounting_report()['actual_http']==0



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


def test_rollup_cannot_replace_an_earlier_failed_execution_with_later_successes(acceptance_public_root):
    rows=[execution('first-failed',repeat=1,risk_branch='same'),execution('later-pass',repeat=2,risk_branch='same'),
        execution('variant-pass',repeat=3,material='variant',risk_branch='same')]
    result=asyncio.run(run_plan(AcceptancePlan(executions=rows),root=acceptance_public_root,
        registry=registry_for_product(wrong_execution_ids=('first-failed',))))
    saved=json.loads(result.result_path.read_bytes())
    assert [row.product_status for row in result.cases]==['failed','passed','passed']
    assert saved['coverage'][0]['offline']==saved['coverage'][0]['execution']=='failed'
    assert [row['status'] for row in saved['coverage'][0]['executions']]==['failed','passed','passed']
    assert saved['planned_denominator']==3



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
