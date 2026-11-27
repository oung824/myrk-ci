"""Independent review observed through the public formal acceptance path."""

import asyncio
import json

import pytest
from pathlib import Path

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan


@pytest.mark.parametrize(("case_id", "approval", "status"), [
    ("formal.independent-review", "passed", "passed"),
    ("formal.review-unclassified-inspection", "passed", "passed"),
    ("formal.review-unread", "failed", "passed"),
    ("formal.delegation", "not_applicable", "passed"),
    ("formal.delegation-scope-deny", "failed", "unknown"),
    ("formal.delegation-wrong-version", "not_applicable", "failed"),
])
def test_default_formal_review_has_case_critical_conclusions(
    acceptance_public_root: Path, case_id: str, approval: str, status: str,
) -> None:
    from tests.support.agent_acceptance import HumanReview, export_review, import_reviews, review_report

    result = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], protocols=["deepseek"]),
        root=acceptance_public_root, registry=builtin_registry()))
    original = result.result_path.read_bytes()
    package_path = export_review(result.result_path, acceptance_public_root / "synthetic-test-review")
    package = json.loads(package_path.read_bytes())
    reports = review_report(package_path)
    assert reports["case_results"][0]["critical_contracts"] == result.cases[0].critical_contracts

    forms = []
    for index, output in enumerate(package["outputs"]):
        dimensions = output["standard"]["dimensions"]
        conclusions = {name: "passed" for name in dimensions}
        if output["type"] in {"main_answer", "child_report"}:
            assert dimensions["K4"]["critical"] is True
            conclusions["K4"] = "unknown" if status == "failed" else status
        else:
            assert "K4" not in dimensions
        if output["type"] == "review_reason" or case_id == "formal.delegation-scope-deny" and output["type"] == "child_report":
            assert dimensions["K2"]["critical"] is True
            conclusions["K2"] = "unknown" if approval == "failed" else approval
        else:
            assert "K2" not in dimensions
        forms.append(HumanReview(review_id=f"synthetic-form-{index}", output_id=output["output_id"],
            reviewer="Synthetic test fixture", authorship="human", signed_at="2026-11-24T12:00:00+08:00",
            scope="Synthetic review import and report contract test",
            package_sha256=package["binding"], criteria_version=output["standard"]["version"],
            output_sha256=output["sha256"], reviewed_originals=output["required_originals"],
            conclusions=conclusions, reasons={name: "Synthetic unknown rating" for name, value in conclusions.items() if value == "unknown"}))
    assert forms
    if case_id == "formal.review-unclassified-inspection":
        assert result.cases[0].critical_contracts["K2"] == "unknown"
        standard = next(output["standard"] for output in package["outputs"] if output["type"] == "review_reason")
        original_dimensions = standard["dimensions"]
        assert original_dimensions["K2"]["case_evidence_complete"] is True
        omitted = {name: row for name, row in original_dimensions.items() if name != "K2"}
        for name, dimensions in (
            ("omitted", omitted),
            ("forged", {**original_dimensions, "K2": {**original_dimensions["K2"], "case_evidence_complete": False}}),
            ("malformed", {**original_dimensions, "K2": {**original_dimensions["K2"], "case_evidence_complete": 1}}),
            ("introduced", {**original_dimensions, "K1": {**original_dimensions["K2"], "case_evidence_complete": True}}),
        ):
            with pytest.raises(ValueError, match="execution-owned critical dimensions|case evidence must be a Boolean"):
                export_review(result.result_path, acceptance_public_root / f"synthetic-invalid-override-{name}",
                    standards={"review_reason": {**standard, "dimensions": dimensions}})

    if case_id == "formal.delegation-scope-deny":
        assert result.cases[0].critical_contracts["K2"] == "passed"
        child = next(form for form in forms if "K2" in form.conclusions)
        forms = [child.model_copy(update={
            "conclusions": {**child.conclusions, "K2": "failed"},
            "reasons": {**child.reasons, "K2": "Synthetic reviewer flags a critical approval issue."},
        })]

    report = import_reviews(package_path, forms)
    reviewed = report["case_results"][0]
    assert reviewed["critical_contracts"]["K2"] == approval
    assert reviewed["critical_contracts"]["K4"] == status
    if case_id == "formal.delegation-scope-deny":
        assert report["original_product_results"][0]["critical_contracts"]["K2"] == "passed"
    assert reviewed["case_status"] == ("failed" if "failed" in (approval, status) else "pending_review")
    assert reviewed["user_acceptance"] == "pending"
    assert all(output["semantic_status"] == "not_measured" for output in report["outputs"])
    assert result.result_path.read_bytes() == original


