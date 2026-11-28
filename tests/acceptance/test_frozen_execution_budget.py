"""Budgets and execution accounting for frozen acceptance runs."""

import asyncio
from hashlib import sha256
import json
from pathlib import Path

import pytest

from tests.acceptance.test_frozen_execution_control import (
    MATERIALS,
    execution,
    own_synthetic_index,
    prepare_synthetic,
    registry_for_product,
    synthetic_parent_index_cleanup as synthetic_parent_index_cleanup,
)
from tests.support.agent_acceptance import AcceptancePlan, CaseOutcome, Reply, run_plan
from tests.support.agent_acceptance.frozen import FrozenExecution, FrozenParentBudget


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
