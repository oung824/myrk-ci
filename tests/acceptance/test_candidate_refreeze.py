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


@pytest.mark.acceptance_desktop
def test_reviewed_candidate_reaches_actual_main_and_keeps_genesis_authority(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry
    from tests.support.agent_acceptance.formal import environment
    from tests.support.agent_acceptance.preparation import LiveExecutionRequest,bundle_material_root,read_prepared_snapshot
    import subprocess,sys
    from types import SimpleNamespace
    registry=builtin_registry()
    directory=SOURCE/'tests/acceptance/materials/independent-review'
    rows=tuple(FrozenExecution(execution_id=name,case_id='formal.independent-review',protocol='deepseek',scenario_id=scenario,
        material_family_id=family,material_variant='baseline',material_path=directory/material,material_sha256=sha256((directory/material).read_bytes()).hexdigest(),
        startpoint=name,order=n,stage='quality',request_cap=16,cost_cap_microusd=235936)
        for n,(name,scenario,family,material) in enumerate((('tuning','QG3.tuning.1','synthetic-retention-control','qg3-retention-register.txt'),
            ('formal','QG3.formal.1','synthetic-release-control','qg3-release-checksum.txt')),1))
    parent=FrozenParentBudget(parent_id='synthetic-main-candidate-'+prepared_root.name,accounting_root=prepared_root,request_cap=32,
        phase_microusd={'screening':0,'quality':471872,'judge':0},reserve_microusd=0,profile='synthetic')
    plan=AcceptancePlan(executions=rows,parent_budget=parent,mode='prepare-live')
    bundle=prepare_live(plan,root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-main-genesis',binding=bundle.binding,total_limit=32,
        case_limits={'tuning':16,'formal':16},total_microusd=471872,case_microusd={'tuning':235936,'formal':235936},transport='mock',parent_budget_id=parent.parent_id)
    gate=LiveGate(prepared_root,registry,grant)
    original_prompt=PROMPT.read_bytes()
    parent_index=gate.parent_binding_path(parent.parent_id)
    def execute_snapshot(current,name):
        root=acceptance_public_root/name
        root.mkdir(parents=True)
        request=root/'execution-request.json'
        request.write_text(LiveExecutionRequest(run_root=prepared_root,grant=current.authorization,execution_ids=(name,)).model_dump_json(),encoding='utf-8')
        fixed=bundle_material_root(read_prepared_snapshot(prepared_root,binding=current.authorization.binding))
        process=subprocess.run([sys.executable,'-B','-m','tests.support.agent_acceptance.formal_cli','execute',str(request),'--output',str(root/'formal-result')],
            cwd=fixed,env={**environment(root/'driver-environment'),'PYTHONPATH':str(fixed)},capture_output=True)
        (root/'controller.stdout').write_bytes(process.stdout)
        (root/'controller.stderr').write_bytes(process.stderr)
        assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
        data=json.loads((root/'formal-result/result.json').read_bytes())
        assert data['planned_denominator']==2
        return SimpleNamespace(cases=[SimpleNamespace(**{**item,'evidence':{key:Path(value) if Path(value).is_absolute() else root/'formal-result'/value for key,value in item['evidence'].items()}}) for item in data['cases']])
    try:
        first=execute_snapshot(gate,'tuning')
        assert next(item for item in first.cases if item.execution_id=='tuning').facts['processes_complete']
        before=gate.accounting_report()
        original=(prepared_root/'prepared.json').read_bytes()
        PROMPT.write_bytes(original_prompt+b'\nExplicit synthetic prompt-source change after actual Main tuning.\n')
        with pytest.raises(SharedControlError):gate.for_execution('formal').validate(case_id=rows[1].case_id,protocol='deepseek')
        candidate=gate.propose_candidate()
        current=approve_candidate(gate,candidate)
        result=execute_snapshot(current,'formal')
        outcome=next(item for item in result.cases if item.execution_id=='formal')
        assert outcome.product_status=='passed' and outcome.facts['processes_complete']
        report=current.accounting_report()
        assert report['actual_http']>before['actual_http']>0
        assert report['attempts'][:len(before['attempts'])]==before['attempts']
        assert report['occupied_microusd']>=before['occupied_microusd']>0
        assert (prepared_root/'prepared.json').read_bytes()==original
        assert all(row['authorization_id']=='synthetic-new-candidate' for row in report['attempts'][len(before['attempts']):])
        originals=[json.loads(path.read_bytes()) for path in outcome.evidence.values() if path.name=='authorization.json']
        assert originals and all(item['candidate_binding']==candidate.binding for item in originals)
        assert outcome.facts['real_http_attempts']==0 and outcome.semantic_status=='not_measured'
    finally:
        PROMPT.write_bytes(original_prompt)
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

def test_reviewed_new_criteria_packet_preserves_tasks_and_parent(prepared_root,acceptance_public_root):
    from uuid import uuid4
    import shutil
    from tests.support.agent_acceptance import builtin_registry, freeze_quality, quality_plan
    from tests.support.agent_acceptance.quality_packet import QualityPacketRef
    packet_parent=SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'
    packet_id=uuid4().hex
    roots=[packet_parent/('candidate-v1-'+packet_id),packet_parent/('candidate-v2-'+packet_id)]
    plans=[]
    registries=[]
    parent=FrozenParentBudget(parent_id='synthetic-criteria-'+prepared_root.name,accounting_root=prepared_root,request_cap=2520,
        phase_microusd={'screening':0,'quality':39076920,'judge':0},reserve_microusd=0,profile='synthetic')
    parent_index=None
    try:
        for number,root in enumerate(roots,1):
            tasks=json.loads((SOURCE/'tests/acceptance/materials/quality/synthetic.json').read_bytes())
            for task in tasks:
                for kind in ('input','gold'):
                    path=root/kind/(task['task_id']+'.txt')
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text(task.pop(kind),encoding='utf-8')
                    task[kind+'_path']=path
                task['prior_uses']=['implementation']
            frozen=freeze_quality(tasks,destination=root/'freeze.json',provenance='synthetic',source_version='synthetic-candidate-'+str(number),
                environment={'kind':'synthetic'},models={'deepseek':{'model':'deepseek-flash'},'google':{'model':'gemini-2.5-flash'}},
                criteria_version='synthetic-criteria-v'+str(number),qualification_families=[],intended_use='implementation_regression')
            registry=builtin_registry(quality_packet=QualityPacketRef(path=frozen,sha256=sha256(frozen.read_bytes()).hexdigest()))
            plans.append(quality_plan(frozen,registry=registry,parent_budget=parent,mode='offline').model_copy(update={'mode':'prepare-live'}))
            registries.append(registry)
        bundle=prepare_live(plans[0],root=prepared_root,registry=registries[0])
        grant=LiveAuthorization(authorization_id='synthetic-criteria-genesis',binding=bundle.binding,total_limit=2520,
            case_limits={row.execution_id:row.request_cap for row in plans[0].executions},total_microusd=39076920,
            case_microusd={row.execution_id:row.cost_cap_microusd for row in plans[0].executions},transport='mock',parent_budget_id=parent.parent_id)
        gate=LiveGate(prepared_root,registries[0],grant)
        parent_index=gate.parent_binding_path(parent.parent_id)
        tuning=[row for row in plans[0].executions if '.tuning.' in row.scenario_id]
        for row in tuning:
            gate.for_execution(row.execution_id).finish_execution(CaseOutcome('not_executed',{'reason':'Synthetic tuning closed without execution; no replacement task.'},
                case_id=row.case_id,protocol=row.protocol,execution_id=row.execution_id))
        candidate=gate.propose_candidate(new_plan=plans[1])
        formal=next(row for row in plans[0].executions if '.formal.' in row.scenario_id)
        from tests.support.agent_acceptance.preparation import CandidateFreezeReview
        review=CandidateFreezeReview(parent_id=parent.parent_id,accounting_root=prepared_root,previous_binding=bundle.binding,candidate_binding=candidate.binding,
            affected_execution_ids=(formal.execution_id,),tuning_execution_ids=tuple(row.execution_id for row in tuning),
            tuning_results={row.execution_id:sha256(json.dumps(vars(gate.for_execution(row.execution_id).recorded_outcome()),default=str,sort_keys=True).encode()).hexdigest() for row in tuning},
            untuned_materials={formal.execution_id:formal.material_sha256},review_kind='synthetic_control',reviewer='synthetic-control-no-human-signature',
            reason='Exact revised criteria were reviewed before any formal output; original tasks and caps retained.')
        original=prepared_root/'new-criteria-review.json'
        original.write_text(review.model_dump_json(),encoding='utf-8')
        current=gate.refreeze_candidate(candidate,review=original,review_sha256=sha256(original.read_bytes()).hexdigest(),
            grant=grant.model_copy(update={'authorization_id':'synthetic-criteria-revised','binding':candidate.binding}))
        current.for_execution(formal.execution_id).validate(case_id=formal.case_id,protocol=formal.protocol)
        assert candidate.plan.quality_packet==plans[1].quality_packet
        assert [row.execution_id for row in candidate.plan.executions]==[row.execution_id for row in plans[0].executions]
        assert [row.material_sha256 for row in candidate.plan.executions]==[row.material_sha256 for row in plans[0].executions]
        assert current.accounting_report()['actual_http']==0
        assert current.accounting_report()['request_limit']==2520 and current.accounting_report()['cost_limit_microusd']==39076920
    finally:
        if parent_index is not None and parent_index.exists():parent_index.unlink()
        for root in roots:
            if root.exists():shutil.rmtree(root)

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


def test_actual_legacy_snapshot_reader_cannot_reserve_after_new_candidate_codec(prepared_root,acceptance_public_root):
    import os
    import shutil
    import subprocess
    import sys
    root=acceptance_public_root
    root.mkdir(parents=True)
    old=root/'old-snapshot-source'
    old.mkdir()
    files=[p for directory in ('myrk','tests/support','tests/acceptance/materials','tests/acceptance/plans') for p in (SOURCE/directory).rglob('*') if p.is_file()]
    files += list((SOURCE/'tests/acceptance').glob('*.py'))
    files += [SOURCE/name for name in ('tests/__init__.py','pyproject.toml','conftest.py','tests/conftest.py','requirements.txt','requirements-dev.txt','requirements.lock','scripts/run_checks.py') if (SOURCE/name).is_file()]
    for path in files:
        target=old/path.relative_to(SOURCE)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(path.read_bytes())
    legacy=SOURCE/'tests/acceptance/materials/candidate-refreeze/legacy-preparation-v2.py.txt'
    (old/'tests/support/agent_acceptance/preparation.py').write_bytes(legacy.read_bytes())
    environment={**os.environ,'PYTHONPATH':str(old),'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8'}
    bootstrap = """
from pathlib import Path
import sys
from tests.acceptance.test_candidate_refreeze import execute_one, synthetic_parent
from tests.support.agent_acceptance import preparation
from tests.support.agent_acceptance.eligibility import preparation_eligibility

preparation.preparation_eligibility = lambda: preparation_eligibility(deepseek_standard="2026-11-16")
registry, plan, gate = synthetic_parent(Path(sys.argv[1]))
execute_one(registry, plan, gate, "tuning", Path(sys.argv[2]))
(gate.root / "legacy-grant.json").write_text(gate.authorization.model_dump_json(), encoding="utf-8")
(gate.root / "legacy-plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
"""
    process=subprocess.run([sys.executable,'-B','-c',bootstrap,str(prepared_root),str(root/'old-tuning')],cwd=old,env=environment,capture_output=True)
    (root/'bootstrap.stdout').write_bytes(process.stdout)
    (root/'bootstrap.stderr').write_bytes(process.stderr)
    assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
    grant=LiveAuthorization.model_validate_json((prepared_root/'legacy-grant.json').read_bytes())
    plan=AcceptancePlan.model_validate_json((prepared_root/'legacy-plan.json').read_bytes())
    gate=LiveGate(prepared_root,candidate_registry(),grant)
    parent_index=gate.parent_binding_path(plan.parent_budget.parent_id)
    try:
        candidate = gate.propose_candidate()
        deepseek = next(route for route in candidate.eligibility if route.protocol == "deepseek")
        assert deepseek.standard_version == "2026-11-16"
        assert deepseek.thinking == {"reasoning_effort": "none"}
        assert "entry_point" not in deepseek.model_dump()
        current=approve_candidate(gate,candidate)
        before=(prepared_root/'ledger.json').read_bytes()
        (root/'ledger-before-old-reader.json').write_bytes(before)
        source="from pathlib import Path\nimport json,sys\nfrom tests.support.agent_acceptance import LiveGate,LiveAuthorization\nfrom tests.acceptance.test_candidate_refreeze import candidate_registry\np=Path(sys.argv[1]);g=LiveGate(p,candidate_registry(),LiveAuthorization.model_validate_json((p/'legacy-grant.json').read_bytes())).for_execution('formal')\ntry:g.reserve(case_id='synthetic.candidate',protocol='deepseek',actor='main',purpose='work')\nexcept Exception as e:print(json.dumps({'rejected':True,'error_type':type(e).__name__,'reason':str(e)}));sys.exit(0)\nprint(json.dumps({'rejected':False}));sys.exit(2)\n"
        observed=subprocess.run([sys.executable,'-B','-c',source,str(prepared_root)],cwd=old,env=environment,capture_output=True)
        (root/'legacy-reader.stdout').write_bytes(observed.stdout)
        (root/'legacy-reader.stderr').write_bytes(observed.stderr)
        (root/'ledger-after-old-reader.json').write_bytes((prepared_root/'ledger.json').read_bytes())
        assert observed.returncode==0,observed.stdout.decode('utf-8',errors='replace')
        observation=json.loads(observed.stdout)
        assert observation['rejected'] is True and observation['error_type']=='SharedControlError'
        assert 'candidate_codec' in observation['reason']
        assert (prepared_root/'ledger.json').read_bytes()==before
        assert current.accounting_report()['reserved_requests']==1
    finally:
        if parent_index.exists():parent_index.unlink()
        if old.exists():shutil.rmtree(old)


def test_reviewed_candidate_transfers_the_original_reserve_using_its_active_execution(prepared_root,acceptance_public_root):
    registry,plan,gate=synthetic_parent(prepared_root,reserve=1000)
    basis=prepared_root/'original-reserve-basis.txt'
    basis.write_text('Synthetic verified fixture bound; original reserve only, no new executions.',encoding='utf-8')
    try:
        gate.transfer_reserve(stage='quality',delta_microusd=500,basis=basis)
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        before=gate.accounting_report()
        original_grant=json.loads((prepared_root/'ledger.json').read_bytes())['authorization']
        current=approve_candidate(gate,gate.propose_candidate())
        current.transfer_reserve(stage='quality',delta_microusd=500,basis=basis)
        report=current.accounting_report()
        assert report['reserve_microusd']==0 and report['phase_microusd']['quality']==30492
        assert report['attempts']==before['attempts'] and report['reserved_requests']==1
        assert json.loads((prepared_root/'ledger.json').read_bytes())['authorization']==original_grant
        assert len(report['reserve_transfers'])==2 and report['reserve_transfers'][1]['before_request']==2
        assert execute_one(registry,plan,current,'formal',acceptance_public_root/'formal').product_status=='passed'
    finally:
        index=gate.parent_binding_path(plan.parent_budget.parent_id)
        if index.exists():index.unlink()


def test_candidate_budget_stop_preserves_genesis_grant_and_full_unknown_reservation(prepared_root,acceptance_public_root):
    from tests.support.agent_acceptance.preparation import CaseBudgetExceeded
    registry,plan,gate=synthetic_parent(prepared_root)
    try:
        execute_one(registry,plan,gate,'tuning',acceptance_public_root/'tuning')
        original=json.loads((prepared_root/'ledger.json').read_bytes())['authorization']
        current=approve_candidate(gate,gate.propose_candidate()).for_execution('formal')
        number=current.reserve(case_id='synthetic.candidate',protocol='deepseek',actor='main',purpose='work')
        current.mark_unknown(number,fault_origin='Synthetic interrupted request: full amount remains occupied.')
        before=current.accounting_report()
        with pytest.raises(CaseBudgetExceeded):
            current.reserve(case_id='synthetic.candidate',protocol='deepseek',actor='main',purpose='retry')
        saved=json.loads((prepared_root/'ledger.json').read_bytes())
        assert saved['authorization']==original
        report=current.accounting_report()
        assert report['attempts']==before['attempts'] and report['reserved_requests']==2
        assert report['occupied_microusd']==29492 and report['attempts'][-1]['state']=='unknown'
        assert report['case_stops']
    finally:
        index=gate.parent_binding_path(plan.parent_budget.parent_id)
        if index.exists():index.unlink()


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
