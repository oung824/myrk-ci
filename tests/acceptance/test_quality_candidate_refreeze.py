"""Quality refreezes keep their original ledger and a usable private data namespace."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from tests.support.agent_acceptance import AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, builtin_registry, prepare_live
from tests.support.agent_acceptance.core import SharedControlError
from tests.support.agent_acceptance.formal import environment
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.preparation import LiveExecutionRequest, bound_files, bundle_material_root, read_prepared_snapshot
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
from tests.support.agent_acceptance.quality_packet import PACKETS, QualityPacketRef, read_quality_packet


def candidate_probe(request_path: Path, output: Path) -> None:
    """Use the same public prepare/stage/review path from an owned SOURCE copy."""
    from myrk.project_paths import project_root
    from tests.acceptance.test_candidate_refreeze import approve_candidate, PROMPT
    value=json.loads(request_path.read_bytes())
    root=Path(value['run_root'])
    plan=AcceptancePlan.model_validate(value['plan'])
    registry=builtin_registry(quality_packet=plan.quality_packet)
    evaluator_directories=value['evaluator_isolation_directories']
    bundle=prepare_live(plan,root=root,registry=registry,evaluator_isolation_directories=tuple(evaluator_directories))
    assert all(bundle.execution[row.case_id]['evaluator_isolation_directories']==evaluator_directories for row in plan.executions)
    grant=LiveAuthorization(authorization_id='synthetic-quality-genesis',binding=bundle.binding,total_limit=16,
        case_limits={'tuning':8,'formal':8},total_microusd=235936,case_microusd={'tuning':117968,'formal':117968},
        transport='mock',parent_budget_id=plan.parent_budget.parent_id)
    gate=LiveGate(root,registry,grant)
    index=gate.parent_binding_path(plan.parent_budget.parent_id)
    prompt=Path(PROMPT)
    original_prompt=prompt.read_bytes()
    original_genesis=(root/'prepared.json').read_bytes()
    def execute(current,name):
        directory=output/name
        directory.mkdir(parents=True)
        path=directory/'execution-request.json'
        path.write_text(LiveExecutionRequest(run_root=root,grant=current.authorization,execution_ids=(name,)).model_dump_json(),encoding='utf-8')
        code=bundle_material_root(read_prepared_snapshot(root,binding=current.authorization.binding))
        process=subprocess.run([sys.executable,'-B','-m','tests.support.agent_acceptance.formal_cli','execute',str(path),'--output',str(directory/'execution')],
            cwd=code,env={**environment(directory/'controller-environment'),'PYTHONPATH':str(code)},capture_output=True)
        (directory/'controller.stdout').write_bytes(process.stdout)
        (directory/'controller.stderr').write_bytes(process.stderr)
        if process.returncode:
            raise RuntimeError(process.stderr.decode('utf-8',errors='replace'))
        result=json.loads((directory/'execution/result.json').read_bytes())
        return next(item for item in result['cases'] if item['execution_id']==name),directory/'execution'
    try:
        tuning,_=execute(gate,'tuning')
        assert tuning['tool_status']==tuning['product_status']=='passed',tuning['facts']
        before=gate.accounting_report()
        prompt.write_bytes(original_prompt+b'\nExplicit private quality prompt revision after actual Main tuning.\n')
        try:
            gate.for_execution('formal').validate(case_id=plan.executions[1].case_id,protocol='deepseek')
        except SharedControlError:
            old_rejected=True
        else:
            old_rejected=False
        assert old_rejected and gate.accounting_report()['actual_http']==before['actual_http']
        candidate=gate.propose_candidate()
        assert gate.accounting_report()['actual_http']==before['actual_http']
        current=approve_candidate(gate,candidate)
        frozen_candidate=read_prepared_snapshot(root,binding=current.authorization.binding)
        assert all(frozen_candidate.execution[row.case_id]['evaluator_isolation_directories']==evaluator_directories
            for row in plan.executions)
        formal,result_root=execute(current,'formal')
        after=current.accounting_report()
        authorities=[json.loads((Path(path) if Path(path).is_absolute() else result_root/path).read_bytes())
            for path in formal['evidence'].values() if Path(path).name=='authorization.json']
        original_manifest=Path(authorities[0]['scenario_root'])/'quality-task.json'
        stored_manifest=formal['facts']['evidence_relocation'].get(str(original_manifest),original_manifest.name)
        manifest_path=result_root/Path(formal['evidence'][stored_manifest])
        denied_paths=json.loads(manifest_path.read_bytes())['denied_paths']
        assert all(str(project_root(SOURCE)/path) in denied_paths for path in evaluator_directories)
        from tests.support.agent_acceptance.formal import verify_authority
        invalid=dict(authorities[0])
        invalid['scenario_root']=str(root/'.scratch'/('execution-'+uuid4().hex))
        invalid_path=Path(authorities[0]['runtime'])/'invalid-paired-root.json'
        invalid_path.write_text(json.dumps(invalid),encoding='utf-8')
        (output/'invalid-paired-root-authority.original.json').write_bytes(invalid_path.read_bytes())
        before_invalid=(root/'ledger.json').read_bytes()
        try:
            verify_authority(invalid_path)
        except SharedControlError as error:
            invalid_pair_rejected='Formal scenario material or root changed' in str(error)
        else:
            invalid_pair_rejected=False
        assert invalid_pair_rejected and (root/'ledger.json').read_bytes()==before_invalid
        (output/'final-ledger.original.json').write_bytes((root/'ledger.json').read_bytes())
        (output/'probe-report.json').write_text(json.dumps({'tuning':tuning,'formal':formal,'before':before,'after':after,
            'old_source_rejected':old_rejected,'invalid_pair_rejected':invalid_pair_rejected,'genesis_unchanged':(root/'prepared.json').read_bytes()==original_genesis,
            'accounting_root':str(root),'candidate_root':str(bundle_material_root(candidate)),'authorities':authorities},ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        prompt.write_bytes(original_prompt)
        if index.exists():index.unlink()


@pytest.mark.acceptance_desktop
def test_quality_candidate_refreeze_reads_legal_inputs_on_the_original_parent(prepared_root,acceptance_public_root):
    from myrk.project_paths import project_root
    identity=uuid4().hex
    packet_root=SOURCE/PACKETS/('candidate-quality-'+identity)
    project=project_root(SOURCE)
    source_parent=project/'.scratch/qsrc'
    controller=source_parent/identity
    evaluator_directory=controller/'evaluator-review-copy'
    output=acceptance_public_root
    output.mkdir(parents=True)
    try:
        reference=build_implementation_quality_packet(packet_root)
        packet=read_quality_packet(reference)
        rows=[]
        for order,(name,task_id) in enumerate((('tuning','QG1.tuning.1'),('formal','QG1.formal.2')),1):
            task=next(item for item in packet.tasks if item.task_id==task_id)
            rows.append(FrozenExecution(execution_id=name,case_id='quality.'+task_id.lower(),protocol='deepseek',scenario_id=task_id,
                material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
                startpoint=name,order=order,stage='quality',request_cap=8,cost_cap_microusd=117968))
        parent=FrozenParentBudget(parent_id='synthetic-quality-refreeze-'+prepared_root.name,accounting_root=prepared_root,request_cap=16,
            phase_microusd={'screening':0,'quality':235936,'judge':0},reserve_microusd=0,profile='synthetic')
        plan=AcceptancePlan(executions=tuple(rows),parent_budget=parent,quality_packet=reference,mode='prepare-live')
        registry=builtin_registry(quality_packet=reference)
        files=bound_files(plan,registry)
        controller.mkdir(parents=True,exist_ok=False)
        (controller/'owner.json').write_text(json.dumps({'owner':'synthetic-quality-source-controller','identity':identity,'authority_created':False}),encoding='utf-8')
        evaluator_directory.mkdir()
        with ZipFile(output/'controller-source-originals.zip','x',ZIP_DEFLATED) as archive:
            for name,expected in files.items():
                body=(SOURCE/name).read_bytes()
                assert sha256(body).hexdigest()==expected
                target=controller/name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(body)
                archive.writestr(name,body)
        request=output/'probe-request.json'
        request.write_text(json.dumps({'run_root':str(prepared_root),'plan':plan.model_dump(mode='json'),
            'evaluator_isolation_directories':[evaluator_directory.relative_to(project).as_posix()]}),encoding='utf-8')
        process=subprocess.run([sys.executable,'-B','-m','tests.acceptance.test_quality_candidate_refreeze',str(request),str(output)],
            cwd=controller,env={**environment(output/'source-controller-environment'),'PYTHONPATH':str(controller)},capture_output=True)
        (output/'probe.stdout').write_bytes(process.stdout)
        (output/'probe.stderr').write_bytes(process.stderr)
        assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
        report=json.loads((output/'probe-report.json').read_bytes())
        assert report['old_source_rejected'] and report['genesis_unchanged'] and report['invalid_pair_rejected']
        assert report['tuning']['product_status']=='passed'
        assert report['formal']['tool_status']==report['formal']['product_status']=='passed',report['formal']['facts']
        native=report['formal']['facts']['quality_native']
        assert sorted(native['actual_sources_read'])==['bulletin.txt','ticket.txt']
        assert native['gold_not_in_sdk'] and native['source_text_in_sdk'] and native['source_unchanged']
        assert report['after']['attempts'][:len(report['before']['attempts'])]==report['before']['attempts']
        assert report['after']['actual_http']>report['before']['actual_http']>0
        assert report['after']['binding']==report['before']['binding']
        candidate=Path(report['candidate_root'])
        assert report['authorities']
        for authority in report['authorities']:
            data=Path(authority['scenario_root'])
            control=Path(authority['runtime'])
            assert data.is_relative_to(prepared_root/'.scratch') and not data.is_relative_to(candidate)
            assert control.is_relative_to(candidate/'.scratch') and control.parent.name==data.name
            assert authority['run_root']==str(prepared_root)
        assert report['formal']['facts']['real_http_attempts']==0 and report['formal']['semantic_status']=='not_measured'
    finally:
        if controller.exists():
            assert controller.resolve().parent==source_parent.resolve() and controller.name==identity
            assert json.loads((controller/'owner.json').read_bytes())['identity']==identity
            shutil.rmtree(controller)
        assert packet_root.parent==SOURCE/PACKETS
        if packet_root.exists():shutil.rmtree(packet_root)


if __name__=='__main__':
    candidate_probe(Path(sys.argv[1]),Path(sys.argv[2]))
