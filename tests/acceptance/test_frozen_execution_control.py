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


def test_shared_control_stop_cancels_the_actual_inflight_product(prepared_root,acceptance_public_root):
    import httpx2
    from dataclasses import replace
    from myrk.conversation import Conversation,ConversationRecord,Task
    from tests.support.agent_acceptance import SharedControlError
    registry=registry_for_product()
    deadline: asyncio.Timeout
    receiver_calls = 0

    async def product(context):
        async def receiver(request):
            nonlocal receiver_calls
            receiver_calls += 1
            assert receiver_calls == 1, 'Expected a single request in the mock transport'
            now = asyncio.get_running_loop().time()
            startup_deadline = deadline.when()
            assert startup_deadline is not None and now < startup_deadline, 'Mock receiver entered after the startup deadline'
            deadline.reschedule(now + 3)
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
        nonlocal deadline
        async with asyncio.timeout(10) as deadline:
            with pytest.raises(SharedControlError, match='shared control failure'):
                await run_plan(plan, root=acceptance_public_root, registry=registry, live_gate=gate)
            assert receiver_calls == 1
            stop_deadline = deadline.when()
            assert stop_deadline is not None and asyncio.get_running_loop().time() < stop_deadline, 'Shared stop exceeded its deadline'
    asyncio.run(bounded())
    report=gate.accounting_report()
    assert report['actual_http']==1 and report['occupied_microusd']==14746 and report['attempts'][0]['state']=='unknown'
    saved=ConversationRecord.model_validate_json(next(acceptance_public_root.rglob('conversation.json')).read_bytes())
    assert saved.turns[-1].status=='cancelled'
