"""Deliverables observed through the canonical real desktop acceptance entry."""
import asyncio
import pytest
from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan


def test_deliverables_are_selected_with_frozen_material_and_twenty_attempt_caps():
    case = builtin_registry().cases['formal.deliverables']
    assert case.group == 'deliverables' and case.owner_ticket == '17'
    assert case.frozen_material_input and case.formal_live
    assert case.screening['deepseek']['scenario_id'] == 'D18'
    assert case.screening['google']['scenario_id'] == 'G05'
    assert {row['request_cap'] for row in case.screening.values()} == {20}


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_multi_file_delivery_keeps_real_versions_ownership_and_reopen_state(protocol, acceptance_public_root):
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.deliverables'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == 'passed', outcome.facts
    facts = outcome.facts['deliverables']
    assert facts['new_modified_command_and_child_bytes'] is True
    assert facts['no_read_temp_or_noop_deliveries'] is True
    assert facts['same_change_same_identity'] is True
    assert facts['independent_before_after'] is True
    assert facts['actual_parent_child_identity'] is True
    assert facts['reopen_identity_and_zero_send'] is True
    assert facts['cross_chat_isolation'] is True
    assert facts['changed_and_missing_honest'] is True
    assert facts['request_attempts'] <= 20
    assert outcome.critical_contracts == {'K1':'passed', 'K2':'not_applicable', 'K3':'passed', 'K4':'passed', 'K5':'passed'}
    assert outcome.user_acceptance == 'pending' and outcome.semantic_status == 'not_measured'


def test_failed_command_retains_partial_bytes_without_reliable_delivery(acceptance_public_root):
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['deliverables.command-failure']),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == 'failed'
    assert outcome.facts['deliverables']['partial_bytes_observed'] is True
    assert outcome.facts['deliverables']['reliable_artifacts'] == []
    assert outcome.facts['deliverables']['actual_turn_status'] == 'failed'
    assert outcome.critical_contracts['K4'] == 'passed'
    assert outcome.user_acceptance == 'pending'


def test_stop_after_actual_partial_write_preserves_unknown_effects_and_usage(acceptance_public_root):
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['deliverables.command-stop']),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == 'failed'
    facts=outcome.facts['deliverables']
    assert facts['partial_bytes_observed'] and facts['stop_accepted_after_partial_write']
    assert facts['actual_turn_status'] == 'cancelled'
    assert facts['reliable_artifacts'] == []
    assert facts['requests_at_stop'] == facts['requests_after_stop'] == 2
    assert outcome.critical_contracts['K3'] == outcome.critical_contracts['K4'] == 'passed'
    assert outcome.user_acceptance == 'pending'


def test_changed_source_before_delivery_remains_unverified_and_unaccepted(acceptance_public_root):
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['deliverables.unverified']),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == outcome.target_status == 'failed'
    facts=outcome.facts['deliverables']
    assert facts['source_changed_before_delivery'] and facts['reliable_artifacts'] == []
    assert facts['actual_turn_status'] == 'completed'
    assert facts['required_delivery_status'] == 'failed'
    assert facts['delivery_status'] != 'accepted'
    assert outcome.critical_contracts['K4'] == 'passed'
    assert outcome.user_acceptance == 'pending'


