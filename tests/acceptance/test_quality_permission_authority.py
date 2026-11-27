"""A public Main mode reset cannot weaken the canonical tool ceiling before send."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from tests.support.agent_acceptance import AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, builtin_registry, freeze_quality, prepare_live
from tests.support.agent_acceptance.formal import environment
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.preparation import LiveExecutionRequest
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
from tests.support.agent_acceptance.quality_packet import PACKETS, QualityPacketRef, read_quality_packet


@pytest.fixture
def mode_reset_packet():
    """Freeze the explicit implementation-only negative input before any execution."""
    identity=uuid4().hex
    parent=SOURCE/PACKETS
    base=parent/('permission-base-'+identity)
    negative=parent/('permission-reset-'+identity)
    try:
        original=read_quality_packet(build_implementation_quality_packet(base))
        tasks=[]
        for task in original.tasks:
            draft=task.model_dump(mode='json')
            for kind in ('input','gold'):
                old=SOURCE/getattr(task,kind+'_path')
                target=negative/kind/old.name
                target.parent.mkdir(parents=True,exist_ok=True)
                body=old.read_bytes()
                if task.task_id=='QG1.tuning.1' and kind=='input':
                    value=json.loads(body)
                    value['parameters']['implementation_negative']='approval_mode_reset'
                    body=json.dumps(value,ensure_ascii=False,sort_keys=True).encode('utf-8')
                target.write_bytes(body)
                draft[kind+'_path']=target
            tasks.append(draft)
        freeze=freeze_quality(tasks,destination=negative/'freeze.json',provenance='synthetic',
            source_version='explicit-implementation-negative-approval-mode-reset',environment=original.environment,
            models=original.models,criteria_version=original.criteria_version,qualification_families=[],intended_use='implementation_regression')
        yield QualityPacketRef(path=freeze,sha256=sha256(freeze.read_bytes()).hexdigest())
    finally:
        for root in (base,negative):
            assert root.parent==SOURCE/PACKETS
            if root.exists():shutil.rmtree(root)


@pytest.mark.acceptance_desktop
def test_actual_main_rejects_mode_reset_before_reservation_or_terminal_http(mode_reset_packet,prepared_root,acceptance_public_root):
    from myrk.permissions import ApprovalSettings
    from myrk.project_paths import project_root

    packet=read_quality_packet(mode_reset_packet)
    task=next(row for row in packet.tasks if row.task_id=='QG1.tuning.1')
    parent=FrozenParentBudget(parent_id='synthetic-quality-permission-'+prepared_root.name,accounting_root=prepared_root,request_cap=4,
        phase_microusd={'screening':0,'quality':58984,'judge':0},reserve_microusd=0,profile='synthetic')
    row=FrozenExecution(execution_id='negative:'+task.task_id,case_id='quality.'+task.task_id.lower(),protocol='deepseek',scenario_id=task.task_id,
        material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
        startpoint='negative:'+task.task_id,order=1,stage='quality',request_cap=4,cost_cap_microusd=58984)
    plan=AcceptancePlan(executions=(row,),parent_budget=parent,quality_packet=mode_reset_packet,mode='prepare-live')
    registry=builtin_registry(quality_packet=mode_reset_packet)
    bundle=prepare_live(plan,root=prepared_root,registry=registry)
    grant=LiveAuthorization(authorization_id='synthetic-quality-permission',binding=bundle.binding,total_limit=4,case_limits={row.execution_id:4},
        total_microusd=58984,case_microusd={row.execution_id:58984},transport='mock',parent_budget_id=parent.parent_id)
    root=acceptance_public_root
    root.mkdir(parents=True)
    source_archive=root/'executed-source-snapshot.zip'
    with ZipFile(source_archive,'x',ZIP_DEFLATED) as archive:
        for name,expected in bundle.files.items():
            body=(prepared_root/name).read_bytes()
            assert sha256(body).hexdigest()==expected
            archive.writestr(name,body)
        archive.writestr('prepared.json',(prepared_root/'prepared.json').read_bytes())
    source_index={'binding':bundle.binding,'archive_sha256':sha256(source_archive.read_bytes()).hexdigest(),
        'members':len(bundle.files)+1,'original_source_root':str(prepared_root),'purpose':'explicit synthetic negative control, not an independent quality sample'}
    (root/'executed-source-snapshot-index.json').write_text(json.dumps(source_index,sort_keys=True),encoding='utf-8')
    marker=SOURCE/'.scratch/retro-agent-live-acceptance-20261116/implementation/t24/permission-authority/freeze-notices'/bundle.binding
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text(json.dumps(source_index,sort_keys=True),encoding='utf-8')
    request=root/'execution-request.json'
    request.write_text(LiveExecutionRequest(run_root=prepared_root,grant=grant).model_dump_json(),encoding='utf-8')
    try:
        process=subprocess.run([sys.executable,'-B','-m','tests.support.agent_acceptance.formal_cli','execute',str(request),'--output',str(root/'formal-result')],
            cwd=prepared_root,env={**environment(root/'controller-environment'),'PYTHONPATH':str(prepared_root)},capture_output=True)
        (root/'controller.stdout').write_bytes(process.stdout)
        (root/'controller.stderr').write_bytes(process.stderr)
        transports=[json.loads(path.read_bytes()) for path in (root/'formal-result').rglob('receiver/transport.json')]
        assert len(transports)==1,'The actual receiver must start and preserve its pre-send control observation.'
        observation=transports[0]
        assert observation['real_http_attempts']==0
        assert observation['mock_http_attempts']==0,'Mode reset reached a terminal HTTP request before canonical permissions were checked.'
        assert process.returncode!=0
        assert observation['control_error_history']
        assert 'Quality actual product Task permissions changed' in observation['control_error_history'][0]
        ledger=json.loads((prepared_root/'ledger.json').read_bytes())
        (root/'final-ledger.original.json').write_bytes((prepared_root/'ledger.json').read_bytes())
        assert ledger['attempts']==[] and ledger['stopped'] is True
        exchanges=[json.loads(path.read_bytes()) for path in (root/'formal-result').rglob('receiver/exchange.json')]
        assert len(exchanges)==1 and exchanges[0]['exchanges']==[] and len(exchanges[0]['sdk_inputs'])==1
        sdk_identity=exchanges[0]['sdk_inputs'][0]['request_identity']
        assert sdk_identity['actor']=='main'
        scenarios=[json.loads(path.read_bytes()) for path in (root/'formal-result').rglob('work/scenario.json')]
        assert len(scenarios)==1 and scenarios[0]['implementation_negative']=='approval_mode_reset'
        expected=ApprovalSettings.model_validate(scenarios[0]['approval'])
        assert expected.access.denied_paths and expected.access.read_only is True
        records=[]
        for path in (root/'formal-result').rglob('*.json'):
            value=json.loads(path.read_bytes())
            if isinstance(value,dict) and 'turns' in value and 'messages' in value:
                records.extend(turn for turn in value['turns'] if turn.get('task') and any(attempt['number']==sdk_identity['number'] and attempt['actor']==sdk_identity['actor'] for attempt in turn.get('request_attempts',[])))
        assert records
        actual=ApprovalSettings.model_validate(records[0]['task']['approval'])
        assert actual.access.denied_paths==[] and actual.access.read_only is False
        assert actual!=expected
        reports=[json.loads(path.read_bytes()) for path in (root/'formal-result').rglob('work/processes.json')]
        assert len(reports)==1 and reports[0]['complete'] is True
        (root/'negative-control-verdict.json').write_text(json.dumps({'permission_mismatch_observed':True,'reserved_requests':0,
            'mock_http_attempts':0,'real_http_attempts':0,'owned_processes_complete':True,'semantic_status':'not_measured',
            'independent_quality_sample_increment':0,'actual_human_review':'not_performed'},sort_keys=True),encoding='utf-8')
    finally:
        index=project_root(SOURCE)/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
        if index.exists():index.unlink()