@pytest.mark.parametrize('protocol',['deepseek','openai-chat','google'])
def test_formal_review_recovers_after_actual_missing_evidence(acceptance_public_root: Path, protocol: str) -> None:
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.independent-review'],protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['evidence_recovery_order_verified'] is True
    assert contract['candidate_identity_matches'] is True
    assert contract['exact_effect_verified'] is True
    assert outcome.evidence['candidate.txt'].read_bytes()==b'approved-candidate-bytes'
    assert outcome.screening['scenario_id'] == 'R07'
    assert outcome.screening['request_cap'] == 16
    assert outcome.semantic_status == 'not_measured'
    assert outcome.critical_contracts['K2'] == 'unknown'
    review = json.loads(outcome.evidence['independent_review'].read_bytes())
    assert review['execution']['execution_id'] == outcome.execution_id
    assert review['criteria_version'] == 'independent-review-v1'
    assert review['human_review'] == 'not_performed'


@pytest.mark.parametrize('protocol',['deepseek','openai-chat','google'])
def test_formal_explicit_review_denial_does_not_execute_or_repeat(acceptance_public_root: Path, protocol: str) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-deny'],protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['review_status'] == 'denied'
    assert contract['denial_respected'] is True
    assert contract['final_effect'] == 'absent'
    assert contract['human_fallback'] is False
    assert outcome.screening['scenario_id'] == 'R08'


@pytest.mark.parametrize('protocol',['deepseek','openai-chat','google'])
def test_public_stop_cancels_inflight_review_without_later_send(acceptance_public_root: Path, protocol: str) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-stop'],protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['review_status'] == 'cancelled'
    assert contract['stop_accepted'] is True
    assert contract['no_new_request_after_stop'] is True
    assert contract['no_new_action_after_stop'] is True
    assert contract['inflight_usage_unknown'] is True
    assert contract['final_effect'] == 'absent'
    assert outcome.screening['scenario_id'] == 'R09'
    assert outcome.critical_contracts['K3'] == 'passed'
    assert outcome.facts['processes_complete'] is True


@pytest.mark.parametrize('method', ['native', 'command'])
def test_independent_reviewer_cannot_write_with_native_program_or_shell(acceptance_public_root: Path, method: str) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-readonly-'+method]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['readonly_toolset_verified'] is True
    assert contract['readonly_os_write_blocked'] is True
    assert contract['review_probe_absent'] is True
    assert contract['denial_respected'] is True


@pytest.mark.parametrize('branch,status,product', [('service-retry','allowed','passed'),
    ('service-failure','failed','failed'), ('stream-retry','allowed','passed'), ('timeout','timeout','failed')])
def test_review_service_errors_stream_fragments_and_deadline_keep_their_layers(acceptance_public_root: Path, branch: str, status: str, product: str) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-'+branch]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == product, outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['review_status'] == status
    assert contract['error_layer'] == branch
    assert contract['original_failure_preserved'] is True
    assert contract['human_fallback'] is False
    assert contract['denial_respected'] is False
    if branch == 'stream-retry':
        assert contract['partial_review_output_preserved'] is True
    if product == 'failed':
        assert contract['final_effect'] == 'absent'


@pytest.mark.parametrize('subject', ['authorization','action'])
def test_old_review_allow_cannot_apply_after_subject_changes(acceptance_public_root: Path, subject: str) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-changed-'+subject]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['old_allow_invalidated'] is True
    assert contract['changed_subject_preserved'] is True
    assert contract['human_fallback'] is False


@pytest.mark.parametrize('missing', [False,True])
def test_stopped_reviewed_action_keeps_partial_effect_or_unknown(acceptance_public_root: Path, missing: bool) -> None:
    name = 'review.action-stop-missing' if missing else 'review.action-stop'
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=[name]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['review_status'] == 'allowed'
    assert contract['no_new_request_after_stop'] is True
    assert contract['no_new_action_after_stop'] is True
    assert contract['process_cleanup_complete'] is True
    assert contract['partial_effect_verified'] is (not missing)
    assert outcome.product_status == ('inconclusive' if missing else 'passed')
    assert outcome.critical_contracts['K3'] == ('unknown' if missing else 'passed')


