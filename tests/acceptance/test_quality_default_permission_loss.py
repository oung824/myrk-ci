"""New chats retain frozen permissions across two formal executions."""
from hashlib import sha256
import json
import shutil
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from tests.support.agent_acceptance import AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, builtin_registry, prepare_live
from tests.support.agent_acceptance.formal import environment
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.preparation import LiveExecutionRequest
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
from tests.support.agent_acceptance.quality_packet import PACKETS, read_quality_packet


@pytest.mark.acceptance_desktop
def test_new_chats_keep_frozen_permissions_and_execute_both_cases(prepared_root,acceptance_public_root):
    from myrk.project_paths import project_root
    packet_root=SOURCE/PACKETS/('new-chat-permissions-'+uuid4().hex)
    parent=FrozenParentBudget(parent_id='synthetic-new-chat-'+prepared_root.name,accounting_root=prepared_root,request_cap=48,
        phase_microusd={'screening':0,'quality':707808,'judge':0},reserve_microusd=0,profile='synthetic')
    index=project_root(SOURCE)/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    try:
        reference=build_implementation_quality_packet(packet_root)
        packet=read_quality_packet(reference)
        rows=[]
        for order,identity in enumerate(('QG4.formal.2','QG4.formal.3'),1):
            task=next(item for item in packet.tasks if item.task_id==identity)
            rows.append(FrozenExecution(execution_id='synthetic-new-chat:'+identity,case_id='quality.'+identity.lower(),protocol='deepseek',scenario_id=identity,
                material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
                startpoint='synthetic-new-chat:'+identity,order=order,stage='quality',request_cap=24,cost_cap_microusd=353904))
        plan=AcceptancePlan(executions=tuple(rows),parent_budget=parent,quality_packet=reference,mode='prepare-live')
        registry=builtin_registry(quality_packet=reference)
        bundle=prepare_live(plan,root=prepared_root,registry=registry, evaluator_isolation_directories=())
        grant=LiveAuthorization(authorization_id='synthetic-new-chat',binding=bundle.binding,total_limit=48,
            case_limits={row.execution_id:24 for row in rows},total_microusd=707808,
            case_microusd={row.execution_id:353904 for row in rows},transport='mock',parent_budget_id=parent.parent_id)
        root=acceptance_public_root
        root.mkdir(parents=True)
        archive_path=root/'executed-source-snapshot.zip'
        with ZipFile(archive_path,'x',ZIP_DEFLATED) as archive:
            for name,expected in bundle.files.items():
                body=(prepared_root/name).read_bytes()
                assert sha256(body).hexdigest()==expected
                archive.writestr(name,body)
            archive.writestr('prepared.json',(prepared_root/'prepared.json').read_bytes())
        source_index={'binding':bundle.binding,'archive_sha256':sha256(archive_path.read_bytes()).hexdigest(),'members':len(bundle.files)+1,
            'purpose':'two planned synthetic controls; no independent quality sample increment'}
        (root/'executed-source-snapshot-index.json').write_text(json.dumps(source_index,sort_keys=True),encoding='utf-8')
        request=root/'execution-request.json'
        request.write_text(LiveExecutionRequest(run_root=prepared_root,grant=grant).model_dump_json(),encoding='utf-8')
        process=subprocess.run([sys.executable,'-B','-m','tests.support.agent_acceptance.formal_cli','execute',str(request),'--output',str(root/'execution')],
            cwd=prepared_root,env={**environment(root/'controller-environment'),'PYTHONPATH':str(prepared_root)},capture_output=True)
        (root/'controller.stdout').write_bytes(process.stdout)
        (root/'controller.stderr').write_bytes(process.stderr)
        assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
        result=json.loads((root/'execution/result.json').read_bytes())
        assert result['planned_denominator']==2 and result['planned_executions']==[row.model_dump(mode='json') for row in rows]
        executions = {row.execution_id for row in rows}
        ledger_body = (prepared_root / 'ledger.json').read_bytes()
        (root / 'final-ledger.original.json').write_bytes(ledger_body)
        ledger = json.loads(ledger_body)
        assert ledger['stopped'] is False and ledger['stop_reason'] is None
        assert ledger['binding'] == bundle.binding and ledger['authorization']['authorization_id'] == grant.authorization_id
        assert set(ledger['execution_results']) == executions
        attempts = ledger['attempts']
        assert all(attempt['execution_id'] in executions and attempt['authorization_id'] == grant.authorization_id
                   for attempt in attempts)

        for row, case in zip(rows, result['cases'], strict=True):
            assert case['execution_id'] == row.execution_id
            facts = case['facts']
            assert case['product_status'] == case['tool_status'] == 'passed', facts
            assert case['critical_contracts']['K1'] == 'passed' and case['semantic_status'] == 'not_measured'
            permissions = facts['quality_task_permissions']
            assert permissions['status'] == 'passed' and permissions['evaluated_task_ids'], permissions
            contract = facts['quality_memory_contract']
            assert contract['frozen_tool_denials_preserved'] and contract['remembered_in_fresh_chat']
            assert contract['source_originals_unchanged'] and contract['original_history_preserved']

            transport = json.loads((root / 'execution' / case['evidence']['work/receiver/transport.json']).read_bytes())
            owned_attempts = [attempt for attempt in attempts if attempt['execution_id'] == row.execution_id]
            assert all(attempt['case_id'] == row.case_id and attempt['request_sha256'] for attempt in owned_attempts)
            assert len(owned_attempts) == transport['mock_http_attempts'] == facts['mock_http_attempts'] > 0
            assert transport['real_http_attempts'] == facts['real_http_attempts'] == 0
            assert not transport['control_error_history'] and facts['processes_complete'] is True
    finally:
        if index.exists():index.unlink()
        assert packet_root.parent==SOURCE/PACKETS
        if packet_root.exists():shutil.rmtree(packet_root)
