"""Triggered contracts use original public observations, including missing evidence."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from tests.acceptance.test_quality_memory_aux import auxiliary_packet


def test_interrupted_quality_reading_derives_recovery_and_reopen_contracts(auxiliary_packet, acceptance_public_root):
    from tests.support.agent_acceptance.quality_files import exercise_files, score
    from tests.support.agent_acceptance.quality_runtime import bound_task
    outcome = asyncio.run(exercise_files(auxiliary_packet, 'QG1.formal.10', 'deepseek', root=acceptance_public_root))
    assert outcome.product_status == 'passed', outcome.facts
    _, visible, _ = bound_task('quality.qg1.formal.10', acceptance_public_root)
    records = [json.loads(outcome.evidence['conversation'].read_bytes())]
    exchanges = [json.loads(outcome.evidence['exchange'].read_bytes())]
    actual = score(visible, acceptance_public_root, records, exchanges, native_entry=True)
    assert actual['critical_contracts']['K3'] == actual['critical_contracts']['K5'] == 'passed'
    path = acceptance_public_root / 'interruption-observation.json'
    original = path.read_bytes()
    (acceptance_public_root / 'complete-interruption-observation.original.json').write_bytes(original)
    path.unlink()
    missing = score(visible, acceptance_public_root, records, exchanges, native_entry=True)
    assert missing['critical_contracts']['K3'] == missing['critical_contracts']['K5'] == 'unknown'
    control = json.loads(original); control['open_sends'] = 1
    path.write_text(json.dumps(control), encoding='utf-8')
    failed = score(visible, acceptance_public_root, records, exchanges, native_entry=True)
    assert failed['critical_contracts']['K3'] == failed['critical_contracts']['K5'] == 'failed'
    assert failed['passed'] is False
    partial = json.loads(original); partial['recovery'].pop('can_resume')
    path.write_text(json.dumps(partial), encoding='utf-8')
    assert score(visible, acceptance_public_root, records, exchanges, native_entry=True)['critical_contracts']['K3'] == 'unknown'
    path.unlink()
    changed = deepcopy(records)
    changed[0]['turns'][-1]['task']['approval']['access']['writable_roots'] = []
    failed = score(visible, acceptance_public_root, changed, exchanges, native_entry=True)
    assert failed['critical_contracts']['K3'] == failed['critical_contracts']['K5'] == 'failed'


def test_uninterrupted_quality_reading_keeps_true_no_trigger_na(auxiliary_packet, acceptance_public_root):
    from tests.support.agent_acceptance.quality_files import exercise_files
    outcome = asyncio.run(exercise_files(auxiliary_packet, 'QG1.formal.1', 'deepseek', root=acceptance_public_root))
    assert outcome.product_status == 'passed'
    assert outcome.critical_contracts['K3'] == outcome.critical_contracts['K5'] == 'not_applicable'


@pytest.mark.parametrize(('evidence', 'expected'), [(True, 'passed'), (False, 'failed'), (None, 'unknown')])
def test_memory_stop_contract_receiver_keeps_failed_and_missing_distinct(tmp_path, evidence, expected):
    from tests.support.agent_acceptance.core import CaseContext, CaseOutcome
    from tests.support.agent_acceptance.memory_isolation import finish
    outcome = CaseOutcome('failed', {'memory_contract': {'no_request_or_action_after_stop': evidence}})
    finish(CaseContext(tmp_path, 'deepseek', 'synthetic-criterion', 'formal.memory-withdrawal'), outcome)
    assert outcome.critical_contracts['K3'] == expected


def test_memory_without_a_stop_branch_keeps_no_trigger_na(tmp_path):
    from tests.support.agent_acceptance.core import CaseContext, CaseOutcome
    from tests.support.agent_acceptance.memory_isolation import finish
    outcome = CaseOutcome('failed', {'memory_contract': {}})
    finish(CaseContext(tmp_path, 'deepseek', 'synthetic-criterion', 'formal.memory-isolation'), outcome)
    assert outcome.critical_contracts['K3'] == 'not_applicable'


@pytest.mark.parametrize('case_id', ['queue.supplement-stopped-saved', 'queue.partial-stream-error'])
def test_actual_saved_queue_open_keeps_its_base_and_recovery_contracts(acceptance_public_root, case_id):
    from tests.support.agent_acceptance import AcceptancePlan, builtin_registry, run_plan
    outcome = asyncio.run(run_plan(AcceptancePlan(cases=[case_id]), root=acceptance_public_root, registry=builtin_registry())).cases[0]
    assert outcome.tool_status == 'passed', outcome.facts
    assert outcome.critical_contracts['K3'] == outcome.critical_contracts['K5'] == 'passed'
    if case_id == 'queue.partial-stream-error':
        assert outcome.product_status == 'failed'
