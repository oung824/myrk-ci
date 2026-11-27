"""The small review refactors keep task choices and original native state."""
import asyncio
import json
from pathlib import Path
import pytest


@pytest.mark.parametrize('selection', ['keep', 'document', 'scope'])
def test_task_continuation_preserves_spent_work_and_selects_only_current_inputs(tmp_path, selection):
    from myrk.conversation import Supplement, Task, TaskScope
    from myrk.permissions import ApprovalSettings
    from myrk.provider import ProviderConfig
    from myrk.task_control import WorkStep
    original_scope = TaskScope(root=str(tmp_path / 'old'), allowed_actions=['read'], provider='deepseek', recipients=['deepseek'])
    original = Task(goal='Continue the same saved verification', scope=original_scope, request_limit=9, requests=4,
        total_requests=6, max_turns=8, turns_used=3, work_step=WorkStep(capacity_recovery_used=True),
        supplements=[Supplement(text='Only this saved source remains applicable', applied_request=3)], memory_reviews=1,
        reasoning='high', legacy_thinking=None)
    snapshot = original.model_dump(mode='json')
    scope = original_scope.model_copy(update={'root': str(tmp_path / 'new')}) if selection == 'scope' else None
    document = str(tmp_path / 'current.txt') if selection == 'document' else None
    updated = original.continuation(scope=scope, document=document, approval=ApprovalSettings())
    assert updated.scope == (None if selection == 'document' else scope if selection == 'scope' else original_scope)
    assert updated.document == document
    assert updated.id == original.id and updated.requests == 4 and updated.total_requests == 6
    assert updated.max_turns == 8 and updated.turns_used == 3 and updated.request_limit == 9
    assert updated.work_step.id == original.work_step.id and updated.work_step.capacity_recovery_used
    assert updated.supplements == original.supplements and updated.memory_reviews == 1
    assert updated.reasoning == 'high'
    desktop = original.continuation(scope=scope, document=document, approval=ApprovalSettings(),
        provider_config=ProviderConfig(model='deepseek-flash'), reasoning=None, legacy_thinking=False)
    assert desktop.provider_config.model == 'deepseek-flash' and desktop.reasoning is None and desktop.legacy_thinking is False
    assert desktop.id == original.id and desktop.work_step.id == original.work_step.id
    assert original.model_dump(mode='json') == snapshot


def test_public_native_service_continuation_preserves_the_old_turn_and_step(acceptance_public_root):
    from tests.support.agent_acceptance.core import CaseContext
    from tests.support.agent_acceptance.service_channel import ServiceChannel
    from tests.support.agent_acceptance.replay import Reply
    root = acceptance_public_root; root.mkdir(parents=True)
    for name in ('old', 'new'): (root / name).mkdir()
    def scope(name):
        return {'root': str(root / name), 'allowed_actions': ['read'], 'provider': 'deepseek', 'recipients': ['deepseek']}
    async def run():
        context = CaseContext(root, 'deepseek', 'synthetic-continuation-refactor', 'synthetic-continuation-refactor')
        async with ServiceChannel(context, 'service', [Reply(text='Held original source observation.')], gates=[0]) as channel:
            await channel.configure()
            await channel.send({'action': 'send', 'prompt': 'Report the selected source only.', 'scope': scope('old')})
            started = await channel.receive('started'); await channel.arrived(0)
            await channel.send({'action': 'stop', 'path': started['path']})
            await channel.receive('stopping'); stopped = await channel.receive('finished', task_id=started['task_id'])
            assert stopped['turn']['status'] == 'cancelled'
            original = Path(started['path']).read_bytes()
            (root / 'stopped-before-continuation.original.json').write_bytes(original)
        async with ServiceChannel(context, 'reopened', [Reply(text='Continued only the same unfinished source task.')]) as channel:
            await channel.configure()
            await channel.send({'action': 'open', 'path': started['path']})
            await channel.receive('conversation')
            await channel.send({'action': 'send', 'path': started['path'], 'prompt': 'Continue the same task with the newly selected source.', 'scope': scope('new')})
            continued = await channel.receive('started')
            pending = await channel.receive('action')
            action = pending['action']
            assert action['kind'] == 'history_reuse' and action['status'] == 'awaiting_approval'
            assert action['task_id'] == started['task_id'] and action['target'] == str(root / 'new')
            await channel.send({'action': 'approve_action', 'path': pending['path'], 'task_id': pending['task_id'],
                'action_id': action['id'], 'approval_id': action['approval_id'], 'approved': True})
            finished = await channel.receive('finished', task_id=continued['task_id'])
        saved = json.loads(Path(started['path']).read_bytes())
        assert finished['turn']['status'] == 'completed'
        assert continued['task_id'] == started['task_id']
        assert saved['turns'][0] == json.loads(original)['turns'][0]
        before, after = saved['turns'][0]['task'], saved['turns'][1]['task']
        assert after['work_step']['id'] == before['work_step']['id']
        assert after['requests'] > before['requests'] and after['total_requests'] > before['total_requests']
        assert after['scope'] == scope('new') | {'checks': []}
        assert after['approval']['mode'] == before['approval']['mode']
        assert after['approval']['access']['writable_roots'] == [str(root / 'new')]
    asyncio.run(run())


@pytest.mark.parametrize('case_id', ['formal.provider-settings', 'formal.user-approval', 'formal.memory-review',
    'formal.queue-supplement', 'formal.delegation', 'formal.compaction-material', 'formal.restart'])
def test_public_formal_projection_keeps_each_domain_and_legacy_restart(tmp_path, case_id):
    from tests.support.agent_acceptance.formal_scenarios import manifest, replies, setup
    setup(case_id, tmp_path, 'deepseek')
    request = manifest(case_id, tmp_path, 'deepseek')
    assert request['case_id'] == case_id
    if case_id == 'formal.provider-settings':
        assert request['connection_adapter'] is True and request['protocol'] == 'deepseek'
    else:
        assert request['max_attempts'] > 0
    assert replies(case_id, tmp_path, 'deepseek')
    if case_id == 'formal.restart':
        assert (tmp_path / 'materials/notes.txt').read_bytes() == b'original-material-5317\n'
        assert request['phases'] == ['work', 'reopen']


def test_invalid_invoice_source_keeps_the_public_unsupported_fallback():
    from tests.support.agent_acceptance.quality_runtime import QualitySource, QualityVisibleInput
    from tests.support.agent_acceptance import quality_recovery
    source = QualityVisibleInput(task_id='QG5.formal.2', adapter='qg5-2', goal='Read only the actual independent invoice originals',
        sources={'source.txt': QualitySource(text='Only invoice F18 is present; no second or third invoice exists.')})
    assert quality_recovery.supports(source) is False
    assert quality_recovery.allowed_paths(source) == []
    assert quality_recovery.current_goal(source) == 'Execute only this task\'s current explicit user instruction; future workflow events grant no current authority.'
    assert quality_recovery.project_input(source) == source
