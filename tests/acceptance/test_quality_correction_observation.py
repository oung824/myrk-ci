"""The real terminal boundary preserves a saved summary without a live file reader."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import os
from time import monotonic, sleep
import pytest
from copy import deepcopy
from tests.support.agent_acceptance.core import SharedControlError

from myrk.conversation import Conversation, ConversationRecord, Task, TaskScope
from myrk.provider import ProviderConfig
from tests.acceptance.test_quality_compaction_correction_instances import correction_packet
from tests.support.agent_acceptance.compaction import history
from tests.support.agent_acceptance.quality_compaction_correction import goals, terminal_response_barrier
from tests.support.agent_acceptance.quality_runtime import bind_packet
from tests.support.agent_acceptance.replay import OfflineSession, Reply


def observe_summary(correction_packet, acceptance_public_root, damage=None):
    root = acceptance_public_root
    root.mkdir(parents=True)
    visible = bind_packet(correction_packet, 'QG6.formal.9', root, protocol='deepseek')
    record_path = root / 'records/conversation.json'
    record_path.parent.mkdir()
    record_path.write_text(ConversationRecord(version=10, messages=history(), turns=[]).model_dump_json(), encoding='utf-8')
    conversation = Conversation.open(record_path)
    config = ProviderConfig(name='Synthetic saved-summary observation', protocol='deepseek',
        base_url='https://api.deepseek.com', model='deepseek-flash', context_window_override=26000,
        output_token_reserve=4096, context_safety_margin=512, compaction_trigger=.7,
        compaction_target=.55, compaction_recent=.1)
    task = Task(goal=goals(visible)['primary'], provider_config=config, request_limit=60,
        scope=TaskScope(root=str(root / 'materials'), allowed_actions=['read'], provider='deepseek', recipients=['deepseek']))
    captured = []
    errors = []

    class BoundarySession(OfflineSession):
        async def respond(self, request):
            response = super().respond(request)
            sdk = self.sdk_inputs[-1]
            actual_sdk = sdk
            if sdk['request_identity']['purpose'] == 'work' and damage:
                if damage == 'request':
                    actual_sdk = deepcopy(sdk)
                    actual_sdk['request_identity']['number'] += 1
                elif damage in {'owner', 'summary_order'}:
                    changed = json.loads(record_path.read_bytes())
                    if damage == 'owner':
                        changed['turns'][-1]['task']['id'] = 'different-saved-task-owner'
                    else:
                        changed['turns'][-1]['compactions'][0]['request_numbers'] = [sdk['request_identity']['number']]
                    record_path.write_text(json.dumps(changed), encoding='utf-8')
                elif damage == 'existing_source':
                    (root / 'review-only').mkdir(exist_ok=True)
                    (root / 'review-only' / 'quality-correction-summary-source.original.json').write_bytes(b'preexisting-original-must-remain')
            try:
                result = await terminal_response_barrier(visible, root, len(self.exchanges) - 1, actual_sdk, response)
            except SharedControlError as error:
                errors.append(str(error))
                raise
            if sdk['request_identity']['purpose'] == 'work':
                if not (root / 'review-only' / 'quality-correction-summary.ready').is_file():
                    captured.append({'missing_observation': True})
                    return result
                raw = record_path.read_bytes()
                record = ConversationRecord.model_validate_json(raw)
                old_path = root / 'quality-correction-old-summary.original.json'
                old_raw = old_path.read_bytes()
                old = json.loads(old_raw)
                observation = json.loads((root / 'review-only' / 'quality-correction-summary-observation.json').read_bytes())
                actual = next(item for turn in record.turns for item in turn.compactions if item.id == old['id'])
                assert old == actual.model_dump(mode='json')
                assert observation['source_record_sha256'] == sha256(raw).hexdigest()
                assert observation['source_record'] == str(record_path)
                assert observation['task_id'] == task.id == actual.task_id
                assert observation['sdk_input_id'] == sdk['sdk_input_id']
                assert observation['request_identity'] == sdk['request_identity']
                assert observation['summary_id'] == old['id'] and observation['summary_sha256'] == sha256(old_raw).hexdigest()
                assert max(old['request_numbers']) < observation['request_identity']['number']
                electron = Path('E:/Myrk/desktop/node_modules/electron/dist/electron.exe')
                program = "const fs=require('node:fs/promises');(async()=>{const h=await fs.open(process.argv[1],'r');await h.readFile();await fs.writeFile(process.argv[2],'ready',{flag:'wx'});process.stdin.once('data',async()=>{await h.close();process.stdin.pause()});process.stdin.resume()})()"
                reader_ready = root / 'review-only/reader-active.marker'
                process = subprocess.Popen([str(electron), '-e', program, str(old_path), str(reader_ready)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'})
                try:
                    deadline = monotonic() + 10
                    while not reader_ready.exists() and process.poll() is None and monotonic() < deadline:
                        sleep(.01)
                    assert reader_ready.exists(), 'Electron reader did not reach the bounded I/O observation'
                    conversation.save()
                finally:
                    _, error = process.communicate('\n', timeout=10)
                assert process.returncode == 0, error
                assert old_path.read_bytes() == old_raw
                captured.append(observation)
            return result

    session = BoundarySession('deepseek', [Reply(text='Historical volunteer-roster summary: shifts=12. Preserve original history.'),
        Reply(text='Synthetic terminal observation finished.')], config=config)
    async def run():
        async with session.model() as model:
            return await conversation.ask(task.goal, document=None, scope=task.scope, task=task, model=model)
    turn = asyncio.run(run())
    return root, task, session, turn, captured, errors


def test_saved_summary_is_published_before_correction_without_blocking_further_saves(correction_packet, acceptance_public_root):
    root, task, session, turn, captured, errors = observe_summary(correction_packet, acceptance_public_root)
    assert turn.status == 'completed', turn.error
    assert captured and 'missing_observation' not in captured[0], captured
    assert len(captured) == 1 and not errors
    assert len(session.exchanges) == len(session.sdk_inputs) == task.requests == 2
    assert [item.purpose for item in turn.request_attempts] == ['compaction', 'work']
    assert task.request_limit == 60 and turn.compactions[0].status == 'completed'
    driver = Path(__file__).parents[1] / 'support/agent_acceptance/desktop/quality-compaction-correction.mjs'
    program = "import(process.argv[1]).then(async m=>{const value=await m.readSavedSummaryObservation(process.argv[2],JSON.parse(process.argv[3]),process.argv[4]);console.log(value.summary.id)}).catch(e=>{console.error(e);process.exitCode=1})"
    primary = {'path': str(root / 'records/conversation.json'), 'task_id': task.id}
    argv = ['E:/Myrk/desktop/node_modules/electron/dist/electron.exe', '-e', program, driver.as_uri(), str(root), json.dumps(primary), turn.compactions[0].id]
    env = {**os.environ, 'ELECTRON_RUN_AS_NODE': '1'}
    result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    metadata = root / 'review-only' / 'quality-correction-summary-observation.json'
    original = metadata.read_bytes()
    for field, changed in [('source_record_sha256', '0' * 64), ('task_id', 'wrong-owner'),
            ('request_identity', {**captured[0]['request_identity'], 'number': 99})]:
        value = json.loads(original)
        value[field] = changed
        metadata.write_text(json.dumps(value), encoding='utf-8')
        rejected = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        assert rejected.returncode != 0, field
    metadata.write_bytes(original)
    summary_path = root / 'quality-correction-old-summary.original.json'
    summary_raw = summary_path.read_bytes()
    changed = json.loads(summary_raw)
    changed['view_fingerprint'] = '0' * 64
    altered = json.dumps(changed).encode()
    summary_path.write_bytes(altered)
    value = json.loads(original)
    value['summary_sha256'] = sha256(altered).hexdigest()
    metadata.write_text(json.dumps(value), encoding='utf-8')
    rejected = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
    assert rejected.returncode != 0, 'Full hidden summary fields must still match the saved source'
    metadata.write_bytes(original)
    summary_path.write_bytes(summary_raw)


@pytest.mark.parametrize('damage, message', [('request', 'SDK request identity'), ('owner', 'owner or saved request order'),
    ('summary_order', 'owner or saved request order'), ('existing_source', 'target already exists')])
def test_summary_observation_rejects_conflicting_saved_evidence_without_publishing(correction_packet, acceptance_public_root, damage, message):
    root, task, session, turn, captured, errors = observe_summary(correction_packet, acceptance_public_root, damage)
    assert turn.status == 'failed' and errors and message in errors[0]
    assert not captured and task.requests == len(session.sdk_inputs) == len(session.exchanges) == 2
    assert not (root / 'review-only' / 'quality-correction-summary.ready').exists()
    assert not (root / 'quality-correction-old-summary.original.json').exists()
    assert not (root / 'review-only' / 'quality-correction-summary-observation.json').exists()
    if damage == 'existing_source':
        assert (root / 'review-only' / 'quality-correction-summary-source.original.json').read_bytes() == b'preexisting-original-must-remain'
