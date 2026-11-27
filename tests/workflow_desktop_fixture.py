"""Offline model responses for the built desktop's two complete user workflows."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from pydantic_ai import models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ToolReturnPart, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.profiles import ModelProfile

from myrk.desktop_service import main


async def workflow(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
    prompts = [str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)]
    goal = next(prompt for prompt in reversed(prompts) if not prompt.startswith('补充：'))
    results = {part.tool_call_id: part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)}
    if '用户明确选择继续原工作' in goal:
        yield '原工作已恢复，早前部分回答与错误仍可回查。'
        return
    if '故障恢复验收' in goal:
        yield '故障前已保留这段部分回答。'
        raise ModelHTTPError(503, 'offline-workflow', {'message': 'controlled workflow service outage'})
    if '停止验收' in goal:
        yield '可停止的真实流式回答已开始。'
        await asyncio.sleep(30)
        yield '不应等到这里。'
        return
    if '图片阅读验收' in goal:
        yield '\n\n'.join(f'阅读段落 {index}：保留聊天位置，图片关闭后继续阅读。' for index in range(100))
        for index in range(150):
            await asyncio.sleep(.08)
            yield f'\n\n浮层期间的新段落 {index}：内容持续增加。'
        return
    if '批准验收' in goal:
        if 'outside-allowed' not in results:
            name, arguments = 'create_text', {'path': str(Path(os.environ['MYRK_WORKFLOW_CASE']) / 'outside-allowed.txt'), 'content': '只对这次确切动作批准\n'}
            call_id = 'outside-allowed'
        else:
            yield '你批准的这次动作已经真实完成。'
            return
    elif '拒绝验收' in goal:
        if 'outside' not in results:
            name, arguments = 'create_text', {'path': str(Path(os.environ['MYRK_WORKFLOW_CASE']) / 'outside.txt'), 'content': 'must stay absent'}
            call_id = 'outside'
        else:
            yield '已按你的拒绝保留原件；可以继续讨论新的做法。'
            return
    elif '讨论验收' in goal or '排队验收' in goal:
        yield '后续讨论和排队工作已经正常完成。'
        return
    elif goal.startswith('受委派工作：'):
        if 'child-create' not in results:
            name, arguments, call_id = 'create_text', {'path': '分工说明.md', 'content': '# 分工说明\n\n已独立核对加法边界。\n'}, 'child-create'
        elif 'child-describe' not in results:
            name, arguments, call_id = 'describe_extension', {'name': 'deliver_files'}, 'child-describe'
        elif 'child-deliver' not in results:
            change = json.loads(str(results['child-create'].content).split('文件改动证据：', 1)[1])
            name, arguments, call_id = 'call_extension', {'name': 'deliver_files', 'schema_version': results['child-describe'].content['schema_version'], 'arguments': {'change_ids': [change['id']]}}, 'child-deliver'
        else:
            yield '分工说明已明确交付，请主助理核对真实内容。'
            return
    elif '编程完整验收' in goal:
        steps = [
            ('guidance', 'read_text', {'path': 'calculator.py'}),
            ('read', 'read_text', {'path': 'calculator.py'}),
            ('edit', 'edit_text', {'path': 'calculator.py', 'old_text': 'return left - right', 'new_text': 'return left + right'}),
            ('delegate', 'delegate_work', {'goal': '独立核对并交付分工说明', 'context': '新建分工说明.md，写明已独立核对加法边界。', 'evidence_requirements': '真实文件改动且明确交付', 'expected_files': ['分工说明.md']}),
            ('verify', 'verify_delegation', {}),
            ('check', 'run_check', {'name': '指定检查'}),
            ('describe', 'describe_extension', {'name': 'deliver_files'}),
            ('deliver', 'call_extension', {}),
        ]
        pending = next((step for step in steps if step[0] not in results), None)
        if pending is None:
            yield '加法程序与分工说明已交付；指定检查通过。\n\n可以在成果区阅读完整代码和真实修改对照。'
            return
        call_id, name, arguments = pending
        if call_id == 'guidance':
            yield '正在核对程序；现在可以补充要求或排队新工作。'
            await asyncio.sleep(1)
        if call_id == 'verify':
            report = json.loads(str(results['delegate'].content))
            arguments = {'delegation_id': report['id'], 'path': '分工说明.md', 'expected_text': '# 分工说明\n\n已独立核对加法边界。\n'}
        if call_id == 'deliver':
            change = json.loads(str(results['edit'].content).split('文件改动证据：', 1)[1])
            arguments = {'name': 'deliver_files', 'schema_version': results['describe'].content['schema_version'], 'arguments': {'change_ids': [change['id']]}}
    elif '资料完整验收' in goal:
        outputs = json.loads(os.environ['MYRK_WORKFLOW_OUTPUTS'])
        if 'command-description' not in results:
            yield '正在处理合成资料；现在可以补充要求或排队新工作。'
            await asyncio.sleep(1)
            name, arguments, call_id = 'describe_extension', {'name': 'run_command_with_outputs'}, 'command-description'
        elif 'command' not in results:
            script = 'import shutil; ' + '; '.join(f'shutil.copyfile({source!r}, {target!r})' for source, target in outputs)
            script += "; open('processing.log','w').write('not a delivery')"
            name, arguments, call_id = 'call_extension', {'name': 'run_command_with_outputs', 'schema_version': results['command-description'].content['schema_version'], 'arguments': {'program': sys.executable, 'arguments': ['-c', script], 'output_paths': [target for _, target in outputs]}}, 'command'
        elif 'description' not in results:
            name, arguments, call_id = 'describe_extension', {'name': 'deliver_files'}, 'description'
        elif 'delivery' not in results:
            changes = json.loads(str(results['command'].content).split('文件改动证据：', 1)[1])
            name, arguments, call_id = 'call_extension', {'name': 'deliver_files', 'schema_version': results['description'].content['schema_version'], 'arguments': {'change_ids': [change['id'] for change in changes]}}, 'delivery'
        else:
            yield '资料已明确交付；原始输入和处理日志没有混入成果。'
            for index in range(50):
                await asyncio.sleep(.08)
                yield f'\n\n交付后的阅读进度 {index}：文件内容保持打开时的快照。'
            return
    else:
        yield '普通讨论正常完成。'
        return
    yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments, ensure_ascii=False), tool_call_id=call_id)}


@asynccontextmanager
async def model_factory(_key: str) -> AsyncIterator[Model]:
    yield FunctionModel(stream_function=workflow, profile=ModelProfile(context_window=128000, supports_inline_system_prompts=True))


if __name__ == '__main__':
    models.ALLOW_MODEL_REQUESTS = False
    if receipts := os.environ.get('MYRK_PROCESS_RECEIPT_DIRECTORY'):
        from tests.support.process_receipt import write_process_receipt
        write_process_receipt(Path(receipts), __file__)
    main(data_directory=Path(sys.argv[1]) if len(sys.argv) > 1 else None, model_factory=model_factory)
