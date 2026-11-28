import asyncio
import json
from pathlib import Path
import sys

import httpx2
from openai import AsyncOpenAI
from pydantic_ai import models
from pydantic_ai import CancellationToken
import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.conversation import Conversation, Task, TaskScope
from myrk.permissions import ApprovalSettings
from tests.support.artifact_requests import ask_with_transport, reply


@pytest.mark.parametrize('delegated', [False, True])
def test_native_file_is_delivered_explicitly_and_reopens_with_the_same_identity(tmp_path: Path, monkeypatch, delegated):
    root = tmp_path / 'task'
    root.mkdir()
    path = tmp_path / 'records' / 'conversation.json'
    conversation = Conversation(path)
    events, sent = [], []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        child = any(message['role'] == 'user' and str(message['content']).startswith('受委派工作：') for message in payload['messages'])
        if delegated and not child:
            if not results:
                return reply('delegate_work', {'goal': '写报告并明确交付', 'context': '新建报告.md',
                    'evidence_requirements': '真实文件改动与明确交付', 'expected_files': ['报告.md']}, call_id='delegate')
            return reply()
        if not results:
            return reply('create_text', {'path': '报告.md', 'content': '交付正文\n'}, call_id='create')
        if len(results) == 1:
            assert not any(turn.artifacts for turn in conversation.turns)
            return reply('describe_extension', {'name': 'deliver_files'}, call_id='describe')
        if len(results) == 2:
            definition = json.loads(results[-1]['content'])
            if definition.get('status') != 'ok':
                return reply(text='成果登记入口尚不可用。')
            change = json.loads(results[0]['content'].split('文件改动证据：', 1)[1])
            return reply('call_extension', {'name': 'deliver_files', 'schema_version': definition['schema_version'],
                'arguments': {'change_ids': [change['id']]}}, call_id='deliver')
        return reply()

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com',
                    http_client=http, max_retries=0) as client:
                return await conversation.ask('创建报告并明确交付。', document=None,
                    scope=TaskScope(root=str(root), allowed_actions=['read', 'edit'],
                        provider='deepseek', recipients=['deepseek']),
                    model=OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)),
                    on_update=events.append)

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    turn = asyncio.run(scenario())
    assert turn.status == 'completed', turn.error
    assert len(turn.artifacts) == 1
    artifact = turn.artifacts[0]
    assert artifact.name == '报告.md'
    assert artifact.path == str(root / '报告.md')
    assert artifact.change_kind == 'created'
    assert bool(artifact.delegation_id) == delegated
    assert artifact.source_record_path == (turn.delegations[0].record_path if delegated else str(path))
    source = next(action for action in turn.actions if action.id == artifact.source_action_id)
    assert source.status == source.effect == 'completed'
    assert artifact.file_change_id == source.file_changes[0].id
    assert artifact.delivered_sha256 == source.file_changes[0].after_sha256
    assert artifact.size_bytes == len('交付正文\n'.encode('utf-8'))
    assert artifact.task_id == turn.task.id
    assert (root / '报告.md').read_text(encoding='utf-8') == '交付正文\n'
    assert Conversation.open(path).turns[-1].artifacts == turn.artifacts
    assert any(event.kind == 'artifacts' and event.artifacts == turn.artifacts for event in events)
    assert all('deliver_files' not in [tool['function']['name'] for tool in request['tools']] for request in sent)


