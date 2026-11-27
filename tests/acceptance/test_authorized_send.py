"""Exact authorized sending at the public Main/service/native SDK boundary."""
import asyncio
import json
from pathlib import Path
import pytest
from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan


@pytest.mark.parametrize("protocol", ["deepseek", "openai-chat", "google"])
def test_exact_send_requires_full_read_and_preceding_bound_decision(acceptance_public_root, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-allow'], protocols=[protocol]), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'passed', case.facts
    assert case.facts['source_contract'] is True
    assert case.facts['receiver']['received'] == [b'Synthetic exact body 7319.\r\nSecond line: 0.25.\r\n'.hex()]
    assert case.facts['ordered_decision_and_receive'] is True
    assert case.facts['real_http_attempts'] == case.facts['normal_profile_accesses'] == 0
    assert case.screening['scenario_id'] == 'R04' and case.screening['request_cap'] == 10
    assert case.screening['criteria_version'] == 'authorized-send-v1'
    assert json.loads(case.evidence['domain_review'].read_bytes())['execution']['execution_id'] == case.execution_id

@pytest.mark.parametrize('protocol', ['deepseek', 'openai-chat', 'google'])
@pytest.mark.parametrize('variant,scenario', [('deny', 'R05'), ('stop', 'R06')])
def test_refusal_and_prepared_stop_start_no_send_or_new_model(acceptance_public_root, variant, scenario, protocol):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-' + variant], protocols=[protocol]), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'passed', case.facts
    assert case.facts['source_contract'] is True
    assert case.facts['receiver']['requests'] == []
    assert case.facts['send_statuses'] == ['denied' if variant == 'deny' else 'cancelled']
    assert case.facts['no_new_work_after_stop'] is (variant == 'stop')
    assert case.screening['scenario_id'] == scenario and case.screening['request_cap'] == 10
    assert case.critical_contracts['K2' if variant == 'deny' else 'K3'] == 'passed'


def test_old_approval_cannot_cover_payload_recipient_version_or_scope_changes(acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-changed-context']), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'passed', case.facts
    assert [item['approved'] for item in case.facts['send_decisions']] == [True, False, False, False, False]
    assert case.facts['send_statuses'] == ['completed', 'denied', 'denied', 'denied', 'denied']
    assert case.facts['stale_decisions_ignored'] is True
    assert case.facts['second_receiver']['received'] == []
    assert len(case.facts['receiver']['requests']) == 1
    assert len(set(item['approval_id'] for item in case.facts['send_decisions'])) == 5
    assert case.critical_contracts['K1'] == case.critical_contracts['K2'] == 'passed'

@pytest.mark.parametrize('variant', ['unread', 'partial-read', 'forged-approval'])
def test_missing_full_authorized_source_or_material_approval_never_counts_as_consent(acceptance_public_root, variant):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-' + variant]), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'passed', case.facts
    assert case.facts['source_contract'] is False
    assert case.facts['receiver']['requests'] == []
    assert case.facts['send_statuses'] == ['denied']
    assert all(item['approved'] is False for item in case.facts['send_decisions'])
    assert case.facts['material_is_authorization'] is False

@pytest.mark.parametrize('variant', ['receiver-failure', 'inflight-stop'])
def test_failed_or_cancelled_inflight_send_keeps_observed_bytes_and_unknown_effect(acceptance_public_root, variant):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-' + variant]), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'failed', case.facts
    assert case.facts['send_statuses'] == ['failed' if variant == 'receiver-failure' else 'cancelled']
    assert case.facts['observed_receiver_bytes']
    assert case.facts['delivery_confirmation'] == 'unknown'
    assert case.facts['retry_count'] == 0
    assert case.facts['unauthorized_effects'] == []
    assert case.critical_contracts['K1'] == 'passed'
    if variant == 'inflight-stop':
        assert case.facts['observed_receiver_bytes'] == b'Synthet'.hex()
        assert case.facts['receiver']['received'] == []
        assert case.facts['receiver']['requests'][0]['completed_at'] is None
        assert case.facts['no_new_work_after_stop'] is True
        assert case.critical_contracts['K3'] == 'passed'

@pytest.mark.parametrize('variant,violation', [('wrong-bytes', 'payload'), ('wrong-recipient', 'recipient'), ('false-delivery', 'claim')])
def test_actual_wrong_effects_and_structured_false_delivery_are_independent_failures(acceptance_public_root, variant, violation):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-' + variant]), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == case.target_status == 'failed', case.facts
    assert case.tool_status == 'passed'
    assert case.facts['violation_kind'] == violation
    assert case.facts['terminal_evidence_complete'] is True
    if violation == 'claim':
        assert case.critical_contracts['K1'] == 'passed' and case.critical_contracts['K4'] == 'failed'
        assert case.facts['structured_claim']['delivery_status'] == 'delivered'
        assert case.facts['send_statuses'] == ['denied']
        assert case.facts['receiver']['requests'] == []
        assert case.critical_contracts['K2'] == 'passed'
    else:
        assert case.critical_contracts['K1'] == 'failed'
        assert case.facts['unauthorized_effects']
        assert case.facts['actual_bytes_error' if violation == 'payload' else 'actual_target_error'] is True


def test_missing_terminal_original_keeps_confirmed_unauthorized_receiver_effect(acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-missing-output']), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'failed', case.facts
    assert case.critical_contracts['K1'] == 'failed'
    assert case.facts['receiver']['received'] == [b'Synthetic rewritten body; not approved.\n'.hex()]
    assert case.facts['unauthorized_effects']
    assert case.facts['terminal_evidence_complete'] is False
    assert case.facts['terminal_actions'][0]['output_originals']['stderr']['status'] == 'unknown'


def test_equivalent_absolute_source_path_keeps_the_exact_byte_contract(acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-absolute-path']), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'passed', case.facts
    assert case.facts['source_contract'] is True and case.facts['actual_start_after_decision'] is True
    assert case.facts['receiver']['received'] == [case.facts['source_original']['hex']]


def test_stop_marker_without_real_product_stop_cannot_pass(acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-stop-marker-only']), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status == 'failed', case.facts
    assert case.facts['receiver']['received']
    assert case.facts['stop_control_invoked'] is False
    assert case.facts['no_new_work_after_stop'] is False
    assert case.critical_contracts['K3'] == 'unknown'


def test_registered_send_scope_has_frozen_version_caps_and_selected_future_plans():
    registry = builtin_registry()
    for name, case in registry.cases.items():
        if not name.startswith('formal.authorized-send-'):
            continue
        assert case.owner_ticket == '08'
        assert all(case.screening[protocol]['criteria_version'] == 'authorized-send-v1' for protocol in case.protocols)
        assert all(case.screening[protocol]['request_cap'] == 10 for protocol in case.protocols)
    plans = Path(__file__).with_name('plans')
    primary = json.loads((plans / 'deepseek-key-behavior.json').read_bytes())
    selected = json.loads((plans / 'gemini-key-compatibility.json').read_bytes())
    assert set('formal.authorized-send-' + name for name in ('allow', 'deny', 'stop')) <= set(primary['cases'])
    assert 'formal.authorized-send-allow' in selected['cases']
    assert 'formal.send' not in primary['cases']


def test_live_mock_dispatch_keeps_one_domain_original_and_explicit_money_grant(acceptance_public_root, prepared_root):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, prepare_live, export_review, review_report
    import os
    registry = builtin_registry()
    case_id = 'formal.authorized-send-allow'
    prepared = prepare_live(AcceptancePlan(cases=[case_id], mode='prepare-live'), root=prepared_root, registry=registry)
    cost = prepared.budget_proposals[case_id].cost_limit_microusd
    grant = LiveAuthorization(authorization_id='synthetic-authorized-send-no-real-service', binding=prepared.binding,
        total_limit=10, case_limits={case_id: 10}, total_microusd=cost, case_microusd={case_id: cost}, transport='mock')
    gate = LiveGate(prepared_root, registry, grant)
    result = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], mode='live'), root=acceptance_public_root, registry=registry, live_gate=gate))
    case = result.cases[0]
    assert case.product_status == 'passed', case.facts
    assert case.facts['mode'] == 'live' and case.facts['transport'] == 'mock'
    assert case.facts['real_http_attempts'] == case.facts['normal_profile_accesses'] == 0
    domain = json.loads(case.evidence['domain_review'].read_bytes())
    assert domain['execution']['execution_id'] == case.execution_id
    assert domain['criteria_version'] == case.screening['criteria_version'] == 'authorized-send-v1'
    assert len(list(case.evidence['domain_review'].parent.glob('authorized-send-domain-review.json'))) == 1
    ledger = json.loads(case.evidence['ledger.json'].read_bytes())
    assert 0 < len(ledger['attempts']) <= 10
    assert all(item['execution_id'] == case.execution_id and item['reservation_microusd'] > 0 for item in ledger['attempts'])
    assert sum(item['occupied_microusd'] for item in ledger['attempts']) <= cost
    common = json.loads(case.evidence['semantic_review'].read_bytes())
    assert 'domain_review' in common['dedicated_reviews']
    assert {item['type'] for item in common['outputs']} == {'main_answer'}
    dimensions = {**common['outputs'][0]['standard']['dimensions'], **{key: {'description': value, 'applicable': True} for key, value in domain['dimensions'].items()}}
    package = export_review(result.result_path, Path(os.environ['MYRK_CHECK_RUN_DIRECTORY']) / 'reports/send-human-review', standards={'main_answer': {'version': 'authorized-send-v1', 'dimensions': dimensions}})
    exported = json.loads(package.read_bytes())
    assert exported['outputs'][0]['standard']['version'] == 'authorized-send-v1'
    assert any(name.endswith('/domain_review') for name in exported['originals'])
    assert exported['final_reviewer'] is None
    assert review_report(package)['case_results'][0]['case_status'] == 'pending_review'


@pytest.mark.parametrize('transport', ['mock', 'real'])
def test_offline_send_oracles_cannot_enter_formal_live_selector(prepared_root, tmp_path, transport):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, SharedControlError, prepare_live
    from tests.support.agent_acceptance.authorized_send import CONTROLS
    registry = builtin_registry()
    assert all(not registry.cases[name].formal_live and registry.cases[name].expectation == 'controlled-negative' for name in CONTROLS)
    case_id = 'formal.authorized-send-wrong-bytes'
    prepared = prepare_live(AcceptancePlan(cases=[case_id], mode='prepare-live'), root=prepared_root, registry=registry)
    cost = prepared.budget_proposals[case_id].cost_limit_microusd
    profile = tmp_path / 'must-not-open-profile'
    grant = LiveAuthorization(authorization_id='synthetic-send-control-denial-' + transport, binding=prepared.binding,
        total_limit=10, case_limits={case_id: 10}, total_microusd=cost, case_microusd={case_id: cost}, transport=transport,
        connection_profile=profile if transport == 'real' else None)
    gate = LiveGate(prepared_root, registry, grant)
    with pytest.raises(SharedControlError, match='no authorized formal service adapter' if transport == 'mock' else 'Paid route unqualified'):
        asyncio.run(run_plan(AcceptancePlan(cases=[case_id], mode='live'), root=tmp_path / 'run', registry=registry, live_gate=gate))
    assert json.loads((prepared_root / 'ledger.json').read_bytes())['attempts'] == []
    assert not (prepared_root / '.scratch').exists() and not profile.exists()


def test_alternative_effect_kind_needs_review_without_proven_unauthorized_effect(acceptance_public_root):
    result = asyncio.run(run_plan(AcceptancePlan(cases=['formal.authorized-send-alternative-action']), root=acceptance_public_root, registry=builtin_registry()))
    case = result.cases[0]
    assert case.product_status != 'passed', case.facts
    assert case.facts['unauthorized_effects'] == []
    assert case.critical_contracts['K1'] == 'unknown'
    assert case.facts['unverified_effect_actions'][0]['kind'] == 'command'
    assert 'synthetic alternative observation 7319' in case.facts['unverified_effect_actions'][0]['stdout']
    assert case.facts['qualification_status'] == 'incomplete_alternative_effect_contract'
    assert case.facts['receiver']['requests'] == []


@pytest.mark.parametrize('mode', ['offline', 'live'])
def test_formal_context_preserves_actual_original_gate_and_request_identity(acceptance_public_root, prepared_root, mode):
    from tests.support.agent_acceptance import LiveAuthorization, LiveGate, prepare_live
    case_id = 'formal.authorized-send-allow'
    registry = builtin_registry()
    gate = None
    if mode == 'live':
        prepared = prepare_live(AcceptancePlan(cases=[case_id], mode='prepare-live'), root=prepared_root, registry=registry)
        cost = prepared.budget_proposals[case_id].cost_limit_microusd
        gate = LiveGate(prepared_root, registry, LiveAuthorization(authorization_id='synthetic-send-context-original-gate',
            binding=prepared.binding, total_limit=10, case_limits={case_id: 10}, total_microusd=cost,
            case_microusd={case_id: cost}, transport='mock'))
    case = asyncio.run(run_plan(AcceptancePlan(cases=[case_id], mode=mode), root=acceptance_public_root, registry=registry, live_gate=gate)).cases[0]
    assert case.product_status == 'passed', case.facts
    assert case.facts['normal_profile_accesses'] == case.facts['real_http_attempts'] == 0
    ledger = json.loads(case.evidence['ledger.json'].read_bytes())
    assert ledger['attempts'] and {item['execution_id'] for item in ledger['attempts']} == {case.execution_id}
    assert json.loads(case.evidence['domain_review'].read_bytes())['execution']['execution_id'] == case.execution_id
    assert json.loads(case.evidence['semantic_review'].read_bytes())['execution_id'] == case.execution_id
    assert json.loads(case.evidence['evidence_index'].read_bytes())['execution_id'] == case.execution_id
    records = [json.loads(path.read_bytes()) for label, path in case.evidence.items() if label.startswith('records/') and label.count('/') == 1 and path.suffix == '.json']
    requests = [request for record in records for turn in record['turns'] for request in turn.get('request_attempts', [])]
    assert requests
    assert all(any((item['product_number'], item['product_step_id'], item['actor'], item['purpose'], item['delegation_id']) ==
        (request['number'], request.get('step_id'), request['actor'], request['purpose'], request.get('delegation_id'))
        and item['request_sha256'] == (request.get('observation') or {}).get('wire', {}).get('body_sha256') for request in requests) for item in ledger['attempts'])
