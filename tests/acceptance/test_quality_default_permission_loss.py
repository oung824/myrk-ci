"""A real new-chat permission loss remains a product failure and stops its parent."""
from hashlib import sha256
import json
from pathlib import Path
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
def test_known_new_chat_permission_loss_retains_failed_product_and_unexecuted_remainder(prepared_root,acceptance_public_root):
    from myrk.project_paths import project_root
    packet_root=SOURCE/PACKETS/('known-new-chat-permissions-'+uuid4().hex)
    parent=FrozenParentBudget(parent_id='synthetic-known-new-chat-'+prepared_root.name,accounting_root=prepared_root,request_cap=48,
        phase_microusd={'screening':0,'quality':707808,'judge':0},reserve_microusd=0,profile='synthetic')
    index=project_root(SOURCE)/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    try:
        reference=build_implementation_quality_packet(packet_root)
        packet=read_quality_packet(reference)
        rows=[]
        for order,identity in enumerate(('QG4.formal.2','QG4.formal.3'),1):
            task=next(item for item in packet.tasks if item.task_id==identity)
            rows.append(FrozenExecution(execution_id='synthetic-known-gap:'+identity,case_id='quality.'+identity.lower(),protocol='deepseek',scenario_id=identity,
                material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
                startpoint='synthetic-known-gap:'+identity,order=order,stage='quality',request_cap=24,cost_cap_microusd=353904))
        plan=AcceptancePlan(executions=tuple(rows),parent_budget=parent,quality_packet=reference,mode='prepare-live')
        registry=builtin_registry(quality_packet=reference)
        bundle=prepare_live(plan,root=prepared_root,registry=registry)
        grant=LiveAuthorization(authorization_id='synthetic-known-new-chat',binding=bundle.binding,total_limit=48,
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
        first,remaining=result['cases']
        assert first['execution_id']==rows[0].execution_id
        assert first['product_status']=='failed' and first['tool_status']=='passed',first['facts']
        assert first['critical_contracts']['K1']=='failed' and first['semantic_status']=='not_measured'
        proof=first['facts']['known_default_permission_loss']
        assert proof['kind']=='public_new_default_permissions_lost' and proof['ledger_stopped'] is True
        assert proof['product_status']=='failed' and proof['actual_terminal_http_before']>0 and proof['actual_terminal_http_after']==proof['actual_terminal_http_before']
        contract=first['facts']['quality_memory_contract']
        assert contract['known_product_gap']=='temporary_connection_resets_new_chat_permissions'
        assert contract['source_originals_unchanged'] and contract['original_history_preserved']
        assert contract['new_chat_http_attempts']==0
        assert contract['dependent_steps_not_started'] and contract['refused_before_terminal_http']
        assert remaining['execution_id']==rows[1].execution_id and remaining['product_status']=='not_executed'
        assert remaining['evidence']=={}
        ledger_body=(prepared_root/'ledger.json').read_bytes()
        (root/'final-ledger.original.json').write_bytes(ledger_body)
        ledger=json.loads(ledger_body)
        assert ledger['stopped'] is True and ledger['authorization']['authorization_id']==grant.authorization_id
        assert ledger['binding']==bundle.binding and proof['guard_error'] in ledger['stop_reason']
        assert rows[0].execution_id in ledger['execution_results'] and rows[1].execution_id not in ledger['execution_results']
        assert len(ledger['attempts'])==proof['actual_terminal_http_before']
        assert all(item['execution_id']==rows[0].execution_id and item['authorization_id']==grant.authorization_id for item in ledger['attempts'])
        processes=[json.loads(path.read_bytes()) for path in (root/'execution').rglob('work/processes.json')]
        assert len(processes)==1 and processes[0]['complete'] is True
        transports=[json.loads(path.read_bytes()) for path in (root/'execution').rglob('receiver/transport.json')]
        assert len(transports)==1 and transports[0]['real_http_attempts']==0
        assert transports[0]['mock_http_attempts']==proof['actual_terminal_http_before']
        assert transports[0]['control_error'] and transports[0]['control_error_history']
        assert not list((root/'execution'/rows[1].case_id).rglob('authorization.json'))
    finally:
        if index.exists():index.unlink()
        assert packet_root.parent==SOURCE/PACKETS
        if packet_root.exists():shutil.rmtree(packet_root)