@pytest.mark.parametrize('existed', [False, True])
def test_declared_command_output_requires_real_change_and_explicit_delivery(tmp_path, monkeypatch, existed):
    root = tmp_path / 'task'
    root.mkdir()
    if existed:
        (root / '报告.txt').write_bytes(b'original-command-file')
    conversation = Conversation(tmp_path / 'records' / 'conversation.json')
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if not results:
            return reply('describe_extension', {'name': 'run_command_with_outputs'}, call_id='describe-command')
        if len(results) == 1:
            definition = json.loads(results[-1]['content'])
            if definition.get('status') != 'ok':
                return reply(text='命令成果观察入口尚不可用。')
            return reply('call_extension', {'name': definition['name'], 'schema_version': definition['schema_version'],
                'arguments': {'program': sys.executable, 'arguments': ['-c', "from pathlib import Path; Path('报告.txt').write_text('命令正文', encoding='utf-8'); Path('临时.txt').write_text('不交付', encoding='utf-8')"],
                    'output_paths': ['报告.txt']}}, call_id='command')
        if len(results) == 2:
            return reply('describe_extension', {'name': 'deliver_files'}, call_id='describe-delivery')
        if len(results) == 3:
            changes = json.loads(results[1]['content'].split('文件改动证据：', 1)[1])
            definition = json.loads(results[-1]['content'])
            return reply('call_extension', {'name': 'deliver_files', 'schema_version': definition['schema_version'],
                'arguments': {'change_ids': [change['id'] for change in changes]}}, call_id='deliver')
        return reply()

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    turn = asyncio.run(ask_with_transport(conversation, root, respond))
    assert turn.status == 'completed', turn.error
    assert len(turn.artifacts) == 1
    assert turn.artifacts[0].path == str(root / '报告.txt')
    assert (root / '临时.txt').exists()
    source = next(action for action in turn.actions if action.kind == 'command')
    assert source.tool_name == 'run_command_with_outputs' and source.tool_source == 'extension'
    assert source.prepared_parameters['output_paths'] == [str(root / '报告.txt')]
    if existed:
        assert source.file_changes[0].change_kind == 'modified'
        assert Path(source.file_changes[0].backup_path).read_bytes() == b'original-command-file'
        assert source.prepared_parameters['output_before'][str(root / '报告.txt')] == source.file_changes[0].before_sha256
    else:
        assert source.file_changes[0].change_kind == 'created'
        assert source.prepared_parameters['output_before'] == {str(root / '报告.txt'): None}
    assert source.cleanup_complete and source.exit_code == 0
    assert len(source.file_changes) == 1
    assert all('run_command_with_outputs' not in [tool['function']['name'] for tool in request['tools']] for request in sent)


def test_deliveries_keep_each_real_version_and_repeated_delivery_keeps_its_id(tmp_path, monkeypatch):
    root = tmp_path / 'task'
    root.mkdir()
    conversation = Conversation(tmp_path / 'records' / 'conversation.json')

    def respond(request):
        results = [item for item in json.loads(request.content)['messages'] if item['role'] == 'tool']
        if not results:
            return reply('create_text', {'path': 'report.txt', 'content': '版本一'}, call_id='create')
        if len(results) == 1:
            return reply('describe_extension', {'name': 'deliver_files'}, call_id='describe')
        if len(results) in (2, 3, 6):
            definition = json.loads(results[1]['content'])
            source = results[5] if len(results) == 6 else results[0]
            change = json.loads(source['content'].split('文件改动证据：', 1)[1])
            return reply('call_extension', {'name': 'deliver_files', 'schema_version': definition['schema_version'],
                'arguments': {'change_ids': [change['id']]}}, call_id=f'deliver-{len(results)}')
        if len(results) == 4:
            return reply('read_text', {'path': 'report.txt'}, call_id='read')
        if len(results) == 5:
            return reply('edit_text', {'path': 'report.txt', 'old_text': '版本一', 'new_text': '版本二'}, call_id='edit')
        return reply()

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    turn = asyncio.run(ask_with_transport(conversation, root, respond))
    assert turn.status == 'completed', turn.error
    assert len(turn.artifacts) == 2
    first, second = turn.artifacts
    assert first.id != second.id and first.path == second.path
    assert [item.change_kind for item in turn.artifacts] == ['created', 'modified']
    deliveries = [json.loads(action.output)['artifacts'][0]['id'] for action in turn.actions if action.kind == 'deliver']
    assert deliveries == [first.id, first.id, second.id]
    source = next(action for action in turn.actions if action.id == second.source_action_id)
    assert source.file_changes[0].before_sha256 == first.delivered_sha256
    assert Path(source.file_changes[0].backup_path).read_text(encoding='utf-8') == '版本一'
    assert Conversation.open(conversation.path).turns[-1].artifacts == turn.artifacts