def test_all_main_outputs_and_review_reasons_keep_inputs_candidate_and_gold_facts(acceptance_public_root: Path) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-changed-authorization']),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    common = json.loads(outcome.evidence['semantic_review'].read_bytes())
    reasons = [output for output in common['outputs'] if output['type']=='review_reason']
    assert len(reasons) == 2
    assert any(output['type']=='main_answer' for output in common['outputs'])
    for output in common['outputs']:
        assert output['standard']['version']=='independent-review-v1::critical-contracts-v1'
        assert output['domain_criteria']['version']=='independent-review-v1'
        assert output['domain_criteria']['independent_expected_facts']['sha256']
        assert output['actual_input']['sdk']['request_identity']['purpose']=='work'
        assert output['provenance']=='fixed_fixture'
    review = json.loads(outcome.evidence['independent_review'].read_bytes())
    assert len(review['request_bindings']) == 4
    assert [row['request_identity']['actor'] for row in review['request_bindings']] == ['main','action_review','action_review','main']
    assert all(row['candidate_subjects'] and row['raw_response_sha256'] and row['sdk_input_sha256'] for row in review['request_bindings'])
    assert outcome.critical_contracts['K2']==outcome.critical_contracts['K4']=='unknown'


def test_live_mock_keeps_one_domain_original_and_actual_review_budget(acceptance_public_root: Path, prepared_root: Path) -> None:
    from tests.support.agent_acceptance import LiveAuthorization,LiveGate,prepare_live
    registry = builtin_registry()
    name = 'formal.independent-review'
    bundle = prepare_live(AcceptancePlan(cases=[name],mode='prepare-live'),root=prepared_root,registry=registry)
    proposal = bundle.budget_proposals[name]
    grant = LiveAuthorization(authorization_id='synthetic-independent-review-no-real-service',binding=bundle.binding,
        total_limit=16,case_limits={name:16},transport='mock',total_microusd=proposal.cost_limit_microusd,
        case_microusd={name:proposal.cost_limit_microusd})
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=[name],mode='live'),root=acceptance_public_root,
        registry=registry,live_gate=LiveGate(prepared_root,registry,grant))).cases[0]
    assert outcome.tool_status==outcome.product_status=='passed',outcome.facts
    review = json.loads(outcome.evidence['independent_review'].read_bytes())
    assert review['execution']['execution_id']==outcome.execution_id
    assert outcome.facts['real_http_attempts']==outcome.facts['normal_profile_accesses']==0
    ledger = json.loads(outcome.evidence['ledger.json'].read_bytes())
    assert len(ledger['attempts'])==5
    assert [row['actor'] for row in ledger['attempts']]==['main','action_review','action_review','action_review','main']
    assert all(row['execution_id']==outcome.execution_id and row['reservation_microusd']>0 for row in ledger['attempts'])
    assert sum(row['occupied_microusd'] for row in ledger['attempts'])<=grant.total_microusd
    common = json.loads(outcome.evidence['semantic_review'].read_bytes())
    assert all({'authorization','rationale','factual_accuracy','sufficiency','readability','critical_status'} <= set(output['standard']['dimensions']) for output in common['outputs'])
    assert outcome.semantic_status=='not_measured' and outcome.user_acceptance=='pending'


@pytest.mark.parametrize('branch,passed',[('unread',False),('out-of-order',False),('equivalent-command',True)])
def test_review_evidence_order_uses_actual_inspection_and_allows_equivalent_native_tool(acceptance_public_root: Path, branch: str, passed: bool) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-'+branch]),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=='passed',outcome.facts
    assert outcome.product_status==('passed' if passed else 'failed'),outcome.facts
    contract = outcome.facts['independent_review_contract']
    assert contract['evidence_recovery_order_verified'] is passed
    if passed:
        assert [item['tool'] for item in contract['observed_inspections']]==['run_command','run_command']
    else:
        assert outcome.critical_contracts['K2']=='failed'
    assert builtin_registry().cases['formal.review-'+branch].formal_live is False


