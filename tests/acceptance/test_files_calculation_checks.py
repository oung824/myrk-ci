"""File/check contracts observed at the shared public entry and native transport."""

import asyncio
import json
import sys

import pytest

from hashlib import sha256
from pathlib import Path

from myrk.conversation import CheckCommand, Conversation, Task, TaskScope
from tests.support.agent_acceptance import AcceptanceCase, AcceptancePlan, CaseRegistry, Reply, run_plan
from tests.support.agent_acceptance.actions import record_outcome


def test_native_check_receipt_binds_executed_command_input_version_and_output(acceptance_public_root):
    async def checked(context):
        folder = context.root / "materials"
        folder.mkdir()
        source = folder / "source.txt"
        source.write_bytes(b"independently-fixed-source-6193\r\n")
        check = folder / "check.py"
        check.write_text("from pathlib import Path\nimport sys\nassert Path(sys.argv[1]).read_bytes()==b'independently-fixed-source-6193\\r\\n'\nprint('checked-exact-version-6193')\n", encoding="utf-8")
        argv = [sys.executable, "-B", str(check), str(source)]
        session = context.session([Reply(tool="run_check", arguments={"name": "Exact version"}), Reply(text="The actual check passed.")])
        conversation = Conversation(context.root / "conversation.json")
        task = Task(goal="Run the registered exact version check.", provider_config=session.config,
            scope=TaskScope(root=str(folder), allowed_actions=["read", "check"], provider="deepseek", recipients=["deepseek"],
                checks=[CheckCommand(name="Exact version", argv=argv)]))
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
        return record_outcome(context, session, conversation, task, turn, turn.status == "completed", evidence={"source": source, "check_script": check})

    registry = CaseRegistry()
    registry.register(AcceptanceCase("files.check-version", "process-effects", ("deepseek",), checked, owner_ticket="06"))
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["files.check-version"]), root=acceptance_public_root, registry=registry)).cases[0]
    assert outcome.product_status == "passed", outcome.facts
    action = outcome.facts["actions"][0]
    receipt = json.loads(Path(action["receipt_path"]).read_bytes())
    source = outcome.evidence["source"]
    assert receipt["argv"] == action["prepared_parameters"]["argv"]
    assert receipt["cwd"] == str(source.parent)
    assert receipt["input_files_before"][str(source)] == {"sha256": sha256(b"independently-fixed-source-6193\r\n").hexdigest(), "size_bytes": 33}
    assert receipt["input_files_after"] == receipt["input_files_before"]
    assert receipt["stdout_sha256"] == sha256(Path(action["stdout_path"]).read_bytes()).hexdigest()
    assert receipt["stderr_sha256"] == sha256(b"").hexdigest()
    assert receipt["started_at"] <= receipt["completed_at"]