@pytest.mark.parametrize('behavior', ['unchanged', 'missing', 'failed'])
def test_command_success_alone_and_failed_commands_never_supply_file_change_evidence(tmp_path, monkeypatch, behavior):
    root = tmp_path / 'task'
    root.mkdir()
    if behavior == 'unchanged':
        (root / 'report.txt').write_text('原件', encoding='utf-8')
    conversation = Conversation(tmp_path / 'records' / 'conversation.json')
    command = "from pathlib import Path; Path('report.txt').write_text('失败时写出的部分内容', encoding='utf-8'); raise SystemExit(7)" if behavior == 'failed' else 'pass'

    def respond(request):
        results = [item for item in json.loads(request.content)['messages'] if item['role'] == 'tool']
        if not results:
            return reply('describe_extension', {'name': 'run_command_with_outputs'}, call_id='describe')
        if len(results) == 1:
            definition = json.loads(results[-1]['content'])
            return reply('call_extension', {'name': definition['name'], 'schema_version': definition['schema_version'],
                'arguments': {'program': sys.executable, 'arguments': ['-c', command], 'output_paths': ['report.txt']}}, call_id='command')
        return reply()

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    turn = asyncio.run(ask_with_transport(conversation, root, respond))
    action = next(action for action in turn.actions if action.kind == 'command')
    assert not action.file_changes and not turn.artifacts
    assert action.status == ('failed' if behavior == 'failed' else 'completed')
    assert action.cleanup_complete


@pytest.mark.parametrize('outcome', ['denied', 'stopped', 'changed_during_approval', 'not_granted'])
def test_output_declaration_preserves_actual_write_scope_and_exact_approval(tmp_path, monkeypatch, outcome):
    root = tmp_path / 'task'
    root.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_text('批准前原件', encoding='utf-8')
    marker = root / 'started.txt'
    conversation = Conversation(tmp_path / 'records' / 'conversation.json')
    token = CancellationToken()
    pending = []

    async def approve(action):
        pending.append(action)
        assert action.tool_name == 'run_command_with_outputs'
        assert action.prepared_parameters['output_before'][str(outside)]
        if outcome == 'stopped':
            token.cancel()
        if outcome == 'changed_during_approval':
            outside.write_text('批准期间外部修改', encoding='utf-8')
        return outcome != 'denied'

    def respond(request):
        results = [item for item in json.loads(request.content)['messages'] if item['role'] == 'tool']
        if not results:
            return reply('describe_extension', {'name': 'run_command_with_outputs'}, call_id='describe')
        if len(results) == 1:
            definition = json.loads(results[-1]['content'])
            arguments = {'program': sys.executable,
                'arguments': ['-c', f"from pathlib import Path; Path({str(outside)!r}).write_text('不应写入', encoding='utf-8'); Path('started.txt').write_text('started')"],
                'output_paths': [str(outside)]}
            if outcome != 'not_granted':
                arguments['additional_write_paths'] = [str(outside)]
            return reply('call_extension', {'name': definition['name'], 'schema_version': definition['schema_version'],
                'arguments': arguments}, call_id='command')
        return reply()

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    turn = asyncio.run(ask_with_transport(conversation, root, respond, approve_action=approve, cancellation_token=token))
    action = next(action for action in turn.actions if action.kind == 'command')
    assert not action.file_changes and not turn.artifacts
    assert not marker.exists()
    assert outside.read_text(encoding='utf-8') == ('批准期间外部修改' if outcome == 'changed_during_approval' else '批准前原件')
    assert bool(pending) == (outcome != 'not_granted')
    assert action.status == {'denied': 'denied', 'stopped': 'cancelled', 'changed_during_approval': 'conflict', 'not_granted': 'failed'}[outcome]