@pytest.mark.parametrize('risk,protocol',[('r07','deepseek'),('r08','deepseek'),('r09','deepseek'),('r07','google')])
def test_frozen_risk_runs_three_complete_main_executions_with_real_material(risk: str, protocol: str, acceptance_public_root: Path, prepared_root: Path) -> None:
    from hashlib import sha256
    from tests.support.agent_acceptance import LiveAuthorization,LiveGate,prepare_live
    from tests.support.agent_acceptance.frozen import FrozenExecution,FrozenParentBudget
    from myrk.project_paths import project_root
    materials = Path(__file__).parent/'materials/independent-review'
    specification = materials/'risk-materials.json'
    family = json.loads(specification.read_bytes())['families'][risk]
    rows = [FrozenExecution(execution_id=f'{risk}:{protocol}:{number}',case_id=family['case_id'],protocol=protocol,
        scenario_id=family['scenario_id'],material_family_id=family['family_id'],material_variant=item['material_variant'],
        material_path=materials/item['material'],material_sha256=sha256((materials/item['material']).read_bytes()).hexdigest(),
        equivalence_path=specification,equivalence_sha256=sha256(specification.read_bytes()).hexdigest(),
        startpoint=f'{risk}-{protocol}-independent-start-{number}',order=number,risk_branch=family['branch'],
        request_cap=16,cost_cap_microusd=16*122880) for number,item in enumerate(family['executions'],1)]
    parent = FrozenParentBudget(parent_id='synthetic-review-'+prepared_root.name,accounting_root=prepared_root,
        request_cap=48,phase_microusd={'screening':48*122880,'quality':0,'judge':0},reserve_microusd=0,profile='synthetic')
    plan = AcceptancePlan(executions=rows,parent_budget=parent)
    registry = builtin_registry()
    activation = project_root(Path(__file__).resolve().parents[2])/'.scratch/acceptance-parent-budgets'/(sha256(parent.parent_id.encode()).hexdigest()+'.json')
    observation = acceptance_public_root.with_name(acceptance_public_root.name+'-scope-before.json')
    observation.parent.mkdir(parents=True,exist_ok=True)
    assert not activation.exists() and not prepared_root.exists()
    observation.write_text(json.dumps({'phase':'supplemental precreation observation; original task baseline unchanged',
        'prepared_root':str(prepared_root),'parent_activation':str(activation),'both_absent_before':True,
        'owner':'synthetic readonly independent review test','delete_when':'Actual formal processes exit and required originals are preserved.'},indent=2)+'\n',encoding='utf-8')
    try:
        bundle = prepare_live(plan.model_copy(update={'mode':'prepare-live'}),root=prepared_root,registry=registry)
        grant = LiveAuthorization(authorization_id='synthetic-review-frozen-parent',binding=bundle.binding,transport='mock',
            parent_budget_id=parent.parent_id,total_limit=48,case_limits={row.execution_id:16 for row in rows},
            total_microusd=48*122880,case_microusd={row.execution_id:16*122880 for row in rows})
        result = asyncio.run(run_plan(plan,root=acceptance_public_root,registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
        assert [case.execution_id for case in result.cases]==[row.execution_id for row in rows]
        assert len({str(case.evidence['independent_review']) for case in result.cases})==3
        for row,case in zip(rows,result.cases):
            assert case.product_status==case.tool_status=='passed',case.facts
            original = json.loads(case.evidence['independent_review'].read_bytes())
            assert original['execution']['execution_id']==row.execution_id
            assert original['execution']['material_sha256']==row.material_sha256
            assert case.facts['real_http_attempts']==0
            assert case.facts['processes_complete'] is True
            exchange = json.loads(case.evidence['work/receiver/exchange.json'].read_bytes())
            material = (materials/family['executions'][row.order-1]['material']).read_text(encoding='utf-8').strip()
            assert all(material in json.dumps(item['messages'],ensure_ascii=False) for item in exchange['sdk_inputs'])
            if risk=='r07':
                assert case.facts['independent_review_contract']['evidence_recovery_order_verified'] is True
                assert material in str(case.facts['independent_review_contract']['observed_inspections'][-1]['original_tool_result']['content'])
            ledger = json.loads(case.evidence['ledger.json'].read_bytes())
            actual = [attempt for attempt in ledger['attempts'] if attempt['execution_id']==row.execution_id]
            assert 0<len(actual)<=16 and all(attempt['role']=='product' and attempt['reservation_microusd']>0 for attempt in actual)
            assert case.semantic_status=='not_measured' and case.user_acceptance=='pending'
        assert json.loads(result.result_path.read_bytes())['planned_denominator']==3
    finally:
        if activation.exists() and not any((prepared_root/'.scratch').glob('execution-*')):
            assert activation.parent==project_root(Path(__file__).resolve().parents[2])/'.scratch/acceptance-parent-budgets'
            assert not activation.is_symlink() and not activation.is_junction()
            data = json.loads(activation.read_bytes())
            assert data['parent_id']==parent.parent_id and Path(data['accounting_root']).resolve()==prepared_root.resolve()
            activation.unlink()


def test_legal_inspection_without_a_program_oracle_remains_unknown(acceptance_public_root: Path) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.review-unclassified-inspection']),
        root=acceptance_public_root,registry=builtin_registry())).cases[0]
    assert outcome.tool_status=='passed',outcome.facts
    assert outcome.product_status=='inconclusive'
    contract = outcome.facts['independent_review_contract']
    assert contract['evidence_order_status']=='unknown'
    assert contract['exact_effect_verified'] is True
    assert len(contract['unclassified_inspections'])==2
    assert outcome.deterministic_status=='not_measured'
    assert outcome.critical_contracts['K2']=='unknown'
    assert outcome.facts['goal_program_contract_passed'] is False


