"""Candidate changes require an explicit new freeze on the original parent ledger."""
import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import pytest

from tests.support.agent_acceptance import AcceptancePlan, LiveAuthorization, LiveGate, Reply, prepare_live, run_plan
from tests.support.agent_acceptance.core import AcceptanceCase, CaseContext, CaseOutcome, CaseRegistry, SharedControlError
from tests.support.agent_acceptance.frozen import FrozenExecution, FrozenParentBudget
from myrk.conversation import Conversation, ConversationRecord, Task

SOURCE=Path(__file__).resolve().parents[2]
PROMPT=SOURCE/'tests/acceptance/materials/candidate-refreeze/prompt.txt'
INPUT=SOURCE/'tests/acceptance/materials/frozen-execution/baseline.txt'


def candidate_registry():
    async def product(context):
        session=context.session([Reply(text='42')])
        conversation=Conversation(context.root/'conversation.json')
        async with session.model() as model:
            turn=await conversation.ask(context.material_input,document=None,task=Task(goal=context.material_input,scope=None,provider_config=session.config),model=model)
        exchange=session.save(context.root/'exchange.json',attempts=[a.model_dump(mode='json') for saved in ConversationRecord.model_validate_json(conversation.path.read_bytes()).turns for a in saved.request_attempts],task_id=turn.task.id,run_id=context.run_id,case_id=context.case_id)
        return CaseOutcome('passed' if turn.answer=='42' else 'failed',evidence={'conversation':conversation.path,'exchange':exchange})
    registry=CaseRegistry()
    registry.register(AcceptanceCase('synthetic.candidate','request-input',('deepseek','google'),product,
        bindings=(INPUT,PROMPT),frozen_material_input=True))
    return registry