def run_frozen_deliverables(acceptance_public_root, prepared_root, entries, suffix):
    from hashlib import sha256
    import json
    from pathlib import Path
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, prepare_live
    material=Path(__file__).parent/'materials/deliverables/task.txt'
    rows=[FrozenExecution(execution_id=f'{scenario.lower()}:{protocol}:{suffix}',case_id='formal.deliverables',protocol=protocol,
        scenario_id=scenario,material_family_id='deliverables-'+scenario.lower(),material_variant='baseline',
        material_path=material,material_sha256=sha256(material.read_bytes()).hexdigest(),
        startpoint=f'deliverables-{scenario.lower()}-{protocol}-{suffix}',order=number,request_cap=20,cost_cap_microusd=20*122880)
        for number,(scenario,protocol) in enumerate(entries,1)]
    parent=FrozenParentBudget(parent_id='synthetic-deliverables-'+prepared_root.name,accounting_root=prepared_root,
        request_cap=20*len(rows),phase_microusd={'screening':20*len(rows)*122880,'quality':0,'judge':0},reserve_microusd=0,profile='synthetic')
    plan=AcceptancePlan(executions=rows,parent_budget=parent)
    registry=builtin_registry()
    activation=project_root(Path(__file__)) / '.scratch/acceptance-parent-budgets' / (sha256(parent.parent_id.encode()).hexdigest()+'.json')
    assert not activation.exists() and not prepared_root.exists()
    try:
        bundle=prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id='synthetic-frozen-deliverables',binding=bundle.binding,transport='mock',
            parent_budget_id=parent.parent_id,total_limit=20*len(rows),case_limits={row.execution_id:20 for row in rows},
            total_microusd=20*len(rows)*122880,case_microusd={row.execution_id:row.cost_cap_microusd for row in rows})
        result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases] == [row.execution_id for row in rows]
        for row,case in zip(rows,result.cases):
            assert case.tool_status == case.product_status == 'passed', case.facts
            facts=case.facts['deliverables']
            assert all(facts[name] is True for name in ('new_modified_command_and_child_bytes','no_read_temp_or_noop_deliveries',
                'same_change_same_identity','independent_before_after','actual_parent_child_identity','reopen_identity_and_zero_send',
                'cross_chat_isolation','changed_and_missing_honest'))
            assert case.screening['scenario_id']==row.scenario_id and case.screening['request_cap']==20
            assert case.semantic_status=='not_measured' and case.user_acceptance=='pending'
            assert case.critical_contracts == {'K1':'passed','K2':'not_applicable','K3':'passed','K4':'passed','K5':'passed'}
            ledger=json.loads(case.evidence['ledger.json'].read_bytes())
            actual=[item for item in ledger['attempts'] if item['execution_id']==row.execution_id]
            assert 0<len(actual)<=20
            assert {item['actor'] for item in actual}=={'main','delegation'}
            assert all(item['role']=='product' and item['reservation_microusd']>0 for item in actual)
            assert len({item['sdk_input_id'] for item in actual})==len(actual)==facts['request_attempts']
            assert case.facts['real_http_attempts']==0 and case.facts['processes_complete']
            assert facts['controlled_user_acceptance']=='accepted_by_synthetic_UI_actor_only'
            prepared=json.loads(case.evidence['prepared.json'].read_bytes())
            assert 'tests/support/agent_acceptance/desktop/deliverables.mjs' in prepared['files']
            assert prepared['execution']['formal.deliverables']['compiled_sources']
        assert json.loads(result.result_path.read_bytes())['planned_denominator']==len(rows)
    finally:
        if activation.exists() and not any((prepared_root/'.scratch').glob('execution-*')):
            assert not activation.is_symlink() and not activation.is_junction()
            data=json.loads(activation.read_bytes())
            assert data['parent_id']==parent.parent_id and Path(data['accounting_root']).resolve()==prepared_root.resolve()
            activation.unlink()


def test_frozen_d18_g05_keep_two_ordinary_executions_and_shared_twenty_attempt_caps(acceptance_public_root, prepared_root):
    run_frozen_deliverables(acceptance_public_root,prepared_root,[("D18","deepseek"),("G05","google")],"1")


def test_g05_new_explicit_tooling_batch_preserves_original_reopen_failure_and_unknown_work_counts(acceptance_public_root, prepared_root):
    run_frozen_deliverables(acceptance_public_root,prepared_root,[("G05","google")],"tooling-recheck-3")


