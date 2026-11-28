import asyncio
import json

from pydantic_ai import models

from myrk.conversation import Conversation
from tests.support.artifact_requests import ask_with_transport, reply


def test_old_v9_record_is_backed_up_without_inventing_artifacts_from_saved_paths(tmp_path):
    path = tmp_path / 'conversation.json'
    original = json.dumps({'version': 9, 'messages': [], 'turns': [{
        'prompt': '原问题', 'answer': '已写好 E:/报告.txt', 'status': 'failed',
        'actions': [{'task_id': 'old-task', 'kind': 'create', 'target': 'E:/报告.txt',
            'status': 'completed', 'effect': 'completed', 'after_sha256': 'a' * 64}]}]}, ensure_ascii=False).encode('utf-8')
    path.write_bytes(original)
    conversation = Conversation.open(path)
    conversation.save()
    conversation.save()
    assert not conversation.turns[0].artifacts
    assert not conversation.turns[0].actions[0].file_changes
    assert json.loads(path.read_text(encoding='utf-8'))['version'] == 10
    backups = list((tmp_path / 'backups').glob('conversation-v9-*.json'))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert not Conversation.open(backups[0]).turns[0].artifacts


def test_sdk_instructions_explain_discovery_before_command_outputs_and_explicit_delivery(tmp_path, monkeypatch):
    root = tmp_path / 'task'
    root.mkdir()
    path = tmp_path / 'records' / 'conversation.json'
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return reply(text='已了解本次文件交付要求。')

    async def scenario():
        first = await ask_with_transport(Conversation(path), root, respond)
        reopened = Conversation.open(path)
        second = await ask_with_transport(reopened, root, respond, task=reopened.turns[-1].task)
        assert first.status == second.status == 'completed'

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    instructions = '\n'.join(str(message['content']) for message in sent[0]['messages'] if message['role'] == 'system')
    assert 'run_command_with_outputs' in instructions
    assert 'search_extensions' in instructions and 'describe_extension' in instructions and 'call_extension' in instructions
    assert '执行前声明' in instructions and 'deliver_files' in instructions and 'change_ids' in instructions
    assert all(word in instructions for word in ['读入资料', '备份', '临时文件', '日志', '不能事后'])
    assert sent[1]['messages'][:len(sent[0]['messages'])] == sent[0]['messages']
    assert sent[1]['tools'] == sent[0]['tools']
    assert Conversation.open(path).turns[-1].request_attempts[0].observation.layout_version == 1


def test_reopening_history_without_delivery_guidance_appends_it_without_editing_old_messages(tmp_path, monkeypatch):
    root = tmp_path / 'task'
    root.mkdir()
    path = tmp_path / 'records' / 'conversation.json'
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return reply(text='继续原任务。')

    async def scenario():
        await ask_with_transport(Conversation(path), root, respond)
        record = json.loads(path.read_text(encoding='utf-8'))
        removed = 0
        for message in record['messages']:
            for part in message['parts']:
                if part['part_kind'] == 'instruction-baseline':
                    previous = part['instructions']
                    part['instructions'] = [block for block in previous if not str(block.get('id', '')).endswith('artifact-delivery')]
                    removed += len(previous) - len(part['instructions'])
        assert removed == 1
        path.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        original_messages = record['messages']
        reopened = Conversation.open(path)
        turn = await ask_with_transport(reopened, root, respond, task=reopened.turns[-1].task)
        assert turn.status == 'completed', turn.error
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['messages'][:len(original_messages)] == original_messages
        additions = [part for message in saved['messages'][len(original_messages):] for part in message['parts']]
        assert any(part['part_kind'] == 'instruction-delta' and 'run_command_with_outputs' in str(part.get('content'))
            for part in additions)
        assert turn.request_attempts[0].observation.layout_version == 1

    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    assert sent[1]['tools'] == sent[0]['tools']
    assert 'run_command_with_outputs' in json.dumps(sent[1]['messages'], ensure_ascii=False)
