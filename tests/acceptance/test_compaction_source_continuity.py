"""Summary provenance and continued work through public product/formal interfaces."""
from tests.support.agent_acceptance.formal import provider_config
from tests.support.agent_acceptance.formal_scenarios import scenario_config


def test_frozen_compaction_settings_match_the_approved_local_estimate() -> None:
    config=scenario_config('formal.compaction-material',provider_config('deepseek'))
    assert (config.context_window_override,config.output_token_reserve,config.context_safety_margin,
        config.compaction_trigger,config.compaction_target,config.compaction_recent)==(26000,4096,512,.70,.55,.10)


def test_three_actual_normal_summaries_preserve_sources_and_continue(acceptance_public_root) -> None:
    import asyncio,json
    from tests.support.agent_acceptance import AcceptancePlan,builtin_registry,run_plan
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=['formal.compaction-continuation']),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=='passed',outcome.facts
    assert outcome.facts['compaction_contract']['completed_summaries']>=3,outcome.facts
    assert outcome.facts['compaction_contract']['replacement_chain'] is True
    assert outcome.facts['compaction_contract']['original_sources_preserved'] is True
    assert outcome.facts['compaction_contract']['source_readback_verified'] is True
    contract=outcome.facts['compaction_contract']
    assert contract['post_summary_work_completed'] and contract['same_task_scope'] and contract['continuation_input_verified']
    assert outcome.product_status=='passed'
    assert outcome.semantic_status=='not_measured'
    assert outcome.user_acceptance=='pending'
    dedicated=json.loads(outcome.evidence['compaction_review'].read_bytes())
    assert dedicated['human_review']=='not_performed'
    assert all(set(row['criteria'])=={'numbers_subjects','corrections_status','source_use','forbidden_actions','pending_ownership','no_new_authority'} for row in dedicated['outputs'])


def test_risk_materials_register_all_seven_branches_and_fixed_two_plus_one() -> None:
    import json
    from pathlib import Path
    from tests.support.agent_acceptance import builtin_registry
    packet=json.loads((Path(__file__).parent/'materials/compaction/continuity/risk-materials.json').read_bytes())
    assert set(packet['families'])=={'R20','R21','R22','R23','R24','R25','R26'}
    registry=builtin_registry()
    for risk,family in packet['families'].items():
        case=registry.cases[family['case_id']]
        assert case.owner_ticket=='14' and case.frozen_material_input and case.formal_live
        assert [item['material_variant'] for item in family['executions']]==['baseline','baseline','variant']
        assert family['request_cap']==80
        assert set(case.output_standards)=={'main_answer','summary'}
        assert set(case.screening)==({'deepseek','google'} if risk in {'R20','R21','R22'} else {'deepseek'})


import pytest


