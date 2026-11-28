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

from myrk.project_paths import project_root
from tests.acceptance.test_candidate_refreeze import approve_candidate
from tests.acceptance.test_quality_answer_isolation import synthetic_quality_packet
from tests.support.agent_acceptance import AcceptancePlan, FrozenExecution, FrozenParentBudget, LiveAuthorization, LiveGate, builtin_registry, freeze_quality, prepare_live
from tests.support.agent_acceptance.core import CaseContext, CaseOutcome, SharedControlError
from tests.support.agent_acceptance.formal import FormalAuthority, SourceBinding, build, environment, provider_config, verify_authority
from tests.support.agent_acceptance.formal_scenarios import scenario_config
from tests.support.agent_acceptance.frozen import SOURCE
from tests.support.agent_acceptance.preparation import LiveExecutionRequest
from tests.support.agent_acceptance.preparation_models import bundle_fingerprint
from tests.support.agent_acceptance.preparation_snapshot import verify_prepared
from tests.support.agent_acceptance.quality_materials import build_implementation_quality_packet
from tests.support.agent_acceptance.quality_packet import PACKETS, QualityPacketRef, read_quality_packet
from tests.support.agent_acceptance.quality_runtime import bind_execution, manifest


@pytest.fixture
def isolation_plan(synthetic_quality_packet, prepared_root):
    packet = read_quality_packet(synthetic_quality_packet)
    rows = []
    for order, (execution_id, task_id) in enumerate((("tuning", "QG2.tuning.1"), ("formal", "QG2.formal.3")), 1):
        task = next(item for item in packet.tasks if item.task_id == task_id)
        rows.append(FrozenExecution(
            execution_id=execution_id, case_id=f"quality.{task_id.lower()}", protocol="deepseek", scenario_id=task_id,
            material_family_id=task.material_family_id, material_variant="baseline", material_path=task.input_path,
            material_sha256=packet.files[task.input_path.as_posix()], startpoint=execution_id, order=order, stage="quality",
            request_cap=4, cost_cap_microusd=58984,
        ))
    parent = FrozenParentBudget(parent_id=f"synthetic-evaluator-isolation-{prepared_root.name}", accounting_root=prepared_root,
        request_cap=8, phase_microusd={"screening": 0, "quality": 117968, "judge": 0}, reserve_microusd=0, profile="synthetic")
    plan = AcceptancePlan(executions=tuple(rows), parent_budget=parent, quality_packet=synthetic_quality_packet, mode="prepare-live")
    try:
        yield plan, builtin_registry(quality_packet=synthetic_quality_packet)
    finally:
        index = project_root(SOURCE) / ".scratch/acceptance-parent-budgets" / (sha256(parent.parent_id.encode()).hexdigest() + ".json")
        index.unlink(missing_ok=True)


def isolation_grant(bundle):
    parent = bundle.plan.parent_budget
    return LiveAuthorization(authorization_id="synthetic-evaluator-isolation", binding=bundle.binding,
        total_limit=parent.request_cap, case_limits={row.execution_id: row.request_cap for row in bundle.plan.executions},
        total_microusd=parent.total_microusd, case_microusd={row.execution_id: row.cost_cap_microusd for row in bundle.plan.executions},
        transport="mock", parent_budget_id=parent.parent_id)


def test_frozen_isolation_directory_must_stay_in_project(isolation_plan, prepared_root):
    plan, registry = isolation_plan
    with pytest.raises(SharedControlError, match="evaluator isolation directories"):
        prepare_live(plan, root=prepared_root, registry=registry, evaluator_isolation_directories=("../outside",))
    assert not prepared_root.exists()


@pytest.mark.parametrize("stopped", [False, True], ids=["eligible", "stopped"])
def test_legacy_quality_freeze_is_read_only_until_eligible_reviewed_refreeze(
    isolation_plan, prepared_root, tmp_path, stopped,
):
    plan, registry = isolation_plan
    original = prepare_live(plan, root=prepared_root, registry=registry)
    original_path = prepared_root / "prepared.json"
    original_bytes = original_path.read_bytes()
    gate = LiveGate(prepared_root, registry, isolation_grant(original))
    tuning, formal = plan.executions
    assert verify_prepared(prepared_root, registry=registry).binding == original.binding
    with pytest.raises(SharedControlError, match="explicit evaluator isolation directory list"):
        gate.for_execution("formal").reserve(case_id=formal.case_id, protocol=formal.protocol, actor="main", purpose="work")
    assert gate.accounting_report()["reserved_requests"] == gate.accounting_report()["actual_http"] == 0
    gate.for_execution("tuning").finish_execution(CaseOutcome("not_executed", {"reason": "Synthetic tuning closed."},
        case_id=tuning.case_id, protocol=tuning.protocol, execution_id="tuning"))
    if stopped:
        gate.stop(reason="Synthetic shared stop before attempting refreeze.")
    relative = (tmp_path / "new-evaluator-drafts").relative_to(project_root(SOURCE)).as_posix()
    candidate = gate.propose_candidate(evaluator_isolation_directories=(relative,))
    for row in plan.executions:
        assert candidate.execution[row.case_id]["evaluator_isolation_directories"] == [relative]
        altered = {**candidate.execution[row.case_id], "evaluator_isolation_directories": []}
        substituted = candidate.model_copy(update={"execution": {**candidate.execution, row.case_id: altered}})
        assert bundle_fingerprint(substituted) != candidate.binding
    if stopped:
        with pytest.raises(SharedControlError, match="stopped"):
            approve_candidate(gate, candidate)
        assert gate.accounting_report()["stopped"] is True
    else:
        current = approve_candidate(gate, candidate)
        current.for_execution("formal").validate(case_id=formal.case_id, protocol=formal.protocol)
        report = current.accounting_report()
        original_report = gate.accounting_report()
        for name in ("parent_budget_id", "request_limit", "cost_limit_microusd", "reserved_requests", "actual_http"):
            assert report[name] == original_report[name]
        with pytest.raises(SharedControlError, match="evaluator isolation"):
            current.propose_candidate(evaluator_isolation_directories=())
    assert verify_prepared(prepared_root, registry=registry, binding=original.binding).binding == original.binding
    assert original_path.read_bytes() == original_bytes