def synthetic_parent(prepared_root,*,reserve=0):
    registry=candidate_registry()
    rows=tuple(FrozenExecution(execution_id=name,case_id='synthetic.candidate',protocol='deepseek',scenario_id=scenario,
        material_family_id=scenario,material_variant='baseline',material_path=INPUT,material_sha256=sha256(INPUT.read_bytes()).hexdigest(),
        startpoint=name,order=n,stage='quality',request_cap=1,cost_cap_microusd=14746+reserve//2)
        for n,(name,scenario) in enumerate((('tuning','QG1.tuning.1'),('formal','QG1.formal.1')),1))
    parent=FrozenParentBudget(parent_id='synthetic-refreeze-'+prepared_root.name,accounting_root=prepared_root,request_cap=2,
        phase_microusd={'screening':0,'quality':29492,'judge':0},reserve_microusd=reserve,profile='synthetic')
    plan=AcceptancePlan(executions=rows,parent_budget=parent,mode='prepare-live')
    bundle=prepare_live(plan,root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-original',binding=bundle.binding,total_limit=2,case_limits={'tuning':1,'formal':1},
        total_microusd=29492+reserve,case_microusd={'tuning':14746+reserve//2,'formal':14746+reserve//2},transport='mock',parent_budget_id=parent.parent_id)
    return registry,plan,LiveGate(prepared_root,registry,grant)


def execute_one(registry,plan,gate,name,root):
    row=next(row for row in plan.executions if row.execution_id==name)
    root.mkdir(parents=True)
    context=CaseContext(root,row.protocol,'synthetic-candidate',row.case_id,execution=row,live_gate=gate.for_execution(name))
    root.rmdir()
    result=asyncio.run(run_plan(plan.model_copy(update={'mode':'offline'}),root=root,registry=registry,live_gate=gate,execution_ids=(name,)))
    return next(item for item in result.cases if item.execution_id==name)


def approve_candidate(gate,candidate,*,affected=('formal',),grant_changes=None,decision_changes=None,review_sha=None):
    from tests.support.agent_acceptance.preparation import CandidateFreezeReview
    report=gate.accounting_report()
    original=gate.root/'synthetic-candidate-review.json'
    value=CandidateFreezeReview(parent_id=candidate.plan.parent_budget.parent_id,accounting_root=gate.root,
        previous_binding=gate.authorization.binding,candidate_binding=candidate.binding,affected_execution_ids=affected,
        tuning_execution_ids=('tuning',),tuning_results={'tuning':sha256(json.dumps(vars(gate.for_execution('tuning').recorded_outcome()),default=str,sort_keys=True).encode()).hexdigest()} if gate.for_execution('tuning').recorded_outcome() is not None else {},
        untuned_materials={'formal':candidate.plan.executions[1].material_sha256},review_kind='synthetic_control',
        reviewer='synthetic-control-no-human-signature',reason='Explicit synthetic review of exact candidate; no formal output has been seen.')
    if decision_changes:value=value.model_copy(update=decision_changes)
    original.write_text(value.model_dump_json(indent=2),encoding='utf-8')
    grant=gate.authorization.model_copy(update={'authorization_id':'synthetic-new-candidate','binding':candidate.binding,**(grant_changes or {})})
    return gate.refreeze_candidate(candidate,review=original,review_sha256=review_sha or sha256(original.read_bytes()).hexdigest(),grant=grant)


def test_tuning_source_change_requires_explicit_candidate_and_keeps_original_ledger(prepared_root,acceptance_public_root):
    registry,plan,gate=synthetic_parent(prepared_root)
    original_prompt=PROMPT.read_bytes()
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        tuning=execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        genesis=(prepared_root/'prepared.json').read_bytes()
        before=gate.accounting_report()
        PROMPT.write_bytes(original_prompt+b'\nSynthetic changed prompt after tuning; formal remains unseen.\n')
        with pytest.raises(SharedControlError):
            gate.for_execution('formal').validate(case_id='synthetic.candidate',protocol='deepseek')
        assert gate.accounting_report()['actual_http']==1
        candidate=gate.propose_candidate()
        with pytest.raises(SharedControlError):
            gate.for_execution('formal').validate(case_id='synthetic.candidate',protocol='deepseek')
        assert gate.accounting_report()['actual_http']==1
        current=approve_candidate(gate,candidate)
        result=execute_one(registry,plan,current,'formal',acceptance_public_root/'formal')
        report=current.accounting_report()
        assert result.product_status=='passed' and report['actual_http']==2
        assert report['reserved_requests']==2 and report['occupied_microusd']==29492
        assert report['completed_executions']==['tuning','formal']
        assert (prepared_root/'prepared.json').read_bytes()==genesis
        assert report['binding']==before['binding'] and report['attempts'][0]==before['attempts'][0]
        assert current.for_execution('tuning').recorded_outcome().evidence==tuning.evidence
        with pytest.raises(SharedControlError):
            gate.for_execution('formal').validate(case_id='synthetic.candidate',protocol='deepseek')
    finally:
        PROMPT.write_bytes(original_prompt)
        if parent_index.exists():parent_index.unlink()

def test_unreviewed_execution_cannot_be_added_to_active_candidate_map(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance.preparation import digest
    registry,plan,gate=synthetic_parent(prepared_root)
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        current=approve_candidate(gate,gate.propose_candidate())
        path=gate.root/'ledger.json'
        original=json.loads(path.read_bytes())
        original['active_candidates']['tuning']=current.authorization.binding
        original['integrity']=digest({key:value for key,value in original.items() if key!='integrity'})
        path.write_text(json.dumps(original),encoding='utf-8')
        with pytest.raises(SharedControlError,match='candidate|Candidate|review'):
            current.for_execution('tuning').validate(case_id='synthetic.candidate',protocol='deepseek')
        assert current.accounting_report()['actual_http']==1
    finally:
        if parent_index.exists():parent_index.unlink()

def test_unexecuted_tuning_can_close_with_its_reason_without_new_parent(prepared_root,acceptance_public_root):
    registry,plan,gate=synthetic_parent(prepared_root)
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    reason=prepared_root/'tuning-not-started.txt'
    reason.write_text('Synthetic independent tuning was not started; its retained failure reason is not a replacement sample.',encoding='utf-8')
    closed=CaseOutcome('not_executed',{'reason':reason.read_text(encoding='utf-8')},{'reason':reason},case_id='synthetic.candidate',protocol='deepseek',execution_id='tuning')
    gate.for_execution('tuning').finish_execution(closed)
    try:
        candidate=gate.propose_candidate()
        current=approve_candidate(gate,candidate)
        outcome=execute_one(registry,plan,current,'formal',acceptance_public_root/'formal')
        assert outcome.product_status=='passed'
        report=current.accounting_report()
        assert report['actual_http']==report['reserved_requests']==1
        assert report['occupied_microusd']==14746
        assert current.for_execution('tuning').recorded_outcome().product_status=='not_executed'
        assert report['binding']==gate.authorization.binding
    finally:
        if parent_index.exists():parent_index.unlink()


@pytest.mark.parametrize("obstruction", ["unfinished_tuning", "inflight", "formal_seen", "execution_stopped", "shared_stopped", "amount", "review_sha", "identity"])
def test_candidate_refreeze_keeps_every_existing_barrier(prepared_root,acceptance_public_root,obstruction):
    registry,plan,gate=synthetic_parent(prepared_root)
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        if obstruction!='unfinished_tuning':
            execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        if obstruction=='formal_seen':
            execute_one(registry,plan,gate,'formal',acceptance_public_root/'formal')
        if obstruction=='inflight':
            gate.for_execution('formal').reserve(case_id='synthetic.candidate',protocol='deepseek',actor='main',purpose='work')
        if obstruction in {'execution_stopped','shared_stopped'}:
            original=prepared_root/'synthetic-stop-original.txt'
            original.write_text('Explicit synthetic stop observation; no real service incident.',encoding='utf-8')
            gate.for_execution('formal').request_stop(scope='shared' if obstruction=='shared_stopped' else 'execution',reason='synthetic accepted stop',source=original,case_id='synthetic.candidate',protocol='deepseek')
        candidate=gate.propose_candidate()
        before=(prepared_root/'ledger.json').read_bytes()
        with pytest.raises(SharedControlError):
            approve_candidate(gate,candidate,grant_changes={'total_microusd':29491} if obstruction=='amount' else None,
                decision_changes={'parent_id':'another-parent'} if obstruction=='identity' else None,
                review_sha='0'*64 if obstruction=='review_sha' else None)
        assert (prepared_root/'ledger.json').read_bytes()==before
        (acceptance_public_root/'retained-refusal.json').parent.mkdir(parents=True,exist_ok=True)
        (acceptance_public_root/'retained-refusal.json').write_bytes(before)
    finally:
        if parent_index.exists():parent_index.unlink()


def test_candidate_rejects_plan_substitution_during_explicit_new_freeze(prepared_root,acceptance_public_root):
    registry,plan,gate=synthetic_parent(prepared_root)
    try:
        changed=plan.model_copy(update={'parent_budget':plan.parent_budget.model_copy(update={'request_cap':3})})
        with pytest.raises(SharedControlError,match='plan|candidate|Candidate'):
            gate.propose_candidate(new_plan=changed)
    finally:
        parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
        if parent_index.exists():parent_index.unlink()


def test_damaged_candidate_record_fails_closed_as_shared_control(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance.preparation import digest
    registry,plan,gate=synthetic_parent(prepared_root)
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        current=approve_candidate(gate,gate.propose_candidate())
        path=gate.root/'ledger.json'
        value=json.loads(path.read_bytes())
        del value['candidates'][0]['directory']
        value['integrity']=digest({name:body for name,body in value.items() if name!='integrity'})
        path.write_text(json.dumps(value),encoding='utf-8')
        with pytest.raises(SharedControlError):
            current.for_execution('formal').validate(case_id='synthetic.candidate',protocol='deepseek')
    finally:
        if parent_index.exists():parent_index.unlink()

def test_genesis_grant_cannot_enter_formal_after_active_candidate_even_without_source_drift(prepared_root,acceptance_public_root):
    registry,plan,gate=synthetic_parent(prepared_root)
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        current=approve_candidate(gate,gate.propose_candidate())
        with pytest.raises(SharedControlError,match='candidate|Candidate|authorization'):
            gate.for_execution('formal').validate(case_id='synthetic.candidate',protocol='deepseek')
        current.for_execution('formal').validate(case_id='synthetic.candidate',protocol='deepseek')
        assert current.accounting_report()['actual_http']==1
    finally:
        if parent_index.exists():parent_index.unlink()


def test_candidate_accounting_archive_keeps_every_original_and_verifies_each_historical_grant(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance.formal import preserve_runtime
    from tests.support.agent_acceptance.reporting import verify_formal_accounting_archive
    import shutil
    registry,plan,gate=synthetic_parent(prepared_root)
    try:
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        current=approve_candidate(gate,gate.propose_candidate())
        execute_one(registry,plan,current,'formal',acceptance_public_root/'formal')
        runtime=prepared_root/'.scratch/archive-test'
        runtime.mkdir(parents=True)
        root=acceptance_public_root/'archive'
        root.mkdir()
        context=CaseContext(root,'deepseek','synthetic-archive','synthetic.candidate',execution=plan.executions[1],live_gate=current.for_execution('formal'))
        preserve_runtime(context,runtime,prepared_root)
        ledger=(prepared_root/'ledger.json').read_bytes()
        bundle=current.prepared()
        shutil.rmtree(prepared_root)
        saved=verify_formal_accounting_archive(root,grant=current.authorization,execution_id='formal')
        assert saved.binding==gate.authorization.binding
        import zipfile
        path=root/'accounting.zip'
        with zipfile.ZipFile(path) as archive:
            originals={name:archive.read(name) for name in archive.namelist()}
        assert originals['ledger.json']==ledger
        assert '.candidates/'+bundle.candidate_directory+'/candidate-review.json' in originals
        forged=json.loads(originals['ledger.json'])
        forged['attempts'][0]['authorization_id']=current.authorization.authorization_id
        from tests.support.agent_acceptance.preparation import digest
        forged['integrity']=digest({key:value for key,value in forged.items() if key!='integrity'})
        originals['ledger.json']=json.dumps(forged).encode()
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,body in originals.items():archive.writestr(name,body)
        with pytest.raises(SharedControlError,match='historical|Historical|grant'):
            verify_formal_accounting_archive(root,grant=current.authorization,execution_id='formal')
    finally:
        index=gate.parent_binding_path(plan.parent_budget.parent_id)
        if index.exists():index.unlink()
