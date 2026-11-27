"""The canonical bounded review and real failed fact save preserve their originals."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
from uuid import uuid4
import pytest
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet


@pytest.fixture
def auxiliary_packet():
    root=SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'/('memory-aux-'+uuid4().hex)
    try:
        yield build_implementation_quality_packet(root)
    finally:
        assert root.parent==SOURCE/'.scratch/retro-agent-live-acceptance-20261116/quality-packets'
        if root.exists():shutil.rmtree(root)


def test_bounded_review_uses_only_memory_tools_and_preserves_the_delivery(auxiliary_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_memory_aux import exercise_memory
    outcome=asyncio.run(exercise_memory(auxiliary_packet,'QG4.formal.4','deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed',outcome.facts
    facts=outcome.facts['quality_memory_contract']
    assert facts['memory_reviews']==1 and facts['review_tools_are_memory_only'] is True
    assert facts['delivery_preserved'] is True and facts['review_history_separate'] is True
    assert facts['current_texts']==['先核原件日期再比对总额，本次两份付款副本日期不同；未证明一般可靠率。']
    assert outcome.facts['real_http_attempts']==0 and outcome.semantic_status=='not_measured'


def test_actual_fact_save_failure_preserves_original_bytes_and_delivery(auxiliary_packet,acceptance_public_root):
    from tests.support.agent_acceptance.quality_memory_aux import exercise_memory
    outcome=asyncio.run(exercise_memory(auxiliary_packet,'QG4.formal.10','deepseek',root=acceptance_public_root))
    assert outcome.product_status=='passed',outcome.facts
    facts=outcome.facts['quality_memory_contract']
    assert facts['actual_save_failure'] is True and facts['fact_original_unchanged'] is True
    assert facts['failed_effect']=='unknown' and facts['denial_source'] is None
    assert facts['current_texts']==[] and facts['delivery_preserved'] is True
    assert outcome.critical_contracts['K4']=='unknown'
    assert outcome.facts['real_http_attempts']==0 and outcome.semantic_status=='not_measured'


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize('task_id',['QG4.formal.4','QG4.formal.10','QG4.tuning.2'])
def test_auxiliary_memory_task_reaches_actual_main_with_its_frozen_packet(auxiliary_packet,prepared_root,acceptance_public_root,task_id):
    import subprocess,sys
    from tests.support.agent_acceptance import AcceptancePlan,FrozenExecution,FrozenParentBudget,LiveAuthorization,builtin_registry,prepare_live
    from tests.support.agent_acceptance.quality_packet import read_quality_packet
    from tests.support.agent_acceptance.formal import environment
    from tests.support.agent_acceptance.preparation import LiveExecutionRequest
    from myrk.project_paths import project_root
    packet=read_quality_packet(auxiliary_packet)
    task=next(row for row in packet.tasks if row.task_id==task_id)
    parent=FrozenParentBudget(parent_id='synthetic-memory-aux-main-'+prepared_root.name,accounting_root=prepared_root,request_cap=16,
        phase_microusd={'screening':0,'quality':235936,'judge':0},reserve_microusd=0,profile='synthetic')
    row=FrozenExecution(execution_id='synthetic-main:'+task_id,case_id='quality.'+task_id.lower(),protocol='deepseek',scenario_id=task_id,
        material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
        startpoint='synthetic-main:'+task_id,order=1,stage='quality',request_cap=16,cost_cap_microusd=235936)
    plan=AcceptancePlan(executions=(row,),parent_budget=parent,quality_packet=auxiliary_packet,mode='prepare-live')
    registry=builtin_registry(quality_packet=auxiliary_packet)
    bundle=prepare_live(plan,root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-memory-aux-main',binding=bundle.binding,total_limit=16,case_limits={row.execution_id:16},
        total_microusd=235936,case_microusd={row.execution_id:235936},transport='mock',parent_budget_id=parent.parent_id)
    root=acceptance_public_root
    root.mkdir(parents=True)
    from zipfile import ZipFile, ZIP_DEFLATED
    source_archive=root/'executed-source-snapshot.zip'
    with ZipFile(source_archive,'x',ZIP_DEFLATED) as archive:
        for name,expected in bundle.files.items():
            original=(prepared_root/name).read_bytes()
            assert sha256(original).hexdigest()==expected
            archive.writestr(name,original)
        archive.writestr('prepared.json',(prepared_root/'prepared.json').read_bytes())
    (root/'executed-source-snapshot-index.json').write_text(json.dumps({'binding':bundle.binding,
        'archive_sha256':sha256(source_archive.read_bytes()).hexdigest(),'members':len(bundle.files)+1,
        'original_source_root':str(prepared_root)},sort_keys=True),encoding='utf-8')
    request=root/'execution-request.json'
    request.write_text(LiveExecutionRequest(run_root=prepared_root,grant=grant).model_dump_json(),encoding='utf-8')
    try:
        process=subprocess.run([sys.executable,'-B','-m','tests.support.agent_acceptance.formal_cli','execute',str(request),'--output',str(root/'formal-result')],
            cwd=prepared_root,env={**environment(root/'driver-environment'),'PYTHONPATH':str(prepared_root)},capture_output=True)
        (root/'controller.stdout').write_bytes(process.stdout)
        (root/'controller.stderr').write_bytes(process.stderr)
        assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
        result=json.loads((root/'formal-result/result.json').read_bytes())
        outcome=result['cases'][0]
        assert outcome['tool_status']==outcome['product_status']=='passed',outcome['facts']
        assert outcome['facts']['processes_complete'] is True and outcome['facts']['real_http_attempts']==0
        facts=outcome['facts']['quality_memory_contract']
        if task_id in {'QG4.formal.4', 'QG4.tuning.2'}:
            assert facts['memory_reviews']==1 and facts['review_tools_are_memory_only'] and facts['delivery_preserved']
            budget = facts['controller_request_budget']
            assert budget['origin'] == 'validated_controller_ledger' and budget['binding'] == bundle.binding
            assert budget['execution_id'] == row.execution_id and budget['case_id'] == row.case_id
            assert budget['frozen_request_cap'] == row.request_cap and budget['approved_request_cap'] == grant.case_limits[row.execution_id]
            assert budget['reserved_requests'] == len(budget['requests']) <= min(budget['frozen_request_cap'], budget['approved_request_cap'])
            assert budget['scope'] == registry.cases[row.case_id].product_stop_scope == 'execution'
            assert outcome['facts']['budget']['product_request_owners'] == {}
            assert all(item['task_id'] is None for item in budget['requests'])
            assert facts['request_budget_status'] == 'passed'
        else:
            assert facts['actual_save_failure'] and facts['fact_original_unchanged'] and facts['delivery_preserved']
        assert outcome['semantic_status']=='not_measured' and result['planned_denominator']==1
    finally:
        index=project_root(SOURCE)/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
        if index.exists():index.unlink()


@pytest.mark.parametrize('task_id',['QG4.formal.4','QG4.formal.10'])
def test_missing_auxiliary_turns_do_not_become_known_contract_failures_or_unobserved_na(auxiliary_packet,acceptance_public_root,task_id):
    from tests.support.agent_acceptance.quality_memory_aux import exercise_memory,score
    from tests.support.agent_acceptance.quality_runtime import bound_task
    asyncio.run(exercise_memory(auxiliary_packet,task_id,'deepseek',root=acceptance_public_root))
    _,visible,_=bound_task('quality.'+task_id.lower(),acceptance_public_root)
    missing=score(visible,acceptance_public_root,[],[])['critical_contracts']
    assert missing['K1']==missing['K2']==missing['K4']=='unknown'


def test_tuning_hygrometer_runs_one_source_bound_review_without_recalling_another_chat(auxiliary_packet, acceptance_public_root):
    from tests.support.agent_acceptance.quality_memory_aux import exercise_memory, score
    from tests.support.agent_acceptance.quality_runtime import bound_task, output_standards, required_outputs
    outcome = asyncio.run(exercise_memory(auxiliary_packet, 'QG4.tuning.2', 'deepseek', root=acceptance_public_root))
    assert outcome.product_status == 'passed', outcome.facts
    facts = outcome.facts['quality_memory_contract']
    assert facts['memory_reviews'] == 1
    assert facts['review_tools_are_memory_only'] and facts['delivery_preserved'] and facts['review_history_separate']
    assert facts['current_texts'] == ['本次湿度计换电池后恢复读数，只在这一次观察中成立。']
    assert 'memory_review' in facts['request_actors']
    _, visible, _ = bound_task('quality.qg4.tuning.2', acceptance_public_root)
    assert required_outputs(visible) == {'main_answer': 1, 'memory': 2}
    assert all(row['dimensions']['bounded_review']['applicable'] for row in output_standards(visible).values())
    missing = score(visible, acceptance_public_root, [], [])
    assert missing['critical_contracts']['K1'] == missing['critical_contracts']['K4'] == 'unknown'
    assert outcome.facts['real_http_attempts'] == 0 and outcome.semantic_status == 'not_measured'


@pytest.mark.parametrize('task_id', ['QG4.formal.4', 'QG4.formal.10', 'QG4.tuning.2'])
def test_auxiliary_scores_actual_delivery_independent_of_fixed_wording(auxiliary_packet, acceptance_public_root, monkeypatch, task_id):
    from tests.support.agent_acceptance import quality_memory_aux
    from tests.support.agent_acceptance.quality_runtime import bound_task

    original_replies = quality_memory_aux.replies

    def alternate_delivery(visible, root, protocol, *, phase='work'):
        old_answer = quality_memory_aux.delivery(visible)
        return [replace(reply, text='Outcome observed. ' + old_answer) if reply.text == old_answer else reply
            for reply in original_replies(visible, root, protocol, phase=phase)]

    monkeypatch.setattr(quality_memory_aux, 'replies', alternate_delivery)
    outcome = asyncio.run(quality_memory_aux.exercise_memory(auxiliary_packet, task_id, 'deepseek', root=acceptance_public_root))
    assert outcome.product_status == 'passed' and outcome.semantic_status == 'not_measured'
    assert outcome.critical_contracts['K4'] == 'unknown'
    contract = outcome.facts['quality_memory_contract']
    record = json.loads(outcome.evidence['conversation'].read_bytes())
    exchange = json.loads(outcome.evidence['exchange'].read_bytes())
    _, visible, _ = bound_task('quality.' + task_id.lower(), acceptance_public_root)
    answer = record['turns'][0]['answer']
    assert answer.startswith('Outcome observed. ')
    assert contract['delivery_preserved'] is True
    assert contract['delivered_report_sha256'] == sha256(answer.encode()).hexdigest()

    altered_record = deepcopy(record)
    altered_record['turns'][0]['answer'] = 'An unrelated replacement report.'
    original_changed = quality_memory_aux.score(visible, acceptance_public_root, [altered_record], [exchange])
    assert not original_changed['passed'] and original_changed['critical_contracts']['K4'] == 'failed'

    if task_id == 'QG4.formal.10':
        assert contract['actual_save_failure'] and contract['fact_original_unchanged']
        return

    assert contract['review_report_bound'] is True
    incomplete_exchange = deepcopy(exchange)
    last_review = next(item for item in reversed(exchange['sdk_inputs']) if item['request_identity']['actor'] == 'memory_review')
    incomplete_exchange['sdk_inputs'] = [item for item in incomplete_exchange['sdk_inputs'] if item != last_review]
    missing_review_input = quality_memory_aux.score(visible, acceptance_public_root, [record], [incomplete_exchange])
    assert not missing_review_input['passed'] and missing_review_input['critical_contracts']['K4'] == 'unknown'
    assert missing_review_input['quality_memory_contract']['review_report_bound'] is None

    for item in exchange['sdk_inputs']:
        if item['request_identity']['actor'] == 'memory_review':
            for message in item['messages']:
                if message['kind'] == 'response':
                    for part in message['parts']:
                        if part['part_kind'] == 'text' and part['content'] == answer:
                            part['content'] = part['content'].removeprefix('Outcome observed. ')
    review_mismatch = quality_memory_aux.score(visible, acceptance_public_root, [record], [exchange])
    assert not review_mismatch['passed'] and review_mismatch['critical_contracts']['K4'] == 'failed'
    assert review_mismatch['quality_memory_contract']['review_source_bound'] is True