def test_frozen_formal_without_the_original_parent_gate_sends_nothing(acceptance_public_root: Path) -> None:
    from hashlib import sha256
    from tests.support.agent_acceptance import SharedControlError
    from tests.support.agent_acceptance.frozen import FrozenExecution
    materials = Path(__file__).parent/'materials/independent-review'
    criteria = materials/'risk-materials.json'
    rows = [FrozenExecution(execution_id=f'no-parent:{number}',case_id='formal.independent-review',protocol='deepseek',
        scenario_id='R07',material_family_id='independent-evidence-family',material_variant='variant' if number==3 else 'baseline',
        material_path=materials/name,material_sha256=sha256((materials/name).read_bytes()).hexdigest(),startpoint=f'no-parent-start-{number}',
        order=number,risk_branch='failed-evidence-then-alternative-allow',request_cap=16,cost_cap_microusd=16*122880,
        equivalence_path=criteria,equivalence_sha256=sha256(criteria.read_bytes()).hexdigest())
        for number,name in enumerate(('r07-baseline-a.txt','r07-baseline-b.txt','r07-variant.txt'),1)]
    with pytest.raises(SharedControlError,match='original parent gate'):
        asyncio.run(run_plan(AcceptancePlan(executions=rows),root=acceptance_public_root,registry=builtin_registry()))
    saved = json.loads((acceptance_public_root/'result.json').read_bytes())
    assert [case['execution_id'] for case in saved['cases']]==[row.execution_id for row in rows]
    assert [case['execution_status'] for case in saved['cases']]==['invalid','not_executed','not_executed']
    assert not list(acceptance_public_root.rglob('authorization.json'))
    assert not list(acceptance_public_root.rglob('ledger.json'))


def test_controlled_review_probes_have_no_formal_sending_selector(prepared_root: Path, acceptance_public_root: Path) -> None:
    from tests.support.agent_acceptance import LiveAuthorization,LiveGate,SharedControlError,prepare_live
    registry = builtin_registry()
    controls = ['formal.review-unread','formal.review-out-of-order','formal.review-equivalent-command',
        'formal.review-unclassified-inspection','formal.review-readonly-native','formal.review-readonly-command',
        'formal.review-service-retry','formal.review-service-failure','formal.review-stream-retry','formal.review-timeout',
        'formal.review-changed-authorization','formal.review-changed-action']
    assert all(registry.cases[name].formal_live is False for name in controls)
    name = 'formal.review-unclassified-inspection'
    bundle = prepare_live(AcceptancePlan(cases=[name],mode='prepare-live'),root=prepared_root,registry=registry)
    proposal = bundle.budget_proposals[name]
    grant = LiveAuthorization(authorization_id='synthetic-control-selector-no-real-service',binding=bundle.binding,transport='mock',
        total_limit=16,case_limits={name:16},total_microusd=proposal.cost_limit_microusd,
        case_microusd={name:proposal.cost_limit_microusd})
    with pytest.raises(SharedControlError,match='no authorized formal service adapter'):
        asyncio.run(run_plan(AcceptancePlan(cases=[name],mode='live'),root=acceptance_public_root,
            registry=registry,live_gate=LiveGate(prepared_root,registry,grant)))
    assert json.loads((prepared_root/'ledger.json').read_bytes())['attempts']==[]
    assert not (prepared_root/'.scratch').exists()
    originals = acceptance_public_root.with_name(acceptance_public_root.name+'-closed-selector')
    originals.mkdir(parents=True,exist_ok=False)
    for leaf in ('prepared.json','ledger.json'):
        (originals/leaf).write_bytes((prepared_root/leaf).read_bytes())
    (originals/'synthetic-grant.json').write_text(grant.model_dump_json(indent=2)+'\n',encoding='utf-8')