@pytest.mark.acceptance_desktop
def test_public_formal_reopen_failure_preserves_prior_runtime_after_one_allowed_request(acceptance_public_root, prepared_root):
    from hashlib import sha256
    import json
    from pathlib import Path
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, SharedControlError, prepare_live

    material=Path(__file__).parent/'materials/deliverables/task.txt'
    execution=FrozenExecution(execution_id='g05:google:failure-retention-regression:'+prepared_root.name,case_id='formal.deliverables',protocol='google',
        scenario_id='G05',material_family_id='deliverables-failure-retention',material_variant='baseline',material_path=material,
        material_sha256=sha256(material.read_bytes()).hexdigest(),startpoint='g05-failure-retention-isolated-'+prepared_root.name,order=1,
        request_cap=1,cost_cap_microusd=122880)
    parent=FrozenParentBudget(parent_id='synthetic-deliverables-retention-'+prepared_root.name,accounting_root=prepared_root,
        request_cap=1,phase_microusd={'screening':122880,'quality':0,'judge':0},reserve_microusd=0,profile='synthetic')
    plan=AcceptancePlan(executions=[execution],parent_budget=parent)
    registry=builtin_registry()
    activation=project_root(Path(__file__)) / '.scratch/acceptance-parent-budgets' / (sha256(parent.parent_id.encode()).hexdigest()+'.json')
    assert not activation.exists() and not prepared_root.exists()
    closed=False
    try:
        bundle=prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id='synthetic-one-request-retention-regression',binding=bundle.binding,transport='mock',
            parent_budget_id=parent.parent_id,total_limit=1,case_limits={execution.execution_id:1},
            total_microusd=122880,case_microusd={execution.execution_id:122880})
        with pytest.raises(SharedControlError,match=r'Frozen formal entry failed pre-connection validation \(exit 1\)'):
            asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        case=acceptance_public_root/'formal.deliverables/google'/execution.directory_name
        failed=json.loads((case/'phase-validation-failure.json').read_bytes())
        assert failed['phase']=='reopen' and failed['returncode']==1
        assert failed['prior_phases']==[{'phase':'work','exit_code':0}]
        assert failed['prior_runtime_and_ledger_preserved'] is True
        for phase,returncode in [('work',0),('reopen',1)]:
            result=json.loads((case/f'{phase}-validate-result.json').read_bytes())
            assert result['case_id']=='formal.deliverables' and result['protocol']=='google' and result['phase']==phase
            assert result['returncode']==returncode
            assert result['argv'][1:5]==['-B','-m','tests.support.agent_acceptance.formal_cli','validate']
            assert Path(result['argv'][-1]).parts[-2:]==(phase,'authorization.json')
            assert sha256((case/f'{phase}-validate.stdout').read_bytes()).hexdigest()==result['stdout_sha256']
            assert sha256((case/f'{phase}-validate.stderr').read_bytes()).hexdigest()==result['stderr_sha256']
        assert (case/'work-validate.stdout').read_text(encoding='utf-8').strip()==bundle.binding
        assert 'unexecuted mandatory request envelope protected' in (case/'reopen-validate.stderr').read_text(encoding='utf-8')
        prepared=json.loads((case/'prepared.json').read_bytes())
        ledger=json.loads((case/'ledger.json').read_bytes())
        assert prepared['binding']==bundle.binding
        assert prepared['plan']['executions'][0]['request_cap']==1
        assert ledger['authorization']['total_limit']==1
        assert ledger['case_stops'][execution.execution_id]['reason']=='unexecuted mandatory request envelope protected'
        assert len(ledger['attempts'])==1 and ledger['attempts'][0]['execution_id']==execution.execution_id
        exchange=json.loads((case/'work/receiver/exchange.json').read_bytes())
        transport=json.loads((case/'work/receiver/transport.json').read_bytes())
        assert len(exchange['exchanges'])==transport['mock_http_attempts']==1
        assert transport['real_http_attempts']==transport['blocked_unplanned_http_attempts']==0
        assert len(exchange['sdk_inputs'])==2
        sent=exchange['exchanges'][0]
        assert sent['status']==200 and sent['received_utf8']
        assert (case/'materials/input.txt').read_bytes()==(material.parent/'input.txt').read_bytes()
        assert (material.parent/'input.txt').read_text(encoding='utf-8').strip() in json.dumps(exchange['sdk_inputs'][1]['messages'],ensure_ascii=False)
        assert sent['request_identity']['number']==1 and sent['request_identity']['actor']=='main'
        assert sent['sdk_input_id']==ledger['attempts'][0]['sdk_input_id']
        assert sha256(sent['request_utf8'].encode()).hexdigest()==ledger['attempts'][0]['request_sha256']
        assert [sdk['request_identity']['number'] for sdk in exchange['sdk_inputs']]==[1,2]
        assert exchange['sdk_inputs'][1]['sdk_input_id'] not in {wire['sdk_input_id'] for wire in exchange['exchanges']}
        saved=list((case/'records').glob('*.json'))
        assert len(saved)==1
        record=json.loads(saved[0].read_bytes())
        turn=record['turns'][-1]
        assert turn['status']!='completed' and turn['delivery_status']!='accepted' and turn['artifacts']==[]
        assert len(turn['request_attempts'])==2
        assert len(turn['actions'])==1 and turn['actions'][0]['kind']=='read' and turn['actions'][0]['status']=='completed'
        assert turn['actions'][0]['task_id']==turn['task']['id']
        processes=json.loads((case/'work/processes.json').read_bytes())
        closed=processes['complete'] and all(item['state']=='exited' and item['completed'] and item['identity']['created_filetime'] for item in processes['processes'])
        assert closed
        owner=json.loads((case/'work/owner/state.json').read_bytes())
        assert owner['synthetic_store_created'] is True and Path(owner['profile']).is_dir()
        assert not (case/'work/owner/profile').exists() and not (case/'work/receiver/profile').exists()
        assert not (case/'work/receiver/session').exists()
        assert all(not any(part in {'profile','session','cache'} for part in path.relative_to(case).parts)
            for path in case.rglob('*') if path.is_file())
        if (case/'long-path-originals.json').exists():
            assert all(not any(part in {'profile','session','cache'} for part in Path(row['logical_path']).parts)
                for row in json.loads((case/'long-path-originals.json').read_bytes()))
        (case/'failure-retention-regression-observation.json').write_text(json.dumps({'execution_id':execution.execution_id,
            'native_terminal_mock':len(exchange['exchanges']),'sdk_inputs':len(exchange['sdk_inputs']),
            'existing_gate_second_request_refused':True,'work_runner_exit':0,'reopen_validation_exit':1,
            'prior_files_preserved':True,'owned_processes_exited':closed,'disposable_profiles_not_copied':True,
            'real_supplier_http':0,'user_acceptance':'pending','old_executions_replaced':False},indent=2)+'\n',encoding='utf-8')
    finally:
        if activation.exists() and closed:
            data=json.loads(activation.read_bytes())
            assert data['parent_id']==parent.parent_id and Path(data['accounting_root']).resolve()==prepared_root.resolve()
            assert not activation.is_symlink() and not activation.is_junction()
            (case/'parent-budget-binding.json').write_bytes(activation.read_bytes())
            activation.unlink()