@pytest.mark.parametrize('risk,protocol',[(r,'deepseek') for r in ('R20','R21','R22','R23','R24','R25','R26')]+[(r,'google') for r in ('R20','R21','R22')])
def test_frozen_three_executions_consume_their_material_and_keep_actual_source_timing(acceptance_public_root,prepared_root,risk,protocol) -> None:
    import asyncio,json
    from pathlib import Path
    from hashlib import sha256
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import AcceptancePlan,FrozenExecution,FrozenParentBudget,LiveAuthorization,LiveGate,builtin_registry,prepare_live,run_plan
    materials=Path(__file__).parent/'materials/compaction/continuity'
    specification=materials/'risk-materials.json'
    family=json.loads(specification.read_bytes())['families'][risk]
    rows=[FrozenExecution(execution_id=f'compaction:{prepared_root.name}:{risk}:{protocol}:{n}',case_id=family['case_id'],protocol=protocol,
        scenario_id=risk,material_family_id=family['family_id'],material_variant=item['material_variant'],material_path=materials/item['material'],
        material_sha256=sha256((materials/item['material']).read_bytes()).hexdigest(),startpoint=f'{prepared_root.name}-{risk}-{protocol}-isolated-{n}',order=n,
        equivalence_path=specification,equivalence_sha256=sha256(specification.read_bytes()).hexdigest(),risk_branch=family['branch'],request_cap=80,cost_cap_microusd=80*122880)
        for n,item in enumerate(family['executions'],1)]
    parent=FrozenParentBudget(parent_id='synthetic-compaction-'+prepared_root.name,accounting_root=prepared_root,request_cap=240,
        phase_microusd={'screening':240*122880,'quality':0,'judge':0},reserve_microusd=0,profile='synthetic')
    plan=AcceptancePlan(executions=rows,parent_budget=parent);registry=builtin_registry()
    activation=project_root(Path(__file__))/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    assert not activation.exists() and not prepared_root.exists()
    try:
        bundle=prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id='synthetic-compaction-frozen',binding=bundle.binding,transport='mock',parent_budget_id=parent.parent_id,
            total_limit=240,case_limits={row.execution_id:80 for row in rows},total_microusd=240*122880,case_microusd={row.execution_id:80*122880 for row in rows})
        result=asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases]==[row.execution_id for row in rows]
        for row,case in zip(rows,result.cases):
            assert case.tool_status=='passed',case.facts
            contract=case.facts['compaction_contract']
            assert contract['original_sources_preserved'] and contract['source_readback_verified']
            assert contract['full_original_chat_preserved'] and contract['all_mapping_sources_readable']
            assert contract['completed_summaries']>=(3 if risk=='R26' else 1)
            exchange=json.loads(case.evidence['exchange.json'].read_bytes())
            material=(materials/family['executions'][row.order-1]['material']).read_text(encoding='utf-8').strip()
            assert json.dumps(material,ensure_ascii=False)[1:-1] in json.dumps(exchange['sdk_inputs'][0],ensure_ascii=False)
            assert 'ordinary_facts' not in json.dumps(exchange['sdk_inputs'],ensure_ascii=False)
            assert case.evidence['frozen-compaction-material.txt'].read_text(encoding='utf-8').strip()==material
            assert contract['comparison_completed']==(risk in {'R21','R22'})
            if risk=='R22': assert contract['late_adoption_applied_before_comparison'] and contract['first_summary_precedes_adoption']
            if risk=='R23': assert contract['assistant_proposal_original_retained']
            if risk=='R24': assert contract['pasted_material_kept_as_quote']
            if risk=='R25': assert contract['later_correction_applied'] and contract['completed_action_retained'] and contract['cancelled_work_not_reactivated']
            if risk=='R26':
                assert contract['replacement_chain'] and contract['same_task_scope'] and contract['continuation_input_verified'] and contract['sdk_scope_reference_and_tools_preserved']
                assert case.product_status==('passed' if contract['post_summary_work_completed'] else 'failed')
            else: assert case.product_status=='passed',case.facts
            review=json.loads(case.evidence['compaction_review'].read_bytes())
            assert review['execution']['material_sha256']==row.material_sha256
            assert review['human_review']=='not_performed'
            assert all(set(output['criteria'])==set(case.facts['output_standard']['dimensions']) for output in review['outputs'])
            ledger=json.loads(case.evidence['ledger.json'].read_bytes())
            actual=[attempt for attempt in ledger['attempts'] if attempt['execution_id']==row.execution_id]
            assert 0<len(actual)<=80 and {'work','compaction'}<=set(attempt['purpose'] for attempt in actual)
            assert all(attempt['actor']=='main' and attempt['role']=='product' for attempt in actual)
            assert case.facts['real_http_attempts']==0 and case.semantic_status=='not_measured' and case.user_acceptance=='pending'
        assert json.loads(result.result_path.read_bytes())['planned_denominator']==3
    finally:
        if activation.exists() and not any((prepared_root/'.scratch').glob('execution-*')):
            data=json.loads(activation.read_bytes())
            assert data['parent_id']==parent.parent_id and Path(data['accounting_root']).resolve()==prepared_root.resolve()
            activation.unlink()


