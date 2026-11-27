"""Offline model script that produces and explicitly delivers synthetic documents."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from pydantic_ai import models
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile

from myrk.desktop_service import main


async def documents(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
    outputs = json.loads(os.environ['MYRK_DOCUMENT_OUTPUTS'])
    if not results:
        name, arguments = 'describe_extension', {'name': 'run_command_with_outputs'}
    elif len(results) == 1:
        script = 'import shutil; ' + '; '.join(f'shutil.copyfile({source!r}, {target!r})' for source, target in outputs)
        name, arguments = 'call_extension', {
            'name': 'run_command_with_outputs', 'schema_version': results[0].content['schema_version'],
            'arguments': {'program': sys.executable, 'arguments': ['-c', script], 'output_paths': [target for _, target in outputs]},
        }
    elif len(results) == 2:
        name, arguments = 'describe_extension', {'name': 'deliver_files'}
    elif len(results) == 3:
        changes = json.loads(str(results[1].content).split('文件改动证据：', 1)[1])
        name, arguments = 'call_extension', {
            'name': 'deliver_files', 'schema_version': results[2].content['schema_version'],
            'arguments': {'change_ids': [change['id'] for change in changes]},
        }
    else:
        yield '已交付本任务的合成文档，可在右侧只读查看。'
        return
    yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=f'documents-{len(results)}')}


@asynccontextmanager
async def model_factory(_key: str) -> AsyncIterator[Model]:
    yield FunctionModel(stream_function=documents, profile=ModelProfile(context_window=128000))


if __name__ == '__main__':
    models.ALLOW_MODEL_REQUESTS = False
    if receipts := os.environ.get('MYRK_PROCESS_RECEIPT_DIRECTORY'):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(receipts), __file__)
    main(data_directory=Path(sys.argv[1]) if len(sys.argv) > 1 else None, model_factory=model_factory)
