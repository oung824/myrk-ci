"""One bounded memory review may contain multiple legal SDK tool requests."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4
import pytest
from tests.acceptance.test_quality_memory_aux import auxiliary_packet


async def native_review(reference, task_id, root, *, list_requests=1, request_limit=None, extra_save=False, stop_during_review=False, second_review=False, wrong_scope=False, recoverable_retry=False):
    from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
    from myrk.permissions import ApprovalSettings
    from tests.support.agent_acceptance.formal import provider_config
    from tests.support.agent_acceptance.quality_memory_aux import replies, score, memory_scope
    from tests.support.agent_acceptance.quality_runtime import bind_packet, manifest
    from tests.support.agent_acceptance.replay import OfflineSession, Reply
    root.mkdir(parents=True, exist_ok=False)
    visible = bind_packet(reference, task_id, root)
    request = manifest('quality.' + task_id.lower(), root, 'deepseek')
    script = replies(visible, root, 'deepseek')
    first_save = next(index for index, row in enumerate(script) if row.tool == 'memory')
    if wrong_scope:
        script[first_save] = Reply(tool='memory', arguments={'action': 'add', 'scope': 'personal', 'text': visible.parameters['original']})
    script[first_save:first_save] = [Reply(tool='memory', arguments={'action': 'list', 'scope': memory_scope(visible)}) for _ in range(list_requests)]
    if recoverable_retry:
        script[first_save:first_save] = [Reply(status=503)]
    if extra_save:
        script[-1:-1] = [Reply(tool='memory', arguments={'action': 'add', 'scope': memory_scope(visible), 'text': 'Unsupported extra experience from this fixture.'})]
    from pydantic_ai import CancellationToken
    from myrk.request_observation import current_request_attempt
    token = CancellationToken()
    class BoundarySession(OfflineSession):
        def respond(self, incoming):
            response = super().respond(incoming)
            if stop_during_review and current_request_attempt().actor == 'memory_review':
                token.cancel()
            return response
    session = BoundarySession('deepseek', script * (2 if second_review else 1), config=provider_config('deepseek'))
    conversation = Conversation(root / 'records/work.json')
    task = Task(goal=request['prompt'], scope=TaskScope.model_validate(request['scope']),
        approval=ApprovalSettings.model_validate(request['approval']), request_limit=len(script) if request_limit is None else request_limit,
        provider_config=session.config)
    async with session.model() as model:
        await conversation.ask(task.goal, document=None, task=task, scope=task.scope, model=model, cancellation_token=token)
        if second_review:
            next_task = Task(goal=request['prompt'], scope=task.scope, approval=task.approval.model_copy(deep=True),
                request_limit=len(script), provider_config=session.config)
            await conversation.ask(next_task.goal, document=None, task=next_task, scope=next_task.scope, model=model, new_task=True)
    record = ConversationRecord.model_validate_json(conversation.path.read_bytes()).model_dump(mode='json')
    exchange = session.save(root / 'exchange.json', attempts=[{'task_id': task.id, **item} for turn in record['turns'] for item in turn['request_attempts']],
        run_id=uuid4().hex, case_id='quality.' + task_id.lower(), task_id=task.id)
    envelope = json.loads(exchange.read_bytes())
    facts = score(visible, root, [record], [envelope])
    (root / 'program-facts.json').write_text(json.dumps(facts, ensure_ascii=False), encoding='utf-8')
    return visible, record, envelope, facts


@pytest.mark.parametrize('task_id', ['QG4.formal.4', 'QG4.tuning.2'])
def test_one_bounded_review_can_list_then_add_then_finish_with_three_native_requests(auxiliary_packet, acceptance_public_root, task_id):
    from myrk.memory import MemoryStore
    from tests.support.agent_acceptance.quality_memory_aux import memory_scope
    visible, record, envelope, facts = asyncio.run(native_review(auxiliary_packet, task_id, acceptance_public_root))
    task = record['turns'][-1]['task']
    review = [row for row in record['turns'][-1]['request_attempts'] if row['actor'] == 'memory_review']
    assert task['memory_reviews'] == 1 and len(review) == 3
    assert all(row['status'] == 'completed' for row in review)
    actual = MemoryStore(acceptance_public_root / 'records/memories').read().facts
    assert [(row.text, row.scope) for row in actual] == [(visible.parameters['original'], memory_scope(visible))]
    assert task['requests'] == task['request_limit'] == len(envelope['exchanges'])
    assert facts['passed'] is True, facts


@pytest.mark.parametrize('list_requests', [0, 2])
def test_one_operation_accepts_two_or_four_review_requests_within_its_actual_task_cap(auxiliary_packet, acceptance_public_root, list_requests):
    _, _, _, facts = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', acceptance_public_root, list_requests=list_requests))
    contract = facts['quality_memory_contract']
    assert facts['passed'] and contract['bounded_review_status'] == contract['request_budget_status'] == 'passed'
    assert contract['review_request_count'] == list_requests + 2
    assert contract['memory_review_operations'] == contract['memory_mutation_count'] == 1


def test_review_shares_main_requests_and_cannot_finish_after_the_saved_cap(auxiliary_packet, acceptance_public_root):
    _, record, envelope, facts = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', acceptance_public_root, request_limit=5))
    assert record['turns'][-1]['task']['requests'] == len(envelope['exchanges']) == 5
    assert len([row for row in record['turns'][-1]['request_attempts'] if row['actor'] == 'memory_review']) == 2
    assert facts['passed'] is False
    assert facts['quality_memory_contract']['review_completed'] is False
    assert facts['quality_memory_contract']['bounded_review_status'] == 'failed'


def test_missing_request_observations_stay_unknown_and_counterevidence_wins(auxiliary_packet, acceptance_public_root):
    from tests.support.agent_acceptance.quality_memory_aux import score
    visible, record, envelope, _ = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', acceptance_public_root))
    (acceptance_public_root / 'complete-record.original.json').write_text(json.dumps(record), encoding='utf-8')
    missing = score(visible, acceptance_public_root, [record], [])
    assert missing['passed'] is False
    assert missing['quality_memory_contract']['request_budget_status'] == 'unknown'
    absent = deepcopy(record); absent['turns'][-1]['task']['request_limit'] = None
    unknown = score(visible, acceptance_public_root, [absent], [envelope])
    assert unknown['quality_memory_contract']['bounded_review_status'] == 'unknown'
    changed = deepcopy(record); changed['turns'][-1]['task']['request_limit'] = 4
    failed = score(visible, acceptance_public_root, [changed], [])
    assert failed['quality_memory_contract']['request_budget_status'] == 'failed'
    assert failed['critical_contracts']['K1'] == 'failed'


def test_second_actual_review_operation_and_extra_fact_are_rejected(auxiliary_packet, acceptance_public_root):
    root = acceptance_public_root; root.mkdir(parents=True)
    _, _, _, twice = asyncio.run(native_review(auxiliary_packet, 'QG4.formal.4', root / 'two-operations', second_review=True))
    assert twice['passed'] is False
    assert twice['quality_memory_contract']['memory_review_operations'] == 2
    assert twice['quality_memory_contract']['bounded_review_status'] == 'failed'
    _, _, _, extra = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', root / 'extra-save', extra_save=True))
    assert extra['passed'] is False and extra['critical_contracts']['K1'] == 'failed'
    assert extra['quality_memory_contract']['memory_mutation_count'] == 2


def test_public_stop_during_review_retains_the_actual_cap_and_starts_no_following_request(auxiliary_packet, acceptance_public_root):
    from myrk.memory import MemoryStore
    _, record, envelope, facts = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', acceptance_public_root, stop_during_review=True))
    assert facts['passed'] is False
    assert len(envelope['exchanges']) == record['turns'][-1]['task']['requests'] == 4
    assert record['turns'][-1]['status'] == 'completed'
    assert any('复盘未完成' in note for note in record['turns'][-1]['context_notes'])
    assert len([row for row in record['turns'][-1]['request_attempts'] if row['actor'] == 'memory_review']) == 1
    assert MemoryStore(acceptance_public_root / 'records/memories').read().facts == []


def test_project_review_cannot_save_its_fact_into_personal_memory(auxiliary_packet, acceptance_public_root):
    from myrk.memory import MemoryStore
    _, _, _, facts = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', acceptance_public_root, wrong_scope=True))
    assert facts['passed'] is False and facts['critical_contracts']['K1'] == 'failed'
    assert facts['quality_memory_contract']['namespace_preserved'] is False
    assert [fact.scope for fact in MemoryStore(acceptance_public_root / 'records/memories').read().facts] == ['personal']


def test_recoverable_review_retry_consumes_the_original_cap_without_becoming_a_second_operation(auxiliary_packet, acceptance_public_root):
    _, record, envelope, facts = asyncio.run(native_review(auxiliary_packet, 'QG4.tuning.2', acceptance_public_root, recoverable_retry=True))
    review = [row for row in record['turns'][-1]['request_attempts'] if row['actor'] == 'memory_review']
    assert any(row['status'] == 'failed' for row in review) and review[-1]['status'] == 'completed'
    assert record['turns'][-1]['task']['requests'] == record['turns'][-1]['task']['request_limit'] == len(envelope['exchanges'])
    assert facts['passed'] is True, facts
    assert facts['quality_memory_contract']['memory_review_operations'] == facts['quality_memory_contract']['memory_mutation_count'] == 1