def test_changed_file_after_check_cannot_be_reported_as_current_verified_version(acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=["files.check-changed-after"]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == "passed", outcome.facts
    assert outcome.product_status == "failed"
    assert outcome.critical_contracts["K4"] == "failed"
    assert outcome.facts["domain"]["input_version_matches"] is False
    assert outcome.facts["actions"][0]["exit_code"] == 0
    assert outcome.evidence["domain_review"].is_file()


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
@pytest.mark.parametrize('name', ['check-version', 'check-wrong-input', 'check-wrong-command', 'check-wrong-output', 'check-wrong-script', 'check-not-executed'])
def test_check_claims_require_actual_version_command_and_output(name, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['files.'+name], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    passed = name == 'check-version'
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == ('passed' if passed else 'failed')
    assert outcome.critical_contracts['K4'] == ('passed' if passed else 'failed')
    assert outcome.semantic_status == 'not_measured'
    assert outcome.evidence['domain_review'].is_file()
    if name == 'check-not-executed':
        assert outcome.facts['actions'] == []
    else:
        assert outcome.facts['actions'][0]['exit_code'] == 0
    if name.startswith('check-wrong-'):
        original = json.loads(outcome.evidence['original_receipt'].read_bytes())
        changed = json.loads(Path(outcome.facts['actions'][0]['receipt_path']).read_bytes())
        assert original != changed


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_missing_check_receipt_blocks_the_public_evidence_chain(protocol, acceptance_public_root):
    from tests.support.agent_acceptance import SharedControlError, builtin_registry

    with pytest.raises(SharedControlError, match='receipt is missing'):
        asyncio.run(run_plan(AcceptancePlan(cases=['files.check-missing-receipt'], protocols=[protocol]),
            root=acceptance_public_root, registry=builtin_registry()))
    saved = json.loads((acceptance_public_root / 'result.json').read_bytes())
    assert saved['cases'][0]['tool_status'] == 'failed'
    assert saved['cases'][0]['case_status'] == 'failed'
    assert list(acceptance_public_root.rglob('original-receipt.json'))


@pytest.mark.acceptance_desktop
@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_formal_command_binds_real_effect_to_following_independent_check(protocol, acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.command'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed' and outcome.product_status == 'passed', outcome.facts
    assert outcome.facts['domain']['command_effect_verified'] is True
    assert outcome.facts['domain']['check_after_command'] is True
    assert outcome.facts['domain']['check']['verified'] is True
    assert outcome.semantic_status == 'not_measured'
    assert outcome.screening['scenario_id'] == 'D11'
    assert outcome.screening['request_cap'] == 20
    assert outcome.evidence['domain_review'].is_file()


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
@pytest.mark.parametrize('name', ['file-no-change', 'file-no-change-false', 'file-wrong-target', 'file-version-changed'])
def test_unchanged_or_wrong_version_is_never_a_new_deliverable(name, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['files.'+name], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed' and outcome.product_status == 'failed', outcome.facts
    assert outcome.facts['domain']['new_file_changes'] == []
    assert outcome.critical_contracts['K4'] == ('failed' if name.endswith('false') else 'passed')
    if name.startswith('file-no-change'):
        assert outcome.facts['actions'][-1]['status'] == 'completed'
        assert outcome.evidence['source'].read_bytes() == b'existing-original\r\n'
    else:
        assert outcome.facts['actions'][-1]['error']


@pytest.mark.parametrize('kind', ['denied', 'redirected'])
def test_receipt_observation_preserves_access_and_does_not_follow_retargeted_paths(kind, acceptance_public_root):
    from myrk.permissions import ApprovalSettings

    async def checked(context):
        folder = context.root / 'materials'
        folder.mkdir()
        actual = folder / 'input'
        actual.mkdir()
        source = actual / 'source.txt'
        source.write_bytes(b'allowed-public-fixture-0317')
        outside = context.root / 'outside-fixture'
        outside.mkdir()
        (outside / 'source.txt').write_bytes(b'outside-private-fixture-8259')
        script = folder / 'check.py'
        script.with_suffix('.ps1').write_text('param([string]$linkPath,[string]$targetPath)\nNew-Item -ItemType Junction -Path $linkPath -Target $targetPath\n',encoding='utf-8')
        if kind == 'denied':
            script.write_text("print('command-does-not-read-denied-argument')\n", encoding='utf-8')
        else:
            script.write_text("from pathlib import Path\nimport subprocess,sys\np=Path(sys.argv[1]).parent\np.rename(p.with_name('original-input'))\nsubprocess.run([r'C:\\Program Files\\PowerShell\\7\\pwsh.exe','-NoProfile','-File',str(Path(__file__).with_suffix('.ps1')),str(p),sys.argv[2]],check=True,capture_output=True)\n",encoding='utf-8')
        argv = [sys.executable, '-B', str(script), str(source), str(outside)]
        session = context.session([Reply(tool='run_check', arguments={'name':'Observation boundary'}), Reply(text='Actual command result only.')])
        conversation = Conversation(context.root / 'conversation.json')
        task = Task(goal='Run only the synthetic check; receipts must respect denied and redirected file boundaries.', provider_config=session.config,
            scope=TaskScope(root=str(folder), allowed_actions=['check'], provider='deepseek',recipients=['deepseek'], checks=[CheckCommand(name='Observation boundary',argv=argv)]),
            approval=ApprovalSettings(mode='full_access') if kind == 'redirected' else ApprovalSettings())
        if kind == 'denied':
            task.approval.access.denied_paths=[str(source)]
        async with session.model() as model:
            turn = await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
        return record_outcome(context,session,conversation,task,turn,turn.status=='completed' and turn.actions[0].status=='completed', facts={'observed_source':str(source)})

    registry=CaseRegistry()
    registry.register(AcceptanceCase('files.observation-boundary','process-effects',('deepseek',),checked,owner_ticket='06'))
    outcome=asyncio.run(run_plan(AcceptancePlan(cases=['files.observation-boundary']),root=acceptance_public_root,registry=registry)).cases[0]
    assert outcome.product_status=='passed',outcome.facts
    receipt=json.loads(Path(outcome.facts['actions'][0]['receipt_path']).read_bytes())
    path=outcome.facts['observed_source']
    try:
        assert receipt['input_files_after'][path] is None
        if kind=='denied':
            assert receipt['input_files_before'][path] is None
        else:
            assert receipt['input_files_before'][path]['sha256']==sha256(b'allowed-public-fixture-0317').hexdigest()
        assert sha256(b'outside-private-fixture-8259').hexdigest() not in json.dumps(receipt)
    finally:
        linked = Path(path).parent
        if kind == 'redirected' and linked.is_junction():
            linked.rmdir()


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
@pytest.mark.parametrize('name', ['command-failure', 'command-timeout', 'command-stop', 'command-recovery'])
def test_command_partial_effects_and_original_errors_survive_failure_and_recovery(name, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['files.'+name], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == ('passed' if name.endswith('recovery') else 'failed'), outcome.facts
    assert outcome.critical_contracts['K4'] == 'passed'
    assert outcome.evidence['partial_effect'].read_bytes() == b'partial-before-error-4713\r\n'
    assert outcome.facts['domain']['partial_effect_verified'] is True
    assert outcome.facts['domain']['original_error']
    assert outcome.facts['actions'][0]['cleanup_complete'] is True
    if name.endswith('recovery'):
        assert [a['status'] for a in outcome.facts['actions']] == ['failed', 'completed', 'completed']
        assert outcome.facts['domain']['recovery_check']['verified'] is True
        assert outcome.facts['domain']['actual_order_preserved'] is True
    if name.endswith('stop'):
        assert outcome.facts['domain']['no_new_request_after_stop'] is True
    if name.endswith('failure'):
        assert outcome.facts['actions'][0]['exit_code'] == 7


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
@pytest.mark.parametrize('name', ['calculation-invalid-recovery', 'calculation-wrong-answer'])
def test_exact_decimal_uses_independent_gold_and_keeps_invalid_attempt(name, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome=asyncio.run(run_plan(AcceptancePlan(cases=['files.'+name],protocols=[protocol]),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=='passed',outcome.facts
    assert outcome.product_status==('passed' if name.endswith('recovery') else 'failed'),outcome.facts
    assert outcome.facts['domain']['independent_decimal_expected']=='0.25'
    assert outcome.facts['domain']['actual_discovery_and_current_version'] is True
    assert outcome.facts['domain']['exact_tool_output'] is True
    assert outcome.critical_contracts['K4']=='passed'
    if name.endswith('recovery'):
        assert [a['status'] for a in outcome.facts['actions']]==['failed','completed','completed']
        assert outcome.facts['domain']['original_calculation_error']
    else:
        assert outcome.facts['domain']['reported_decimal_matches'] is False


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
@pytest.mark.parametrize('name', ['check-before-command', 'check-after-command-false'])
def test_explicit_check_before_change_cannot_be_replaced_by_same_final_bytes(name, protocol, acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome=asyncio.run(run_plan(AcceptancePlan(cases=['files.'+name],protocols=[protocol]),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    correct=name=='check-before-command'
    assert outcome.tool_status=='passed',outcome.facts
    assert outcome.product_status==('passed' if correct else 'failed'),outcome.facts
    assert outcome.facts['domain']['gate_check']['verified'] is True
    assert outcome.facts['domain']['required_check_before_command'] is correct
    assert outcome.evidence['product'].read_bytes()==b'actual-command-product-741\r\n'
    assert outcome.critical_contracts['K4']==('passed' if correct else 'failed')



@pytest.mark.acceptance_desktop
@pytest.mark.parametrize('protocol',['deepseek','openai-chat','google'])
@pytest.mark.parametrize('name',['formal.files','formal.calculation-check'])
def test_formal_file_and_decimal_originals_reach_the_same_domain_review(name,protocol,acceptance_public_root):
    from tests.support.agent_acceptance import builtin_registry

    outcome=asyncio.run(run_plan(AcceptancePlan(cases=[name],protocols=[protocol]),root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=='passed' and outcome.product_status=='passed',outcome.facts
    assert outcome.facts['domain']['passed'] is True
    assert outcome.evidence['domain_review'].is_file() and outcome.evidence['domain_standard'].is_file()
    assert outcome.semantic_status=='not_measured' and outcome.case_status=='pending_review'
    if name=='formal.files':
        assert outcome.screening['scenario_id']=='D09' and outcome.screening['request_cap']==20
        assert outcome.facts['domain']['exact_bytes_bom_crlf_preserved'] is True
        assert outcome.facts['domain']['required_backups_preserved'] is True
    else:
        assert outcome.screening['scenario_id']==('G02' if protocol=='google' else 'D10')
        assert outcome.screening['request_cap']==16
        assert outcome.facts['domain']['check']['checker_version_matches'] is True
        assert outcome.facts['domain']['exact_tool_output'] is True


def test_screening_registry_binds_original_d09_d10_d11_and_shared_g02():
    from tests.support.agent_acceptance import builtin_registry

    rows={row['case_id']:row for row in builtin_registry().catalogue()}
    assert all(row['observation_layer']=='conversation-sdk-executor' for name,row in rows.items() if name.startswith('files.'))
    for name,scenario,cap in [('formal.files','D09',20),('formal.calculation-check','D10',16),('formal.command','D11',20)]:
        row=rows[name]
        assert row['owner_ticket']=='06' and row['formal_live'] is True
        assert row['screening']['deepseek']=={'scenario_id':scenario,'request_cap':cap,'criteria_version':'files-calculation-v1'}
        assert row['sending_authorized'] is False
        assert {'criteria.json','check_version.py','check_cents.py','command.py'}.issubset({Path(path).name for path in row['bindings']})
    assert rows['formal.calculation-check']['screening']['google']=={'scenario_id':'G02','request_cap':16,'criteria_version':'files-calculation-v1','shared_extension_fact':'formal.guidance-extensions'}
    plan=AcceptancePlan.model_validate_json((Path(__file__).parent/'plans/deepseek-key-behavior.json').read_bytes())
    assert plan.cases.index('formal.command')==plan.cases.index('formal.calculation-check')+1