def test_review_retains_actual_pre_http_budget_refusal_without_inventing_response(tmp_path) -> None:
    import json,zipfile
    from pathlib import Path
    from hashlib import sha256
    from myrk.conversation import ConversationRecord
    from tests.support.agent_acceptance.core import CaseContext
    from tests.support.agent_acceptance.replay import OfflineSession
    from tests.support.agent_acceptance.compaction_review import write_review
    with zipfile.ZipFile(Path(__file__).parent/'materials/compaction/continuity/pre-http-refusal-originals.zip') as archive:
        origin=json.loads(archive.read('origin.json'))
        for name in ('record.json','exchange.json'):
            raw=archive.read(name)
            assert sha256(raw).hexdigest()==origin['originals'][name]['sha256']
            (tmp_path/name).write_bytes(raw)
    e=json.loads((tmp_path/'exchange.json').read_bytes())
    record=ConversationRecord.model_validate_json((tmp_path/'record.json').read_bytes())
    session=OfflineSession('deepseek',[]);session.exchanges=e['exchanges'];session.sdk_inputs=e['sdk_inputs']
    review=json.loads(write_review(CaseContext(tmp_path,'deepseek',e['run_id'],e['case_id']),record,session).read_bytes())
    assert len(review['samples'])==20 and len(e['exchanges'])==19
    last=review['samples'][-1]
    assert last['stage_result']=='not_sent' and last['response_utf8'] is None and last['exchange_index'] is None
    assert last['actual_input']['sdk_before_provider_adaptation'] is not None
    assert last['actual_input']['actual_wire_body'] is None
    assert last['formal_attempt']['status']=='failed'
    assert all(row['semantic_status']=='not_measured' for row in review['samples'])


def test_qg6_contribution_keeps_ten_templates_two_tuning_families_and_human_boundary() -> None:
    import json
    from pathlib import Path
    from tests.support.agent_acceptance.quality import QualityTask,TEMPLATES
    packet=json.loads((Path(__file__).parent/'materials/compaction/continuity/qg6-templates.json').read_bytes())
    tasks=[QualityTask.model_validate(row) for row in packet['tasks']]
    formal=[row for row in tasks if row.purpose=='formal'];tuning=[row for row in tasks if row.purpose=='tuning']
    assert len(formal)==10 and len(tuning)==2 and len({row.material_family_id for row in tasks})==12
    assert [row.template for row in formal]==list(TEMPLATES['QG6'])
    assert len({row.shape for row in formal})>=4
    assert formal[5].required_outputs['summary']>=3 and formal[9].required_outputs['summary']>=3
    assert packet['human_review']=='not_performed' and packet['unseen_eligible'] is False
    assert packet['canonical_material_assembly']=='ticket24_not_ready'
    for row in tasks:
        assert set(row.domain_standards)=={'main_answer','summary'}
        for standard in row.domain_standards.values():
            assert standard.dimensions['no_new_authority']['critical'] is True
            gold=json.loads((Path(__file__).resolve().parents[2]/row.gold_path).read_bytes())
            assert all(subject in standard.dimensions['numbers_subjects']['description'] for subject in gold['ordinary_expected_facts'])


