"""Actual later adoption is checked against each real prepared read, never a final flag."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4
import pytest
from tests.acceptance.test_quality_compaction_sources import projected_task


async def adopted_source_run(visible, root, request):
    from myrk.conversation import Conversation, ConversationRecord, Supplement, Task, TaskScope
    from myrk.permissions import ApprovalSettings
    from myrk.request_observation import current_request_attempt
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.formal_scenarios import scenario_config
    from tests.support.agent_acceptance.quality_runtime import terminal_mock
    from tests.support.agent_acceptance.replay import OfflineSession
    case_id = 'quality.' + visible.task_id.lower()
    config = scenario_config(case_id, provider_config('deepseek'))
    fixture = terminal_mock(case_id, root, 'deepseek', config)
    class NativeSession(OfflineSession):
        def respond(self, incoming):
            attempt = current_request_attempt()
            assert attempt is not None
            return fixture.respond(incoming, self.sdk_inputs[-1], attempt.purpose)
    session = NativeSession('deepseek', [], config=config)
    session.exchanges = fixture.terminal.exchanges
    conversation = Conversation(root / 'records/adoption.json')
    task = Task(goal=request['prompt'], scope=TaskScope.model_validate(request['scope']),
        approval=ApprovalSettings.model_validate(request['approval']), provider_config=config, request_limit=60)
    sent = False
    def updated(event):
        nonlocal sent
        completed = [summary for summary in conversation.compaction_status() if summary['status'] == 'completed']
        if completed and not sent and request['quality_compaction']['adoption']:
            task.supplements.append(Supplement(text=request['quality_compaction']['adoption']))
            sent = True
    async with session.model() as model:
        turn = await conversation.ask(task.goal, document=None, task=task, scope=task.scope, model=model, on_update=updated)
    assert turn.status == 'completed', turn.error
    assert (sent or not request['quality_compaction']['adoption']) and any(row.status == 'completed' for row in turn.compactions)
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes()).model_dump(mode='json')
    exchange = session.save(root / 'adoption-exchange.json', attempts=[row.model_dump(mode='json') for row in turn.request_attempts],
        run_id=uuid4().hex, case_id=case_id, task_id=task.id)
    state = root / 'native/receiver/state.json'; state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'passed': turn.status == 'completed', 'observation_layer': 'conversation-sdk native probe, no Main execution', 'quality_compaction': {'adoption_sent_after_summary': sent}}), encoding='utf-8')
    return record, json.loads(exchange.read_bytes())


def test_adoption_after_a_summary_cannot_erase_an_earlier_comparison_read(acceptance_public_root):
    from tests.support.agent_acceptance.quality_compaction import score
    with projected_task('QG6.formal.3', acceptance_public_root) as (visible, request):
        record, exchange = asyncio.run(adopted_source_run(visible, acceptance_public_root, request))
        actual = score(visible, acceptance_public_root, [record], [exchange])
        assert actual['passed'] and actual['critical_contracts']['K1'] == 'passed', actual
        original = acceptance_public_root / 'adoption-before-timing-controls.original.json'
        original.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        turn = record['turns'][-1]
        adoption = next(row for row in turn['task']['supplements'] if row['text'] == request['quality_compaction']['adoption'])
        names = set(visible.parameters['comparison_paths'])
        from pathlib import Path
        assert all(action['prepared_request'] >= adoption['applied_request'] for action in turn['actions'] if action['kind'] == 'read' and Path(action['target']).name in names)
        early = deepcopy(record)
        action = next(row for row in early['turns'][-1]['actions'] if row['kind'] == 'read' and Path(row['target']).name in names)
        action['prepared_request'] = adoption['applied_request'] - 1
        wrong = score(visible, acceptance_public_root, [early], [exchange])
        assert wrong['passed'] is False and wrong['critical_contracts']['K1'] == 'failed'
        missing = deepcopy(record)
        for action in missing['turns'][-1]['actions']:
            if action['kind'] == 'read' and Path(action['target']).name in names:
                action['prepared_request'] = None
        uncertain = score(visible, acceptance_public_root, [missing], [exchange])
        assert uncertain['passed'] is False and uncertain['critical_contracts']['K1'] == 'unknown'
        absent = deepcopy(record); absent['turns'][-1]['task']['supplements'] = []
        no_adoption = score(visible, acceptance_public_root, [absent], [exchange])
        assert no_adoption['passed'] is False and no_adoption['critical_contracts']['K1'] == 'failed'
        foreign = deepcopy(record)
        foreign['turns'][-1]['task']['id'] = 'other-task'
        mismatch = score(visible, acceptance_public_root, [foreign], [exchange])
        assert mismatch['passed'] is False and mismatch['critical_contracts']['K1'] == 'failed'

        pending = deepcopy(record)
        pending['turns'][-1]['task']['supplements'][0]['applied_request'] = None
        assert score(visible, acceptance_public_root, [pending], [exchange])['critical_contracts']['K1'] == 'failed'
        mixed = deepcopy(early)
        for action in mixed['turns'][-1]['actions']:
            if action['kind'] == 'read' and Path(action['target']).name == visible.parameters['comparison_paths'][-1]:
                action['prepared_request'] = None
        assert score(visible, acceptance_public_root, [mixed], [exchange])['critical_contracts']['K1'] == 'failed'
        missing_summary = deepcopy(record)
        missing_summary['turns'][-1]['compactions'][0]['request_numbers'] = []
        unknown_summary = score(visible, acceptance_public_root, [missing_summary], [exchange])
        assert unknown_summary['critical_contracts']['K1'] == 'unknown' and unknown_summary['passed'] is False


def test_weather_station_full_native_task_reports_the_proposal_without_replacing_sources(acceptance_public_root):
    from tests.support.agent_acceptance.quality_compaction import score
    with projected_task('QG6.tuning.1', acceptance_public_root) as (visible, request):
        record, exchange = asyncio.run(adopted_source_run(visible, acceptance_public_root, request))
        observed = score(visible, acceptance_public_root, [record], [exchange])
        assert observed['passed'], observed
        facts = observed['quality_compaction_contract']
        assert facts['source_facts'] == {'stations': 8, 'temperature_samples': 144}
        assert facts['replacement_proposal_read'] and facts['replacement_proposal_in_sdk']
        assert facts['replacement_proposal_state'] == 'unadopted_source'
        assert facts['replacement_effects_absent'] and facts['source_originals_unchanged']
        assert all(action['kind'] in {'read', 'list'} for action in observed['actions'])
        assert 'replacement-proposal.txt' in record['turns'][-1]['answer'] and 'unadopted' in record['turns'][-1]['answer']
        assert facts['semantics'].startswith('not_measured')
