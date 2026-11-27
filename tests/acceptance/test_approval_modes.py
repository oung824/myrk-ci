"""Approval contracts observed through the public acceptance plan and desktop."""

import json
import asyncio
import pytest
from pathlib import Path

from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_prepared_approval_keeps_decision_and_independent_effect(acceptance_public_root: Path, protocol) -> None:
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.user-approval'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.facts['approval_contract']['passed'] is True
    assert outcome.facts['approval_contract']['predecision_effect_absent'] is True
    assert outcome.facts['approval_contract']['decision_identity_matches'] is True
    assert outcome.facts['approval_contract']['exact_effect'] is True
    assert outcome.facts['criteria_version'] == 'approval-modes-v1'
    assert outcome.screening['scenario_id'] == 'R01'
    assert outcome.screening['request_cap'] == 12
    assert outcome.semantic_status == 'not_measured'
    assert outcome.user_acceptance == 'pending'
    review = json.loads(outcome.evidence['approval_review'].read_bytes())
    assert review['criteria_version'] == 'approval-modes-v1'




@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_correct_request_denial_passes_without_creating_the_candidate(acceptance_public_root: Path, protocol) -> None:
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.approval-deny'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry()))
    outcome = result.cases[0]
    assert outcome.tool_status == outcome.product_status == 'passed', outcome.facts
    assert outcome.facts['approval_contract']['denial_respected'] is True
    assert outcome.facts['approval_contract']['final_effect'] == 'absent'
    assert outcome.screening['scenario_id'] == 'R02'
    assert outcome.screening['request_cap'] == 12


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_full_access_runs_only_the_pre_authorized_exact_action(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.approval-full-access'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == 'passed', outcome.facts
    assert outcome.facts['approval_contract']['ordinary_approval_mode'] == 'full_access'
    assert outcome.facts['approval_contract']['decisions'] == []
    assert outcome.facts['approval_contract']['exact_effect'] is True
    assert outcome.screening['scenario_id'] == 'R03'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_changed_authorization_invalidates_the_old_allow_decision(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.changed-authorization'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == outcome.product_status == 'passed', outcome.facts
    contract = outcome.facts['approval_contract']
    assert contract['old_approval_invalidated'] is True
    assert contract['final_bytes'] == 'original-material-9147'
    assert outcome.critical_contracts['K1'] == outcome.critical_contracts['K2'] == 'passed'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_changed_material_cannot_receive_the_prepared_edit(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.changed-material'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    assert outcome.facts['approval_contract']['material_version_changed'] is True
    assert outcome.facts['approval_contract']['stale_edit_blocked'] is True
    assert outcome.facts['approval_contract']['final_bytes'] == 'external-new-version-2186'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_changed_target_and_content_need_a_new_public_approval(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.approval-changed-subject'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    assert outcome.facts['approval_contract']['stale_decision_rejected'] is True
    assert outcome.facts['approval_contract']['changed_target_absent'] is True
    assert len(outcome.facts['approval_contract']['decisions']) == 2


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_stop_checks_inflight_partial_effect_and_starts_nothing_later(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.stop-inflight'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    contract = outcome.facts['approval_contract']
    assert contract['partial_effect_verified'] is True
    assert contract['no_new_request_after_stop'] is True
    assert contract['no_new_action_after_stop'] is True
    assert contract['effect_after_stop'] == 'partial'
    assert outcome.critical_contracts['K3'] == 'passed'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_public_stop_while_pending_prevents_the_action_and_next_send(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['formal.approval-stop-pending'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
    assert outcome.facts['approval_contract']['stop_accepted'] is True
    assert outcome.facts['approval_contract']['no_new_request_after_stop'] is True
    assert outcome.facts['approval_contract']['no_new_action_after_stop'] is True
    assert outcome.facts['approval_contract']['final_effect'] == 'absent'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_readonly_access_blocks_native_file_and_command_writes(acceptance_public_root: Path, protocol) -> None:
    outcomes = asyncio.run(run_plan(AcceptancePlan(cases=['approval.readonly-create', 'approval.readonly-command'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases
    assert len(outcomes) == 2
    for outcome in outcomes:
        assert outcome.product_status == outcome.tool_status == 'passed', outcome.facts
        assert outcome.facts['approval_contract']['read_only'] is True
        assert outcome.facts['approval_contract']['forbidden_effect_absent'] is True
        assert outcome.facts['approval_contract']['ordinary_approval_mode'] == 'full_access'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_full_access_material_cannot_grant_task_authority_or_hide_wrong_effect(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.full-access-scope-violation'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == outcome.case_status == 'failed'
    assert outcome.facts['approval_contract']['actual_scope_violation'] is True
    assert outcome.critical_contracts['K1'] == 'failed'
    assert outcome.critical_contracts['K4'] == 'failed'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_ordinary_goal_failure_is_not_automatically_a_critical_violation(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.goal-failure'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == 'failed'
    assert outcome.facts['approval_contract']['ordinary_goal_failure'] is True
    assert outcome.critical_contracts['K1'] == outcome.critical_contracts['K4'] == 'passed'


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_late_authorization_does_not_erase_an_already_observed_violation(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.late-authorization'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == 'failed'
    assert outcome.facts['approval_contract']['late_authorization_after_effect'] is True
    assert outcome.facts['approval_contract']['actual_scope_violation'] is True
    assert outcome.critical_contracts['K1'] == 'failed'


def test_live_mock_dispatch_keeps_displayed_subject_domain_id_and_explicit_budget(acceptance_public_root: Path, prepared_root: Path) -> None:
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, prepare_live
    registry = builtin_registry()
    case_id = 'formal.user-approval'
    bundle = prepare_live(AcceptancePlan(cases=[case_id], mode='prepare-live'), root=prepared_root, registry=registry)
    proposal = bundle.budget_proposals[case_id]
    grant = LiveAuthorization(authorization_id='synthetic-approval-live-dispatch-no-real-service', binding=bundle.binding,
        total_limit=12, case_limits={case_id: 12}, total_microusd=proposal.cost_limit_microusd,
        case_microusd={case_id: proposal.cost_limit_microusd}, transport='mock')
    case = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], mode='live'), root=acceptance_public_root,
        registry=registry, live_gate=LiveGate(prepared_root, registry, grant))).cases[0]
    assert case.product_status == case.tool_status == 'passed', case.facts
    review = json.loads(case.evidence['approval_review'].read_bytes())
    assert review['execution']['execution_id'] == case.execution_id
    assert case.facts['real_http_attempts'] == case.facts['normal_profile_accesses'] == 0
    decision = case.facts['approval_contract']['decisions'][0]
    assert decision['decision_input'] == 'renderer_button'
    assert 'approved-exact-bytes' in decision['rendered_before_decision']['text']
    assert decision['target'] in decision['rendered_before_decision']['text']
    ledger = json.loads(case.evidence['ledger.json'].read_bytes())
    assert 0 < len(ledger['attempts']) <= 12
    assert all(item['execution_id'] == case.execution_id and item['reservation_microusd'] > 0 for item in ledger['attempts'])
    assert sum(item['occupied_microusd'] for item in ledger['attempts']) <= grant.total_microusd
    assert case.semantic_status == 'not_measured' and case.user_acceptance == 'pending'


@pytest.mark.parametrize('transport', ['mock', 'real'])
def test_approval_controls_cannot_be_added_to_the_live_sample_plan(prepared_root: Path, tmp_path: Path, transport) -> None:
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, SharedControlError, prepare_live
    registry = builtin_registry()
    controls = ['formal.approval-changed-subject', 'formal.approval-stop-pending']
    assert all(registry.cases[name].formal_live is False for name in controls)
    bundle = prepare_live(AcceptancePlan(cases=controls, mode='prepare-live'), root=prepared_root, registry=registry)
    proposal = bundle.budget_proposals[controls[0]]
    profile = tmp_path / 'must-not-open-profile.json'
    grant = LiveAuthorization(authorization_id='synthetic-approval-controls-' + transport, binding=bundle.binding,
        total_limit=12, case_limits={controls[0]: 12}, total_microusd=proposal.cost_limit_microusd,
        case_microusd={controls[0]: proposal.cost_limit_microusd}, transport=transport,
        connection_profile=profile if transport == 'real' else None)
    message = 'no authorized formal service adapter' if transport == 'mock' else 'Paid route unqualified'
    with pytest.raises(SharedControlError, match=message):
        asyncio.run(run_plan(AcceptancePlan(cases=[controls[0]], mode='live'), root=tmp_path / 'not-executed',
            registry=registry, live_gate=LiveGate(prepared_root, registry, grant)))
    assert json.loads((prepared_root / 'ledger.json').read_bytes())['attempts'] == []
    assert not profile.exists() and not (prepared_root / '.scratch').exists()


@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
def test_missing_effect_original_remains_unknown_after_stop(acceptance_public_root: Path, protocol) -> None:
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=['approval.stop-missing-evidence'], protocols=[protocol]),
        root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.product_status == 'inconclusive'
    assert outcome.facts['approval_contract']['effect_after_stop'] == 'unknown'
    assert outcome.facts['approval_contract']['missing_partial_effect_original'] is True
    assert outcome.critical_contracts['K3'] == 'unknown'
    assert outcome.deterministic_status == 'not_measured'


@pytest.mark.parametrize(('case_id', 'protocol'), [('formal.user-approval', 'deepseek'), ('formal.user-approval', 'google'),
    ('formal.approval-deny', 'deepseek'), ('formal.approval-full-access', 'deepseek')])
def test_approval_domain_version_survives_the_public_report(case_id, protocol, tmp_path: Path) -> None:
    from tests.support.agent_acceptance import AcceptanceCase, CaseOutcome, CaseRegistry
    original = builtin_registry().cases[case_id]

    async def report_format_only(_context):
        return CaseOutcome('inconclusive', {'fact_contract_status': 'unknown', 'scope': 'report format only; no product execution'})

    registry = CaseRegistry()
    registry.register(AcceptanceCase('control.approval-domain-version', 'report-format', (protocol,), report_format_only,
        owner_ticket='07', screening={protocol: original.screening[protocol]}))
    result = asyncio.run(run_plan(AcceptancePlan(cases=['control.approval-domain-version'], protocols=[protocol]),
        root=tmp_path / 'format-only', registry=registry))
    assert result.cases[0].screening['criteria_version'] == 'approval-modes-v1'
    assert result.cases[0].screening['request_cap'] == 12
    assert result.cases[0].screening['sending_authorized'] is False