@pytest.mark.parametrize("boundary", ["declared-evaluator", "original-evidence"])
def test_formal_authority_rejects_removed_directory_and_denial(
    isolation_plan, prepared_root, acceptance_public_root, tmp_path, monkeypatch, boundary,
):
    plan, registry = isolation_plan
    directory = tmp_path / "new-evaluator-drafts"
    directory.mkdir()
    relative = directory.relative_to(project_root(SOURCE)).as_posix()
    bundle = prepare_live(plan, root=prepared_root, registry=registry, evaluator_isolation_directories=(relative,))
    grant = isolation_grant(bundle)
    formal = plan.executions[1]
    gate = LiveGate(prepared_root, registry, grant).for_execution("formal")
    runtime = prepared_root / ".scratch" / f"execution-{uuid4().hex}"
    runtime.mkdir(parents=True)
    build_root = acceptance_public_root / "build"
    compiled = build(build_root)
    bind_execution(CaseContext(acceptance_public_root, formal.protocol, "synthetic-authority", formal.case_id,
        live_gate=gate, execution=formal), runtime)
    scenario = manifest(formal.case_id, runtime, formal.protocol)
    phase = runtime / "work"
    config = scenario_config(formal.case_id, provider_config(formal.protocol))
    authority = FormalAuthority(run_root=prepared_root, grant=grant, case_id=formal.case_id, protocol=formal.protocol,
        case_nonce="synthetic-isolation", source_hashes=[
            SourceBinding(path=prepared_root / name, sha256=checksum) for name, checksum in bundle.files.items()
            if Path(name).suffix in {".py", ".mjs", ".js", ".cjs", ".ps1", ".ts"}
        ], runtime=phase, main=build_root / "project/desktop/out/main/index.js", compiled_sources=compiled,
        run_id="synthetic-authority", provider_config=config, scenario_root=runtime, execution_id=formal.execution_id)
    authority_path = phase / "authorization.json"
    authority_path.write_text(authority.model_dump_json(), encoding="utf-8")
    scenario_path = phase / "scenario.json"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.setenv("MYRK_ACCEPTANCE_EVIDENCE_ROOT", str(acceptance_public_root))
    verify_authority(authority_path)

    binding_path = runtime / "quality-task.json"
    bound = json.loads(binding_path.read_bytes())
    if boundary == "declared-evaluator":
        assert bound.pop("evaluator_isolation_directories") == [relative]
        removed = str(directory)
    else:
        removed = str(acceptance_public_root)
        assert bound.pop("evidence_root") == removed
    bound["denied_paths"].remove(removed)
    binding_path.write_text(json.dumps(bound), encoding="utf-8")
    scenario["approval"]["access"]["denied_paths"].remove(removed)
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    mismatch = "evaluator isolation" if boundary == "declared-evaluator" else "evidence isolation"
    with pytest.raises(SharedControlError, match=mismatch):
        verify_authority(authority_path)
    report = gate.accounting_report()
    assert report["reserved_requests"] == report["actual_http"] == 0
    shutil.rmtree(build_root)


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

    packet=read_quality_packet(mode_reset_packet)
    task=next(row for row in packet.tasks if row.task_id=='QG1.tuning.1')
    parent=FrozenParentBudget(parent_id='synthetic-quality-permission-'+prepared_root.name,accounting_root=prepared_root,request_cap=4,
        phase_microusd={'screening':0,'quality':58984,'judge':0},reserve_microusd=0,profile='synthetic')
    row=FrozenExecution(execution_id='negative:'+task.task_id,case_id='quality.'+task.task_id.lower(),protocol='deepseek',scenario_id=task.task_id,
        material_family_id=task.material_family_id,material_variant='baseline',material_path=task.input_path,material_sha256=packet.files[task.input_path.as_posix()],
        startpoint='negative:'+task.task_id,order=1,stage='quality',request_cap=4,cost_cap_microusd=58984)
    plan=AcceptancePlan(executions=(row,),parent_budget=parent,quality_packet=mode_reset_packet,mode='prepare-live')
    registry=builtin_registry(quality_packet=mode_reset_packet)
    bundle=prepare_live(plan,root=prepared_root,registry=registry,evaluator_isolation_directories=())
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
        assert str(project_root(SOURCE) / ".git") in expected.access.denied_paths
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