@pytest.mark.parametrize('received_chunks',[0,1,2])
def test_early_closed_native_stream_ends_inflight_but_keeps_unknown_amount_and_stop(prepared_root,tmp_path,received_chunks) -> None:
    import asyncio,json
    from pathlib import Path
    from hashlib import sha256
    import httpx2
    from myrk.project_paths import project_root
    from tests.support.agent_acceptance import AcceptancePlan,FrozenExecution,FrozenParentBudget,LiveAuthorization,LiveGate,SharedControlError,builtin_registry,prepare_live
    from tests.support.agent_acceptance.recorded_response import RecordedResponseStream,capture_response_metadata,response_evidence
    source=Path(__file__).parent/'materials/compaction/continuity/r20-baseline.txt'
    execution=FrozenExecution(execution_id='stream-close:synthetic',case_id='formal.compaction-material',protocol='deepseek',scenario_id='R20',
        material_family_id='stream-close-control',material_variant='baseline',material_path=source,material_sha256=sha256(source.read_bytes()).hexdigest(),
        startpoint='isolated-stream-close',order=1,request_cap=8,cost_cap_microusd=8*122880)
    parent=FrozenParentBudget(parent_id='stream-close-'+prepared_root.name,accounting_root=prepared_root,request_cap=8,
        phase_microusd={'screening':8*122880,'quality':0,'judge':0},reserve_microusd=0,profile='synthetic')
    registry=builtin_registry();plan=AcceptancePlan(executions=[execution],parent_budget=parent)
    activation=project_root(Path(__file__))/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    try:
        bundle=prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id='synthetic-real-stream-consumption',binding=bundle.binding,parent_budget_id=parent.parent_id,transport='mock',
            total_limit=8,case_limits={execution.execution_id:8},total_microusd=8*122880,case_microusd={execution.execution_id:8*122880})
        gate=LiveGate(prepared_root,registry,grant).for_execution(execution.execution_id)
        chunks=[b'data: {"choices":[{"delta":{"content":"received"}}]}\n\n',b'data: [DONE]\n\n',b'data: ignored trailing source bytes\n\n']
        captures=[]
        class Source(httpx2.AsyncByteStream):
            async def __aiter__(self):
                for chunk in chunks: yield chunk
        async def consume():
            for number in range(1,7):
                body=json.dumps({'stream':True,'messages':[{'role':'user','content':str(number)}]}).encode()
                reserved=gate.reserve(case_id=execution.case_id,protocol='deepseek',actor='main',purpose='work',delegation_id=None,product_number=number,
                    sdk_input_id='stream-source-'+str(number),request_sha256=sha256(body).hexdigest())
                gate.mark_http_started(reserved)
                record={'status':200,'received_utf8':'','request_utf8':body.decode()}
                response=httpx2.Response(200,stream=Source(),headers={'content-type':'text/event-stream'})
                record['response_metadata']=capture_response_metadata(response,source='fixed_fixture')
                response.stream=RecordedResponseStream(response.stream,record,lambda:None,
                    lambda:gate.mark_response_complete(reserved,record),lambda reason:gate.mark_unknown(reserved,fault_origin=reason))
                iterator=response.aiter_bytes()
                for _ in range(received_chunks): await anext(iterator)
                await response.aclose()
                assert record.get('response_body_state')=='closed_before_eof'
                before=(prepared_root/'ledger.json').read_bytes()
                await response.aclose()
                assert (prepared_root/'ledger.json').read_bytes()==before
                assert record['received_utf8']==b''.join(chunks[:received_chunks]).decode()
                assert response_evidence(record)['status']=='incomplete'
                captures.append(record)
        asyncio.run(consume())
        ledger=json.loads((prepared_root/'ledger.json').read_bytes())
        assert len(ledger['attempts'])==6 and all(row['state']=='unknown' for row in ledger['attempts'])
        assert all(row['occupied_microusd']==row['reservation_microusd']>0 for row in ledger['attempts'])
        assert not any(row['state'] in {'reserved','http_started'} for row in ledger['attempts'])
        gate.stop(reason='actual test stop preserves closed unknown attempts')
        with pytest.raises(SharedControlError):
            gate.reserve(case_id=execution.case_id,protocol='deepseek',actor='main',purpose='work',delegation_id=None,product_number=7,
                sdk_input_id='stopped',request_sha256='0'*64)
        (tmp_path/'early-closed-originals.json').write_text(json.dumps(captures,indent=2),encoding='utf-8')
    finally:
        if activation.exists() and not any((prepared_root/'.scratch').glob('execution-*')): activation.unlink()


def test_real_summary_save_obstruction_preserves_durable_chat_and_sends_no_work(acceptance_public_root) -> None:
    import asyncio
    from tests.support.agent_acceptance import AcceptancePlan,builtin_registry,run_plan
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=['compaction.summary-save-obstruction']),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=='passed',outcome.facts
    assert outcome.product_status=='failed'
    assert outcome.facts['summary_received'] and outcome.facts['original_history_preserved']
    assert outcome.facts['durable_bytes_unchanged_after_lock'] and outcome.facts['work_http_attempts']==0
    assert outcome.facts['failure_origin']=='actual_windows_file_share_delete_obstruction'
    assert outcome.semantic_status=='not_measured' and outcome.critical_contracts['K4']=='unknown'
