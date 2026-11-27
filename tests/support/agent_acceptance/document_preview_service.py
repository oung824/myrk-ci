"""Finite native-SDK terminal replay used only to create preview deliveries."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from typing import Any
import httpx2
from pydantic_ai import models
from pydantic_ai.models import Model
from myrk.desktop_service import serve
from myrk.provider import deepseek_model
from tests.support.agent_acceptance.delegation import returned_object
from tests.support.agent_acceptance.replay import OfflineSession, Reply, SDKCapture


def replies(root: Path) -> list[Reply]:
    scenario = json.loads((root / 'scenario.json').read_bytes())
    outputs = scenario['outputs']
    def copy(payload: dict[str, Any]) -> dict[str, Any]:
        version = returned_object(payload, {'name','schema_version','schema'})['schema_version']
        code = 'import shutil; ' + '; '.join(f'shutil.copyfile({source!r},{target!r})' for source,target in outputs)
        return {'name':'run_command_with_outputs','schema_version':version,'arguments':{'program':sys.executable,'arguments':['-B','-c',code],'output_paths':[target for _,target in outputs]}}
    def deliver(payload: dict[str, Any]) -> dict[str, Any]:
        version = returned_object(payload, {'name','schema_version','schema'})['schema_version']
        changes = []
        for message in payload['messages']:
            content = message.get('content','')
            if isinstance(content,str) and '文件改动证据：' in content:
                changes = json.loads(content.split('文件改动证据：',1)[1])
        if len(changes) != len(outputs):
            raise ValueError('Actual public command file changes do not match the controlled delivery materials.')
        return {'name':'deliver_files','schema_version':version,'arguments':{'change_ids':[row['id'] for row in changes]}}
    return [Reply(tool='describe_extension',arguments={'name':'run_command_with_outputs'}),
        Reply(tool='call_extension',arguments=copy), Reply(tool='describe_extension',arguments={'name':'deliver_files'}),
        Reply(tool='call_extension',arguments=deliver), Reply(text='Synthetic documents were delivered for local read-only preview; user acceptance is pending.')]


async def main() -> None:
    root = Path(os.environ['MYRK_ACCEPTANCE_DOCUMENT_ROOT'])
    records = Path(os.environ['MYRK_DATA_DIRECTORY'])
    scenario = json.loads((root/'scenario.json').read_bytes())
    session = OfflineSession('deepseek', replies(root))
    def persist() -> None:
        attempts: list[dict[str, Any]] = []
        for path in records.glob('*.json'):
            for turn in json.loads(path.read_bytes()).get('turns',[]):
                attempts.extend({'task_id':turn['task']['id'],**item} for item in turn.get('request_attempts',[]))
        session.save(root/'exchange.json',attempts=attempts,run_id=scenario['run_id'],case_id=scenario['case_id'],task_id=attempts[0]['task_id'] if attempts else '')
    async def respond(request: httpx2.Request) -> httpx2.Response:
        if request.method != 'POST':
            raise AssertionError('Only the finite creation HTTP script is allowed.')
        response = session.respond(request)
        persist()
        return response
    async def reject_async(_self: httpx2.AsyncHTTPTransport, _request: httpx2.Request) -> httpx2.Response:
        raise AssertionError('Real network is closed for local previews.')
    def reject_sync(_self: httpx2.HTTPTransport, _request: httpx2.Request) -> httpx2.Response:
        raise AssertionError('Real network is closed for local previews.')
    models.ALLOW_MODEL_REQUESTS=True
    setattr(httpx2.AsyncHTTPTransport,'handle_async_request',reject_async)
    setattr(httpx2.HTTPTransport,'handle_request',reject_sync)
    if receipt := os.environ.get('MYRK_PROCESS_RECEIPT_DIRECTORY'):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(receipt),__file__)
    client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond),trust_env=False)
    @asynccontextmanager
    async def factory(key: str) -> AsyncIterator[Model]:
        async with deepseek_model(key,http_client=client) as model:
            yield model
    try:
        await serve(records,factory,http_client=client,model_observer=lambda model: SDKCapture(model,session.sdk_inputs))
    finally:
        persist()
        await client.aclose()

if __name__=='__main__':
    asyncio.run(main())
