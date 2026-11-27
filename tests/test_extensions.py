import asyncio
import json
from pathlib import Path

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, CancellationToken, RunContext, Tool, ToolReturn, models
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from myrk.extensions import ExtensionCatalog
from myrk.actions import ActionExecutor
from myrk.conversation import Conversation, Task, TaskScope
from myrk.executor import OfficialExecutor
from myrk.permissions import ApprovalSettings
from myrk.task_control import TaskBudget
from myrk.context import RequestLayout


def response(*, tool=None, arguments=None, text='已完成。', call_id='call'):
    delta = {'role': 'assistant'}
    if tool:
        delta['tool_calls'] = [{'index': 0, 'id': call_id, 'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(arguments or {})}}]
    else:
        delta['content'] = text
    chunk = {'id': call_id, 'object': 'chat.completion.chunk', 'created': 1, 'model': 'deepseek-flash',
             'choices': [{'index': 0, 'delta': delta, 'finish_reason': 'tool_calls' if tool else 'stop'}]}
    return httpx2.Response(200, text=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n', headers={'content-type': 'text/event-stream'})


def test_unused_extensions_leave_final_sdk_prefix_and_tools_unchanged(monkeypatch):
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                model = OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client))
                for names in [[], ['alpha'], ['alpha', 'beta'], ['beta', 'alpha']]:
                    catalog = ExtensionCatalog()
                    for name in names:
                        catalog.add_function(lambda value: value, name=name, description='隐藏目录项')
                    agent = Agent(model, instructions='固定指令。')
                    catalog.install(agent)
                    async with agent.run_stream('固定请求。') as result:
                        await result.get_output()
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    assert len(sent) == 4
    assert all(request['messages'] == sent[0]['messages'] and request['tools'] == sent[0]['tools'] for request in sent)
    assert [tool['function']['name'] for tool in sent[0]['tools']] == ['search_extensions', 'describe_extension', 'call_extension']
    assert 'alpha' not in json.dumps(sent[0]['tools']) and 'beta' not in json.dumps(sent[0]['tools'])


def test_discovery_supports_chinese_exact_names_empty_results_and_prepared_schemas(monkeypatch):
    sent = []
    steps = [('search_extensions', {'query': '表格'}), ('search_extensions', {'query': 'sum_values'}),
             ('search_extensions', {'query': '不存在'}), ('describe_extension', {'name': 'sum_values'}),
             ('describe_extension', {'name': 'restricted'})]
    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) <= len(steps):
            name, args = steps[len(sent) - 1]
            return response(tool=name, arguments=args, call_id=f'discover-{len(sent)}')
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                catalog = ExtensionCatalog()
                @catalog.tool_plain(metadata={'search_keywords': ['表格', '求和']})
                def sum_values(amount: int) -> int:
                    """Sum numeric values."""
                    return amount
                @catalog.tool_plain(prepare=lambda ctx, definition: None)
                def restricted() -> str:
                    return '不可用'
                agent = Agent(OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                catalog.install(agent)
                async with agent.run_stream('先发现，再查看定义。') as result:
                    await result.get_output()
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    results = [json.loads(message['content']) for message in sent[-1]['messages'] if message['role'] == 'tool']
    assert [item['name'] for item in results[0]['tools']] == ['sum_values']
    assert [item['name'] for item in results[1]['tools']] == ['sum_values']
    assert results[2] == {'status': 'ok', 'tools': []}
    assert results[3]['name'] == 'sum_values' and results[3]['schema']['required'] == ['amount']
    assert results[3]['schema']['properties']['amount']['type'] == 'integer'
    assert len(results[3]['schema_version']) == 64
    assert results[4]['status'] == 'unavailable'
    assert all(request['tools'] == sent[0]['tools'] for request in sent)
    assert all(current['messages'][:len(previous['messages'])] == previous['messages'] for previous, current in zip(sent, sent[1:]))


def test_extension_creates_real_file_through_the_same_action_as_direct_tool(tmp_path, monkeypatch):
    evidence = []
    async def scenario():
        for hidden in [False, True]:
            root = tmp_path / ('hidden' if hidden else 'direct')
            root.mkdir()
            task = Task(goal='写入报告', scope=None, approval=ApprovalSettings(mode='full_access'))
            actions = []
            executor = ActionExecutor(actions=actions, budget=TaskBudget(task), save=lambda: None, notify=lambda action: None)
            file_executor = OfficialExecutor(root / 'executor')
            async def create_report(ctx: RunContext[None], path: str, content: str) -> ToolReturn:
                async def create(action):
                    await file_executor.create_file(root / path, content.encode('utf-8'), root, action.approval_settings.access)
                    return 'created:report.txt'
                output = await executor.run('create', path, 'create report', create, tool_call_id=ctx.tool_call_id)
                return ToolReturn(return_value=output, content=['report-content-block'])
            sent = []
            def respond(request):
                payload = json.loads(request.content)
                sent.append(payload)
                results = [message for message in payload['messages'] if message['role'] == 'tool']
                if hidden and len(results) == 0:
                    return response(tool='search_extensions', arguments={'query': '报告'}, call_id='search')
                if hidden and len(results) == 1:
                    return response(tool='describe_extension', arguments={'name': 'create_report'}, call_id='describe')
                if hidden and len(results) == 2:
                    definition = json.loads(results[-1]['content'])
                    return response(tool='call_extension', arguments={'name': 'create_report', 'schema_version': definition['schema_version'],
                        'arguments': {'path': 'report.txt', 'content': '精确报告\n'}}, call_id='create')
                if not hidden and not results:
                    return response(tool='create_report', arguments={'path': 'report.txt', 'content': '精确报告\n'}, call_id='create')
                return response()
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
                async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                    agent = Agent(OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                    if hidden:
                        catalog = ExtensionCatalog()
                        catalog.add_function(create_report, metadata={'search_keywords': ['报告']})
                        catalog.install(agent)
                    else:
                        agent.tool(create_report)
                    async with agent.run_stream('写入报告。') as result:
                        await result.get_output()
            await file_executor.close()
            assert (root / 'report.txt').read_bytes() == '精确报告\n'.encode('utf-8')
            assert len(actions) == 1 and actions[0].kind == 'create' and actions[0].effect == 'completed'
            assert actions[0].tool_call_id == 'create'
            if hidden:
                assert actions[0].tool_name == 'create_report'
                assert actions[0].tool_arguments == {'path': 'report.txt', 'content': '精确报告\n'}
                assert actions[0].tool_source == 'extension'
                assert actions[0].tool_schema_version == definition_version(sent)
            evidence.append(sent[-1]['messages'][-2:])
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    assert all('created:report.txt' in str(messages) and 'report-content-block' in str(messages) for messages in evidence)


def definition_version(sent):
    return next(json.loads(message['content'])['schema_version'] for message in sent[-1]['messages']
                if message['role'] == 'tool' and message.get('tool_call_id') == 'describe')


@pytest.mark.parametrize('invalid_args,error_type', [({}, 'missing'), ({'count': 'not-an-integer'}, 'int_parsing')])
def test_bad_extension_arguments_are_correctable_without_side_effects(tmp_path, monkeypatch, invalid_args, error_type):
    sent = []
    calls = []
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if not results:
            return response(tool='describe_extension', arguments={'name': 'write_report'})
        version = json.loads(results[0]['content'])['schema_version']
        if len(results) <= 2:
            args = invalid_args if len(results) == 1 else {'count': 2}
            return response(tool='call_extension', arguments={'name': 'write_report', 'schema_version': version, 'arguments': args}, call_id=f'write-{len(results)}')
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                catalog = ExtensionCatalog()
                @catalog.tool_plain
                def write_report(count: int) -> str:
                    calls.append(count)
                    (tmp_path / 'report.txt').write_text(str(count), encoding='utf-8')
                    return 'written'
                agent = Agent(OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                catalog.install(agent)
                async with agent.run_stream('修正参数后写入。') as result:
                    await result.get_output()
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    results = [json.loads(message['content']) for message in sent[-1]['messages'] if message['role'] == 'tool' and message['tool_call_id'] == 'write-1']
    assert results[0]['status'] == 'invalid_arguments'
    assert results[0]['errors'][0]['field'] == 'count' and results[0]['errors'][0]['type'] == error_type
    assert calls == [2] and (tmp_path / 'report.txt').read_text(encoding='utf-8') == '2'


def test_changed_actual_schema_requires_explicit_describe_before_any_execution(tmp_path, monkeypatch):
    sent, calls = [], []
    layout = RequestLayout(task_id='schema-task', instructions='fixed')
    catalog = ExtensionCatalog(on_schema_refresh_required=lambda: layout.record_change(['extension_schema_refresh_required']))
    @catalog.tool_plain
    def write_report(count: int) -> str:
        calls.append(count)
        return 'old'
    def revised(count: int, suffix: str = 'new') -> str:
        calls.append(count)
        (tmp_path / 'report.txt').write_text(f'{count}:{suffix}', encoding='utf-8')
        return 'new'
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if not results:
            return response(tool='describe_extension', arguments={'name': 'write_report'}, call_id='old-definition')
        old_version = json.loads(results[0]['content'])['schema_version']
        if len(results) <= 2:
            catalog.tools['write_report'] = Tool(revised, name='write_report')
            return response(tool='call_extension', arguments={'name': 'write_report', 'schema_version': old_version,
                'arguments': {'count': 1}}, call_id=f'stale-{len(results)}')
        if len(results) == 3:
            return response(tool='describe_extension', arguments={'name': 'write_report'}, call_id='new-definition')
        if len(results) == 4:
            version = json.loads(results[-1]['content'])['schema_version']
            return response(tool='call_extension', arguments={'name': 'write_report', 'schema_version': version,
                'arguments': {'count': 2}}, call_id='fresh')
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                agent = Agent(OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                catalog.install(agent)
                async with agent.run_stream('刷新定义后调用。') as result:
                    await result.get_output()
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    results = [json.loads(message['content']) for message in sent[-1]['messages'] if message['role'] == 'tool' and message['tool_call_id'].startswith('stale')]
    assert [item['status'] for item in results] == ['schema_refresh_required', 'schema_refresh_required']
    assert all(item['next'] == 'describe_extension' for item in results)
    assert calls == [2] and (tmp_path / 'report.txt').read_text(encoding='utf-8') == '2:new'
    assert layout.version == 2 and layout.changes[0].reasons == ['extension_schema_refresh_required']


@pytest.mark.parametrize('delegated', [False, True])
def test_conversation_calculates_via_extension_and_writes_real_result(tmp_path, monkeypatch, delegated):
    folder = tmp_path / 'task'
    folder.mkdir()
    (folder / '金额.csv').write_text('item,amount\na,0.10\nb,0.20\nc,-0.05\n', encoding='utf-8')
    sent = []
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        child = any(message['role'] == 'user' and str(message['content']).startswith('受委派工作：') for message in payload['messages'])
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if delegated and not child:
            if not results:
                return response(tool='delegate_work', arguments={'goal': '合计金额并写入结果', 'context': '金额.csv', 'evidence_requirements': '逐行证据与结果文件', 'expected_files': ['result.txt']})
            return response()
        if not results:
            return response(tool='search_extensions', arguments={'query': '表格'})
        if len(results) == 1:
            return response(tool='describe_extension', arguments={'name': 'calculate_table'})
        if len(results) == 2:
            version = json.loads(results[-1]['content'])['schema_version']
            return response(tool='call_extension', arguments={'name': 'calculate_table', 'schema_version': version,
                'arguments': {'path': '金额.csv', 'column': 'amount'}}, call_id='calculate')
        if len(results) == 3:
            assert '合计：0.25' in results[-1]['content'] and '[金额.csv:4]' in results[-1]['content']
            return response(tool='create_text', arguments={'path': 'result.txt', 'content': results[-1]['content']}, call_id='create')
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                path = tmp_path / 'records' / 'conversation.json'
                turn = await Conversation(path).ask('合计金额并写入结果', document=None,
                    scope=TaskScope(root=str(folder), allowed_actions=['read', 'calculate', 'edit'], provider='deepseek', recipients=['deepseek']),
                    model=OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                assert turn.status == 'completed', turn.error
                calculation = next(action for action in turn.actions if action.kind == 'calculate')
                assert calculation.tool_name == 'calculate_table' and calculation.tool_source == 'extension'
                assert calculation.tool_arguments == {'path': '金额.csv', 'column': 'amount'}
                assert bool(calculation.delegation_id) == delegated
                reopened = Conversation.open(path)
                assert next(action for action in reopened.turns[-1].actions if action.kind == 'calculate').tool_arguments == calculation.tool_arguments
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    assert '合计：0.25' in (folder / 'result.txt').read_text(encoding='utf-8')
    assert all('calculate_table' not in [tool['function']['name'] for tool in request['tools']] for request in sent)


@pytest.mark.parametrize('outcome', ['denied', 'stopped', 'final_turn'])
def test_conversation_extension_preserves_approval_stop_and_final_turn_rules(tmp_path, monkeypatch, outcome):
    root = tmp_path / 'task'
    root.mkdir()
    outside = tmp_path / 'outside.csv'
    outside.write_bytes(b'amount\n19.25\n')
    scope = TaskScope(root=str(root), allowed_actions=['read', 'calculate'], provider='deepseek', recipients=['deepseek'])
    task = Task(goal='检查批准与停止', scope=scope, max_turns=3 if outcome == 'final_turn' else None)
    task.approval.access.legacy_scope = True
    token = CancellationToken()
    pending, sent = [], []
    async def approve(action):
        pending.append(action)
        assert action.tool_name == 'calculate_table'
        assert action.tool_arguments == {'path': '../outside.csv', 'column': 'amount'}
        if outcome == 'stopped':
            token.cancel()
            return True
        return False
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if not results:
            return response(tool='search_extensions', arguments={'query': 'calculate_table'})
        if len(results) == 1:
            return response(tool='describe_extension', arguments={'name': 'calculate_table'})
        if len(results) == 2:
            version = json.loads(results[-1]['content'])['schema_version']
            return response(tool='call_extension', arguments={'name': 'calculate_table', 'schema_version': version,
                'arguments': {'path': '../outside.csv', 'column': 'amount'}}, call_id='blocked')
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                turn = await Conversation(tmp_path / 'records' / 'blocked.json').ask(task.goal, document=None, task=task, scope=scope,
                    cancellation_token=token, approve_action=approve,
                    model=OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                if outcome == 'final_turn':
                    assert not turn.actions and not pending and turn.status == 'limited'
                    assert not sent[-1].get('tools')
                else:
                    assert len(pending) == len(turn.actions) == 1
                    assert turn.actions[0].effect == 'not_started'
                    assert turn.actions[0].status == ('denied' if outcome == 'denied' else 'cancelled')
                    assert '19.25' not in turn.actions[0].output
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    assert outside.read_bytes() == b'amount\n19.25\n'


def test_extension_rechecks_prepare_after_description_and_runs_async_argument_validator(monkeypatch):
    sent, calls, validated = [], [], []
    available = True
    catalog = ExtensionCatalog()
    async def validate(ctx: RunContext[None], amount: int):
        validated.append((ctx.tool_name, amount))
        if amount < 0:
            raise ModelRetry('amount must be nonnegative')
    @catalog.tool_plain(prepare=lambda ctx, definition: definition if available else None, args_validator=validate)
    def check_value(amount: int) -> str:
        calls.append(amount)
        return 'accepted'
    def respond(request):
        nonlocal available
        payload = json.loads(request.content)
        sent.append(payload)
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if not results:
            return response(tool='describe_extension', arguments={'name': 'check_value'})
        version = json.loads(results[0]['content'])['schema_version']
        if len(results) <= 3:
            if len(results) == 3:
                available = False
            return response(tool='call_extension', arguments={'name': 'check_value', 'schema_version': version,
                'arguments': {'amount': '-1' if len(results) == 1 else '2'}}, call_id=f'check-{len(results)}')
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                agent = Agent(OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                catalog.install(agent)
                async with agent.run_stream('校验并重新检查可用性。') as result:
                    await result.get_output()
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    results = [message for message in sent[-1]['messages'] if message['role'] == 'tool']
    assert 'amount must be nonnegative' in results[1]['content']
    assert calls == [2] and validated == [('check_value', -1), ('check_value', 2)]
    assert json.loads(results[-1]['content'])['status'] == 'unavailable'


def test_delegated_extension_uses_the_childs_narrower_scope(tmp_path, monkeypatch):
    root = tmp_path / 'task'
    child_root = root / 'child'
    child_root.mkdir(parents=True)
    (child_root / 'amounts.csv').write_bytes(b'amount\n23.5\n')
    parent_scope = TaskScope(root=str(root), allowed_actions=['read', 'calculate'], provider='deepseek', recipients=['deepseek'])
    child_scope = parent_scope.model_copy(update={'root': str(child_root), 'allowed_actions': ['read']})
    task = Task(goal='只读子任务不能计算', scope=parent_scope)
    task.approval.access.legacy_scope = True
    def respond(request):
        payload = json.loads(request.content)
        child = any(message['role'] == 'user' and str(message['content']).startswith('受委派工作：') for message in payload['messages'])
        results = [message for message in payload['messages'] if message['role'] == 'tool']
        if not child:
            if not results:
                return response(tool='delegate_work', arguments={'goal': '只读核查', 'context': '不得扩大动作', 'evidence_requirements': '说明未进行计算',
                    'expected_files': ['child/amounts.csv'], 'scope': child_scope.model_dump()})
            return response()
        if not results:
            return response(tool='describe_extension', arguments={'name': 'calculate_table'})
        if len(results) == 1:
            return response(tool='call_extension', arguments={'name': 'calculate_table', 'schema_version': json.loads(results[0]['content'])['schema_version'],
                'arguments': {'path': 'amounts.csv', 'column': 'amount'}})
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                turn = await Conversation(tmp_path / 'records' / 'child.json').ask(task.goal, document=None, task=task, scope=parent_scope,
                    model=OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)))
                assert turn.status == 'completed', turn.error
                action = next(action for action in turn.actions if action.kind == 'calculate')
                assert action.delegation_id == turn.delegations[0].id
                assert action.tool_name == 'calculate_table' and action.status == 'denied'
                assert '未允许读取和计算' in action.error and action.effect == 'not_started'
                assert '23.5' not in action.output
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())


def test_adding_unused_extension_after_a_sent_step_keeps_old_messages_and_tools(monkeypatch):
    catalog = ExtensionCatalog()
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            catalog.add_function(lambda value: value, name='unused_later', description='后来注册的未使用工具')
            return response(tool='search_extensions', arguments={'query': 'no-match'})
        return response()
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as http:
            async with AsyncOpenAI(api_key='synthetic-only', base_url='https://api.deepseek.com', http_client=http, max_retries=0) as client:
                agent = Agent(OpenAIChatModel('deepseek-flash', provider=DeepSeekProvider(openai_client=client)), instructions='固定指令')
                catalog.install(agent)
                async with agent.run_stream('固定请求') as result:
                    await result.get_output()
    monkeypatch.setattr(models, 'ALLOW_MODEL_REQUESTS', True)
    asyncio.run(scenario())
    assert sent[1]['tools'] == sent[0]['tools']
    assert sent[1]['messages'][:len(sent[0]['messages'])] == sent[0]['messages']
    assert 'unused_later' not in json.dumps(sent[1])