@pytest.mark.parametrize('outcome', ['changed', 'missing', 'forged', 'stopped', 'last_round', 'save_failed'])
def test_unverified_or_interrupted_delivery_never_registers_a_reliable_artifact(tmp_path, monkeypatch, outcome):
    root = tmp_path / 'task'
    root.mkdir()
    path = tmp_path / 'records' / 'conversation.json'
    conversation = Conversation(path)
    token = CancellationToken()
    events = []

    def observe(event):
        events.append(event)
        if outcome == 'save_failed' and event.action is not None and event.action.kind == 'deliver' and event.action.status == 'started':
            path.with_suffix('.json.tmp').mkdir()

    def respond(request):
        results = [item for item in json.loads(request.content)['messages'] if item['role'] == 'tool']
        if not results:
            return reply('create_text', {'path': 'report.txt', 'content': '真实文件'}, call_id='create')
        if len(results) == 1:
            return reply('describe_extension', {'name': 'deliver_files'}, call_id='describe')
        if len(results) == 2:
            change = json.loads(results[0]['content'].split('文件改动证据：', 1)[1])
            definition = json.loads(results[1]['content'])
            if outcome == 'changed':
                (root / 'report.txt').write_text('外部版本', encoding='utf-8')
            if outcome == 'missing':
                (root / 'report.txt').unlink()
            if outcome == 'stopped':
                token.cancel()
            return reply('call_extension', {'name': 'deliver_files', 'schema_version': definition['schema_version'],
                'arguments': {'change_ids': ['forged-file-change' if outcome == 'forged' else change['id']]}}, call_id='deliver')
        return reply()

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    task = Task(goal='处理并明确交付报告。', scope=TaskScope(root=str(root), allowed_actions=['read', 'edit'],
        provider='deepseek', recipients=['deepseek']), max_turns=3 if outcome == 'last_round' else None)
    if outcome == 'save_failed':
        with pytest.raises(OSError):
            asyncio.run(ask_with_transport(conversation, root, respond, task=task,
                cancellation_token=token, on_update=observe))
        turn = conversation.turns[-1]
    else:
        turn = asyncio.run(ask_with_transport(conversation, root, respond, task=task,
            cancellation_token=token, on_update=observe))
    assert not turn.artifacts
    assert not any(event.kind == 'artifacts' for event in events)
    assert not Conversation.open(path).turns[-1].artifacts
    assert any(action.kind == 'create' and action.status == 'completed' for action in turn.actions)
    if outcome == 'save_failed':
        assert conversation.save_failures


@pytest.mark.parametrize('behavior', ['read', 'same_content', 'answer_only'])
def test_reading_unchanged_writes_and_answer_paths_are_not_delivered_files(tmp_path, monkeypatch, behavior):
    root = tmp_path / 'task'
    root.mkdir()
    (root / 'input.txt').write_text('原始全文', encoding='utf-8')
    conversation = Conversation(tmp_path / 'records' / 'conversation.json')

    def respond(request):
        results = [item for item in json.loads(request.content)['messages'] if item['role'] == 'tool']
        if not results and behavior != 'answer_only':
            return reply('read_text', {'path': 'input.txt'}, call_id='read')
        if len(results) == 1 and behavior == 'same_content':
            return reply('write_text', {'path': 'input.txt', 'content': '原始全文'}, call_id='write')
        return reply(text=f'最终文件是 [{root / "input.txt"}]({root / "input.txt"})，此说法不能登记文件。')

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    turn = asyncio.run(ask_with_transport(conversation, root, respond))
    assert turn.status == 'completed', turn.error
    assert not turn.artifacts and not any(action.file_changes for action in turn.actions)
    assert not Conversation.open(conversation.path).turns[-1].artifacts
    assert (root / 'input.txt').read_text(encoding='utf-8') == '原始全文'
